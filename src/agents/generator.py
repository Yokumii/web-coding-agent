from __future__ import annotations

import hashlib
from html.parser import HTMLParser
from pathlib import Path
import json
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Literal

from src.agents._shared import expose_local_claude_skills
from src.agents.sdk_runner import (
    AgentRunStats,
    build_agent_run_stats,
    run_sdk_agent,
)
from src.agents.openai_runner import OpenAIHTTPClient
from src.config import HarnessConfig
from src.orchestration.design_contract import DesignContractContext
from src.orchestration.edit_dom_guard import repair_baseline_name
from src.orchestration.edit_context import read_edit_context, render_edit_context
from src.orchestration.edit_task_contract import chain_obligations, read_edit_task_contract
from src.orchestration.file_comm import FileComm
from src.orchestration.git_journal import ensure_repo
from src.orchestration.minimal_path_guidance import (
    MinimalPathPolicy,
    effective_patch_line_count,
    expected_fragment_consumes_scope_slot,
    plan_name,
)
from src.orchestration.round_artifacts import RoundArtifacts
from src.orchestration.sprint_state import SprintState
from src.orchestration.target_profile import (
    target_profile_guidance,
    validate_target_submission,
)
from src.orchestration.task_inputs import (
    openai_user_content,
    task_input_image_paths,
    task_input_prompt_context,
)
from src.orchestration.pricing import estimate_cost_usd
from src.prompts.generator import GENERATOR_SYSTEM_PROMPT
from src.prompts.edit import EDIT_SYSTEM_PROMPT
from src.prompts.repair import REPAIR_SYSTEM_PROMPT
from src.prompts.grading import criterion_threshold
from src.utils.logger import get_logger
from src.utils.llm_json import extract_json_object

logger = get_logger(__name__)

GeneratorMode = Literal["generate", "repair"]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_CLAUDE_SKILLS_DIR = _REPO_ROOT / ".claude" / "skills"
_GENERATE_REQUIRED_READS = (
    ".harness/sprint_plan.json",
    ".harness/design_tokens.json",
    ".harness/ui_verification_plan.json",
)
_MAX_REPAIR_FILES = 4
_MAX_REPAIR_CHANGED_LINES = 1000
_MAX_ATOMIC_SEMANTIC_ATTEMPTS = 1
_REMOTE_URL_RE = re.compile(r"https?://[^\s'\"<>),]+", re.IGNORECASE)
_HTML_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


class AtomicCandidateRejected(RuntimeError):
    """A paid compact candidate failed deterministic local validation."""


class _OpenHTMLTagParser(HTMLParser):
    def __init__(self, source_lines: list[str]) -> None:
        super().__init__(convert_charrefs=False)
        self.source_lines = source_lines
        self.stack: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in _HTML_VOID_TAGS:
            return
        line_number, _ = self.getpos()
        source_line = self.source_lines[min(line_number - 1, len(self.source_lines) - 1)]
        indent = len(source_line) - len(source_line.lstrip())
        self.stack.append((tag.lower(), indent))

    def handle_endtag(self, tag: str) -> None:
        target = tag.lower()
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == target:
                del self.stack[index:]
                return


class _HTMLBalanceParser(HTMLParser):
    """Small structural checker used comparatively against the seed file."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.mismatches = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag not in _HTML_VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        target = tag.lower()
        if self.stack and self.stack[-1] == target:
            self.stack.pop()
            return
        self.mismatches += 1
        if target in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(target)
            del self.stack[index:]

    @property
    def error_score(self) -> int:
        return self.mismatches + len(self.stack)


def _html_balance_error_score(content: str) -> int:
    parser = _HTMLBalanceParser()
    parser.feed(content)
    parser.close()
    return parser.error_score


def _validate_html_structure_preserved(
    before: str, after: str, *, expected_path: str
) -> None:
    if Path(expected_path).suffix.lower() not in {".html", ".htm"}:
        return
    before_errors = _html_balance_error_score(before)
    after_errors = _html_balance_error_score(after)
    if after_errors > before_errors:
        raise ValueError(
            f"unbalanced HTML edit for {expected_path}: structural error score "
            f"increased from {before_errors} to {after_errors}; preserve complete tag boundaries"
        )


def _validate_html_insertion_boundary(
    lines: list[str], *, after_line: int, insertion: str, expected_path: str
) -> None:
    """Reject indentation that would silently nest a requested sibling.

    Compact line edits intentionally avoid exposing the whole HTML file.  The
    immutable prefix is still enough to detect the common destructive error
    where a model inserts a low-indented ``section`` before closing a deeper
    ``div``.  Equal indentation remains valid because HTML is often formatted
    with a closing sibling at the parent's indentation.
    """
    if Path(expected_path).suffix.lower() not in {".html", ".htm"}:
        return
    first = next((line for line in insertion.splitlines() if line.strip()), "")
    if not first or not first.lstrip().startswith("<") or first.lstrip().startswith("</"):
        return
    parser = _OpenHTMLTagParser(lines[:after_line])
    parser.feed("".join(lines[:after_line]))
    parser.close()
    if not parser.stack:
        return
    insertion_indent = len(first) - len(first.lstrip())
    open_tag, open_indent = parser.stack[-1]
    # Body children commonly start at column zero, as does <body> itself.
    # Only a nested content container can indicate an unintended sibling here.
    if (
        insertion_indent < open_indent
        or (insertion_indent == open_indent and open_indent > 0)
    ) and open_tag not in {"html", "body"}:
        raise ValueError(
            f"unsafe HTML insertion boundary for {expected_path}: content indent "
            f"{insertion_indent} would escape still-open <{open_tag}> at indent "
            f"{open_indent}; insert after its complete closing boundary"
        )


def _rebase_malformed_html_addition(
    content: str,
    lines: list[str],
    operation: dict[str, Any],
    *,
    expected_path: str,
) -> dict[str, Any] | None:
    """Recover a pure new subtree encoded as a destructive line replacement.

    A compact-context model can correctly author a new HTML subtree but attach
    it to a nearby line coordinate while also echoing closing/opening context.
    Recovery is deliberately narrow: the original candidate must worsen HTML
    balance, exactly one novel ``data-testid`` opener must identify a complete
    subtree, and a unique nearest insertion boundary must preserve structure.
    Existing source bytes are then retained verbatim.
    """
    if Path(expected_path).suffix.lower() not in {".html", ".htm"}:
        return None
    start = operation.get("start_line")
    end = operation.get("end_line")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or not 1 <= start <= end <= len(lines)
    ):
        return None
    replacement = str(operation.get("replacement") or "")
    trial_lines = list(lines)
    trial_lines[start - 1 : end] = [
        replacement + ("\n" if replacement and not replacement.endswith(("\n", "\r")) else "")
    ]
    if _html_balance_error_score("".join(trial_lines)) <= _html_balance_error_score(content):
        return None

    replacement_lines = replacement.splitlines(keepends=True)
    novel_openers: list[tuple[int, str, int]] = []
    for index, line in enumerate(replacement_lines):
        match = re.search(
            r"<([A-Za-z][\w:-]*)\b[^>]*\bdata-testid\s*=\s*(['\"])[^'\"]+\2[^>]*>",
            line,
        )
        if match is not None and line.strip() not in content:
            novel_openers.append(
                (index, match.group(1).lower(), len(line) - len(line.lstrip()))
            )
    if not novel_openers:
        return None
    min_indent = min(item[2] for item in novel_openers)
    roots = [item for item in novel_openers if item[2] == min_indent]
    if len(roots) != 1:
        return None
    subtree_start, tag, _indent = roots[0]
    depth = 0
    subtree_end: int | None = None
    token_pattern = re.compile(rf"<(/?){re.escape(tag)}\b[^>]*>", re.IGNORECASE)
    for index in range(subtree_start, len(replacement_lines)):
        for token in token_pattern.finditer(replacement_lines[index]):
            raw = token.group(0)
            if token.group(1):
                depth -= 1
            elif not raw.rstrip().endswith("/>"):
                depth += 1
        if depth == 0:
            subtree_end = index
            break
    if subtree_end is None:
        return None
    if any(
        index < subtree_start or index > subtree_end
        for index, _nested_tag, _nested_indent in novel_openers
    ):
        return None
    while subtree_start > 0:
        prior = replacement_lines[subtree_start - 1].strip()
        if not prior or re.fullmatch(r"<!--.*-->", prior):
            subtree_start -= 1
            continue
        break
    subtree = "".join(replacement_lines[subtree_start : subtree_end + 1])
    if not subtree.strip():
        return None
    source_stripped = {line.strip() for line in lines if line.strip()}
    outside = replacement_lines[:subtree_start] + replacement_lines[subtree_end + 1 :]
    if any(line.strip() and line.strip() not in source_stripped for line in outside):
        return None
    if not any(
        line.strip()
        and re.fullmatch(r"</[A-Za-z][\w:-]*\s*>", line.strip()) is None
        for line in outside
    ):
        return None

    candidates: list[tuple[int, int]] = []
    for after_line in range(max(0, start - 4), min(len(lines), end + 12) + 1):
        try:
            _validate_html_insertion_boundary(
                lines,
                after_line=after_line,
                insertion=subtree,
                expected_path=expected_path,
            )
        except ValueError:
            continue
        inserted = list(lines)
        inserted[after_line:after_line] = [
            subtree + ("\n" if not subtree.endswith(("\n", "\r")) else "")
        ]
        if _html_balance_error_score("".join(inserted)) <= _html_balance_error_score(content):
            candidates.append((abs(after_line - end), after_line))
    if not candidates:
        return None
    candidates.sort()
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    return {
        "op": "insert_after",
        "path": operation.get("path"),
        "file_sha256": operation.get("file_sha256"),
        "after_line": candidates[0][1],
        "content": subtree.rstrip("\r\n"),
    }


def _native_openai_runtime(config: HarnessConfig) -> bool:
    runtime = config.agent_runtime.strip().lower()
    model = config.generator_model.strip().lower()
    return runtime == "openai" or (
        runtime == "auto"
        and model.startswith(("deepseek", "qwen", "gpt-", "o1", "o3", "o4"))
    )


def _validate_javascript_syntax(
    frontend_dir: Path, relative_path: str
) -> tuple[bool, str]:
    """Parse classic and ESM JavaScript despite Node's ambiguous `.js` mode.

    Node 24 can return success for ``node --check file.js`` when the file uses
    ESM syntax outside a package type declaration, even for a later unmatched
    brace. Checking the same immutable bytes through a temporary `.mjs` path
    closes that false-negative without executing application code.
    """
    target = frontend_dir / relative_path
    check_path = target
    temporary_path: Path | None = None
    try:
        source = target.read_text(encoding="utf-8")
        if target.suffix.lower() == ".js" and re.search(
            r"(?m)^\s*(?:import\s|export\s)", source
        ):
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".mjs",
                prefix="web-coding-syntax-",
                delete=False,
            ) as handle:
                handle.write(source)
                temporary_path = Path(handle.name)
            check_path = temporary_path
        result = subprocess.run(
            ["node", "--check", str(check_path)],
            cwd=frontend_dir,
            text=True,
            capture_output=True,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except OSError as exc:
        return False, str(exc)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_rejection_feedback(exc: Exception) -> str:
    """Add one deterministic repair hint for common transactional failures."""
    message = str(exc)[:4000]
    duplicate = re.search(r"Identifier ['\"]([^'\"]+)['\"] has already been declared", message)
    if duplicate is not None:
        symbol = duplicate.group(1)
        message += (
            f"\nHARD: the candidate puts multiple {symbol} declarations in one scope. "
            "This can also result from deleting a closing brace or callback boundary, "
            "even when no declaration was added. Inspect the exact numbered original "
            "lines for every replacement, especially lines containing only }); or }. "
            "Preserve the complete original boundary; replace the intended statement's "
            "actual line or use insert_after without deleting its closing delimiter. "
            "Only remove a duplicate declaration if the candidate actually added one."
        )
    elif "overlapping atomic line edits for " in message:
        message += (
            "\nHARD: merge overlapping operations for that file into one replacement covering "
            "the union of the intended original lines; keep other declarations outside that "
            "range unchanged."
        )
    return message


def _normalize_atomic_patch_response(payload: dict[str, Any]) -> list[dict[str, str]]:
    def _strip_trailing_horizontal_whitespace(value: str) -> str:
        return re.sub(r"(?m)[ \t]+(?=\r?$)", "", value)

    patches: list[dict[str, str]] = []
    items = list(payload.get("patches") or []) + [
        item for item in payload.get("operations") or []
        if isinstance(item, dict) and (item.get("op") or item.get("operation")) == "patches"
    ]
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if path and not path.startswith("frontend/"):
            path = f"frontend/{path}"
        patches.append(
            {
                "path": path,
                "old_text": str(item.get("old_text", item.get("search", ""))),
                "new_text": _strip_trailing_horizontal_whitespace(
                    str(item.get("new_text", item.get("replace", "")))
                ),
            }
        )
    if not patches:
        raise ValueError("atomic Edit executor returned no exact patches")
    return patches


def _normalize_atomic_new_files(payload: dict[str, Any]) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for item in payload.get("new_files") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if path and not path.startswith("frontend/"):
            path = f"frontend/{path}"
        files.append({"path": path, "content": str(item.get("content") or "")})
    return files


def _atomic_new_path_allowed(
    config: HarnessConfig, relative: str, planned_new_paths: set[str]
) -> bool:
    """Only enforce planner file predictions when minimality checking is enabled."""
    return not config.minimality_guard_enabled or relative in planned_new_paths


def _atomic_transaction_sort_key(
    item: tuple[str, str, Any], initial_paths: set[str]
) -> tuple[int, bool, str, int]:
    """Snapshot copy sources before any same-response mutation can change them."""
    operation, relative, _ = item
    return (
        0 if operation == "copy_from" else 1,
        relative not in initial_paths,
        relative,
        0 if operation in {"line_edits", "apply_patch"} else 1,
    )


def _atomic_frontend_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if path and not path.startswith("frontend/"):
        path = f"frontend/{path}"
    return path


def _normalize_atomic_operations(
    payload: dict[str, Any], source_revisions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Normalize the compact SHA/line protocol without weakening exact-patch fallback."""
    output: list[dict[str, Any]] = []
    for item in payload.get("operations") or []:
        if not isinstance(item, dict):
            continue
        operation = str(item.get("op") or item.get("operation") or "").strip()
        if operation == "patches":
            continue  # Normalized by the exact-text patch path.
        if operation in {"replace_lines", "insert_after"}:
            normalized: dict[str, Any] = {
                "op": operation,
                "path": _atomic_frontend_path(item.get("path")),
                "file_sha256": str(item.get("file_sha256") or "").strip(),
            }
            if source_revisions is not None:
                if normalized["path"] not in source_revisions:
                    raise ValueError(f"source was not supplied to the model: {normalized['path']}")
                normalized["file_sha256"] = source_revisions[normalized["path"]]
            if operation == "replace_lines":
                normalized.update({
                    "start_line": item.get("start_line"),
                    "end_line": item.get("end_line"),
                    "replacement": re.sub(
                        r"(?m)[ \t]+(?=\r?$)", "", str(item.get("replacement") or "")
                    ),
                })
            else:
                normalized.update({
                    "after_line": item.get("after_line"),
                    "content": re.sub(
                        r"(?m)[ \t]+(?=\r?$)", "", str(item.get("content") or "")
                    ),
                })
            output.append(normalized)
            continue
        if operation == "copy_from":
            line_edits = _normalize_atomic_operations(
                {"operations": item.get("line_edits") or []}
            )
            source_path = _atomic_frontend_path(item.get("source"))
            source_hash = str(item.get("source_sha256") or "").strip()
            if source_revisions is not None:
                if source_path not in source_revisions:
                    raise ValueError(f"copy source was not supplied to the model: {source_path}")
                source_hash = source_revisions[source_path]
                for edit in line_edits:
                    edit["file_sha256"] = source_hash
            output.append({
                "op": operation,
                "source": source_path,
                "path": _atomic_frontend_path(item.get("path")),
                "source_sha256": source_hash,
                "line_edits": line_edits,
            })
            continue
        raise ValueError(f"unsupported atomic operation: {operation}")
    return output


def _apply_sha_line_operations(
    content: str,
    operations: list[dict[str, Any]],
    *,
    expected_path: str,
    expected_sha256: str,
    preserve_operations: bool = False,
) -> tuple[str, list[dict[str, str]]]:
    """Apply non-overlapping line edits against one immutable source revision."""
    actual_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if not expected_sha256 or expected_sha256 != actual_sha256:
        raise ValueError(f"atomic source SHA mismatch for {expected_path}")
    lines = content.splitlines(keepends=True)

    def structural_section_range(
        start: int, end: int, replacement: str
    ) -> tuple[int, int]:
        """Rebase a section replacement that omitted its comment boundary.

        Coding models often count the first declaration below a section header
        as line one, then leave the old closing braces behind. An immutable file
        hash plus an adjacent, same-indent comment boundary lets the Harness
        repair that off-by-one range deterministically without guessing code.
        """
        replacement_first = next(
            (line for line in replacement.splitlines() if line.strip()), ""
        )
        replacement_last = next(
            (line for line in reversed(replacement.splitlines()) if line.strip()), ""
        )
        function_match = re.match(
            r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
            replacement_first,
        )
        source_function_header = (
            lines[start - 1]
            if 1 <= start <= len(lines)
            and re.match(
                r"^\s*(?:async\s+)?function\s+[A-Za-z_$][\w$]*\s*\(",
                lines[start - 1],
            )
            else ""
        )
        if source_function_header and not function_match:
            source_indent = len(source_function_header) - len(
                source_function_header.lstrip()
            )
            replacement_indent = len(replacement_first) - len(
                replacement_first.lstrip()
            )
            if replacement_indent > source_indent and start < end:
                # The replacement is clearly an indented body slice, but its
                # immutable coordinate accidentally includes the immediately
                # preceding function declaration. Preserve that declaration.
                return start + 1, end
        replacement_functions = re.findall(
            r"(?m)^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
            replacement,
        )
        if (
            function_match
            and len(replacement_functions) == 1
            and replacement_last.strip().endswith("}")
        ):
            name = function_match.group(1)
            source_match = re.compile(
                rf"^\s*(?:async\s+)?function\s+{re.escape(name)}\s*\("
            )
            declarations = [
                line_number
                for line_number in range(max(1, start - 2), start + 1)
                if line_number <= len(lines)
                and source_match.match(lines[line_number - 1])
            ]
            if len(declarations) == 1:
                function_start = declarations[0]
                header = lines[function_start - 1]
                indent = len(header) - len(header.lstrip())
                for line_number in range(function_start + 1, len(lines) + 1):
                    candidate = lines[line_number - 1]
                    if (
                        len(candidate) - len(candidate.lstrip()) == indent
                        and candidate.lstrip().startswith("}")
                    ):
                        return function_start, line_number
        if not replacement_first.lstrip().startswith("//"):
            return start, end
        if replacement_last.lstrip().startswith("//"):
            # A prefix edit inside a section is not evidence that the model
            # supplied a complete replacement for the untouched section body.
            return start, end

        def comment_words(value: str) -> set[str]:
            return {
                word
                for word in re.findall(r"[a-z0-9]+", value.casefold())
                if len(word) >= 3
            }

        replacement_words = comment_words(replacement_first)
        prior_candidates: list[int] = []
        for line_number in range(max(1, start - 2), start + 1):
            source_line = lines[line_number - 1] if line_number <= len(lines) else ""
            if not source_line.lstrip().startswith("//"):
                continue
            source_words = comment_words(source_line)
            if len(source_words) >= 2 and source_words <= replacement_words:
                prior_candidates.append(line_number)
        if not prior_candidates:
            return start, end
        section_start = max(prior_candidates)
        source_header = lines[section_start - 1]
        indent = len(source_header) - len(source_header.lstrip())
        # An explicit range that already includes the following section
        # header must not be expanded to EOF. The next line is often a
        # protected bootstrap call such as DOMContentLoaded registration.
        if any(
            lines[line_number - 1].lstrip().startswith("//")
            and len(lines[line_number - 1])
            - len(lines[line_number - 1].lstrip())
            <= indent
            for line_number in range(section_start + 1, min(end, len(lines)) + 1)
        ):
            return section_start, end
        section_end = len(lines)
        for line_number in range(max(end + 1, section_start + 1), len(lines) + 1):
            candidate = lines[line_number - 1]
            if (
                candidate.lstrip().startswith("//")
                and len(candidate) - len(candidate.lstrip()) <= indent
            ):
                section_end = line_number - 1
                break
        return section_start, section_end

    operations = operations if preserve_operations else [
        _rebase_malformed_html_addition(
            content, lines, item, expected_path=expected_path
        )
        or item
        if item.get("op") == "replace_lines"
        else item
        for item in operations
    ]
    normalized: list[tuple[float, int, int, str, dict[str, str]]] = []
    occupied: set[int] = set()
    insertion_points: set[int] = set()
    for item in operations:
        if item.get("path") not in {"", expected_path}:
            raise ValueError(f"copy line edit escapes destination {expected_path}")
        if item.get("file_sha256") and item.get("file_sha256") != expected_sha256:
            raise ValueError(f"atomic line edit SHA mismatch for {expected_path}")
        if item.get("op") == "replace_lines":
            start = item.get("start_line")
            end = item.get("end_line")
            if (
                isinstance(start, bool) or not isinstance(start, int)
                or isinstance(end, bool) or not isinstance(end, int)
                or not 1 <= start <= end <= len(lines)
            ):
                raise ValueError(f"invalid line range for {expected_path}: {start}-{end}")
            if not preserve_operations:
                start, end = structural_section_range(
                    start, end, str(item.get("replacement") or "")
                )
            span = set(range(start, end + 1))
            if occupied & span:
                raise ValueError(f"overlapping atomic line edits for {expected_path}")
            occupied.update(span)
            old_text = "".join(lines[start - 1 : end])
            new_text = str(item.get("replacement") or "")
            if old_text.endswith(("\n", "\r")) and new_text and not new_text.endswith(("\n", "\r")):
                new_text += "\n"
            normalized.append((float(start), start - 1, end, new_text, {
                "path": expected_path,
                "old_text": old_text,
                "new_text": new_text,
                "_harness_source_sha256": expected_sha256,
                "_harness_start_line": start,
                "_harness_end_line": end,
            }))
        elif item.get("op") == "insert_after":
            after = item.get("after_line")
            if (
                isinstance(after, bool) or not isinstance(after, int)
                or not 0 <= after <= len(lines) or after in insertion_points
            ):
                raise ValueError(f"invalid or duplicate insertion point for {expected_path}: {after}")
            insertion_points.add(after)
            insertion = str(item.get("content") or "")
            if not insertion:
                raise ValueError(f"empty atomic insertion for {expected_path}")
            if (
                not preserve_operations
                and Path(expected_path).suffix.lower()
                in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
                and re.search(
                    r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+"
                    r"[A-Za-z_$][\w$]*\s*\(",
                    insertion,
                )
                and after < len(lines)
                and re.fullmatch(r"}\s*;?\s*(?:\r?\n)?", lines[after])
            ):
                # The model selected the last statement inside a complete
                # top-level function (commonly the closing ``});`` of a loop)
                # instead of the immediately following function brace. Move a
                # new named function across that one unambiguous boundary.
                after += 1
                if after in insertion_points:
                    raise ValueError(
                        f"duplicate rebased insertion point for {expected_path}: {after}"
                    )
                insertion_points.add(after)
            if not preserve_operations:
                _validate_html_insertion_boundary(
                    lines, after_line=after, insertion=insertion, expected_path=expected_path,
                )
            if lines and insertion and not insertion.endswith(("\n", "\r")):
                insertion += "\n"
            if lines:
                if after == 0:
                    anchor = lines[0]
                    old_text, new_text = anchor, insertion + anchor
                else:
                    # A blank or common brace is not a unique exact-patch
                    # anchor. Grow backward only to the shortest unique suffix
                    # ending at the immutable line coordinate.
                    old_text = ""
                    for width in range(1, min(8, after) + 1):
                        candidate = "".join(lines[after - width : after])
                        if candidate.strip() and content.count(candidate) == 1:
                            old_text = candidate
                            break
                    if not old_text:
                        old_text = "".join(lines[max(0, after - 8) : after])
                    new_text = old_text + insertion
            else:
                old_text, new_text = "", insertion
            normalized.append((after + 0.5, after, after, insertion, {
                "path": expected_path, "old_text": old_text, "new_text": new_text,
            }))
        else:
            raise ValueError(f"unsupported atomic line operation for {expected_path}")
    updated = list(lines)
    for _position, start_index, end_index, replacement, tool_input in sorted(
        normalized, key=lambda value: value[0], reverse=True
    ):
        if start_index == end_index and tool_input["old_text"] != "" and (
            tool_input["new_text"].startswith(tool_input["old_text"])
            or tool_input["new_text"].endswith(tool_input["old_text"])
        ):
            updated[start_index:start_index] = [replacement]
        else:
            updated[start_index:end_index] = [replacement]
    updated_content = "".join(updated)
    if not preserve_operations:
        _validate_html_structure_preserved(
            content, updated_content, expected_path=expected_path
        )
    return updated_content, [item[4] for item in normalized]


def _control_topology_invariants(
    checks: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Derive concrete DOM placement constraints from ordered browser actions."""
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for check in checks:
        route = str(check.get("route") or "/")
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        for index, action in enumerate(actions):
            if action.get("action") != "assert_hidden" or not action.get("selector"):
                continue
            target = str(action["selector"])
            for later in actions[index + 1 :]:
                if (
                    later.get("action") == "assert_visible"
                    and later.get("selector") == target
                ):
                    break
                if later.get("action") != "click" or not later.get("selector"):
                    continue
                control = str(later["selector"])
                key = (route, control, target)
                if key in seen:
                    continue
                seen.add(key)
                output.append(
                    {
                        "route": route,
                        "control_selector": control,
                        "hidden_target_selector": target,
                        "constraint": "control_must_not_be_descendant_of_hidden_target",
                    }
                )
    return output


def _render_control_topology_directives(
    invariants: list[dict[str, str]],
) -> str:
    if not invariants:
        return "(none)"
    return "\n".join(
        "- HARD: insert {control} before the opening element for {target}, as a sibling; "
        "never place {control} between that target's opening and closing tags. Toggle the "
        "visibility of {target} itself, while {control} remains visible.".format(
            control=item["control_selector"],
            target=item["hidden_target_selector"],
        )
        for item in invariants
    )


def _render_failed_action_directives(
    repair_packet: dict[str, Any], checks: list[dict[str, Any]]
) -> str:
    checks_by_id = {str(item.get("id")): item for item in checks}
    directives: list[str] = []
    for failure in repair_packet.get("failed_checks") or []:
        if not isinstance(failure, dict):
            continue
        visual_sanity = failure.get("visual_sanity")
        if isinstance(visual_sanity, dict):
            for issue in visual_sanity.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                directives.append(
                    "- HARD: the Harness computed-style audit found nearly unreadable "
                    f"text at {issue.get('selector')} (contrast {issue.get('ratio')}:1, "
                    f"foreground {issue.get('foreground')}, background "
                    f"{issue.get('background')}). Reuse the accepted local theme's "
                    "defined text and surface tokens for that target; do not alter "
                    "unrelated global colors."
                )
        for console_error in failure.get("console_errors") or []:
            error_text = str(console_error or "").strip()
            undefined = re.search(
                r"\b([A-Za-z_$][\w$]*) is not defined\b", error_text
            )
            if undefined:
                name = undefined.group(1)
                directives.append(
                    f"- HARD: the real browser raised `{name} is not defined` at the "
                    "current call site. Inspect the existing declaration and caller in the "
                    "shown source window, then place that existing function in a common "
                    "enclosing lexical scope (or move the caller into its scope). Do not hide "
                    "the defect with `typeof`, optional checks, try/catch, or a duplicate "
                    "helper; the call must actually execute after the binding is available."
                )
                directives.append(
                    f"- MINIMAL PATH: if `{name}` is intentionally inside an existing feature "
                    "guard while a sibling handler needs it, keep the function in place. "
                    "Declare one distinctly named nullable callback binding in their common "
                    f"outer scope, assign the existing `{name}` function reference to it inside "
                    "the owner block after the declaration, and invoke that binding from the "
                    "sibling handler. For the assignment, use one `insert_after` operation on "
                    "the function's existing closing line and insert only the assignment; do "
                    "not repeat or replace any closing brace. This is a three-site bridge; do "
                    "not move or copy the large feature block."
                )
        check = checks_by_id.get(str(failure.get("check_id"))) or {}
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        steps = [item for item in failure.get("steps") or [] if isinstance(item, dict)]
        for index, step in enumerate(steps):
            if step.get("ok") is not False or index >= len(actions):
                continue
            action = actions[index]
            kind = str(action.get("action") or step.get("action") or "")
            target = str(action.get("selector") or "")
            preceding_control = next(
                (
                    str(actions[prior].get("selector"))
                    for prior in range(index - 1, -1, -1)
                    if actions[prior].get("action") == "click"
                    and actions[prior].get("selector")
                ),
                "",
            )
            preceding_hash = next(
                (
                    str(actions[prior].get("value"))
                    for prior in range(index - 1, -1, -1)
                    if actions[prior].get("action") == "set_hash"
                    and actions[prior].get("value")
                ),
                "",
            )
            reload_before_failure = any(
                actions[prior].get("action") == "reload"
                for prior in range(index)
            )
            target_worked_before_reload = any(
                str(actions[prior].get("selector") or "") == target
                and prior < len(steps)
                and steps[prior].get("ok") is True
                and any(
                    actions[middle].get("action") == "reload"
                    for middle in range(prior + 1, index)
                )
                for prior in range(index)
            )
            diagnostic = step.get("visibility_diagnostic")
            hidden_ancestors = (
                diagnostic.get("hidden_ancestors")
                if isinstance(diagnostic, dict)
                else []
            ) or []
            hidden_ancestor = (
                hidden_ancestors[0] if isinstance(hidden_ancestors[0], dict) else {}
            ) if hidden_ancestors else {}
            ancestor_label = str(hidden_ancestor.get("label") or "")
            ancestor_reasons = ", ".join(
                str(item) for item in hidden_ancestor.get("hidden_reasons") or []
            )
            base_selector = (
                str(diagnostic.get("base_selector") or "")
                if isinstance(diagnostic, dict)
                and int(diagnostic.get("base_matched_count") or 0) > 0
                else ""
            )
            if (
                kind in {"assert_visible", "wait_for"}
                and target
                and reload_before_failure
                and target_worked_before_reload
            ):
                directives.append(
                    f"- HARD: {target} was visible before reload and disappeared only after "
                    "reload. Preserve the existing target markup. Load its persisted state in "
                    "the accepted startup/load path and render that existing target when its "
                    "current route is shown; do not add a duplicate target or replace the router."
                )
            elif kind in {"assert_visible", "wait_for"} and target and base_selector:
                directives.append(
                    f"- HARD: {base_selector} exists, but it never entered the required "
                    f"state expressed by {target}. Repair the event/hash state transition in "
                    "behavior code; do not add or duplicate the existing DOM item."
                )
                if preceding_hash and "data-name" in base_selector:
                    directives.append(
                        f"- HARD: after setting hash {preceding_hash}, resolve its slug against "
                        f"the existing item address {base_selector}. Attribute selector values "
                        "are case-sensitive: compare a normalized slug derived from each item's "
                        "data-name instead of querying data-name with lowercase hash words."
                    )
            elif kind in {"assert_visible", "wait_for"} and target and ancestor_label:
                directives.append(
                    f"- HARD: {target} exists, but its ancestor {ancestor_label} is hidden"
                    f" ({ancestor_reasons or 'computed hidden state'}). Keep that ancestor visible "
                    "during the target state, or move the target outside it; changing only the "
                    "target's own display cannot make a descendant of a hidden ancestor visible."
                )
            if (
                kind in {"assert_visible", "wait_for"}
                and target
                and reload_before_failure
                and target_worked_before_reload
            ):
                pass
            elif kind in {"assert_visible", "wait_for"} and target and base_selector:
                pass
            elif kind == "assert_hidden" and target and preceding_control:
                directives.append(
                    f"- HARD: clicking {preceding_control} left {target} visible. "
                    f"Fix {preceding_control}'s event handler so it hides {target}. "
                    f"Do not only change {target}'s initial style."
                )
            elif kind == "assert_visible" and target and preceding_control:
                directives.append(
                    f"- HARD: clicking {preceding_control} left {target} hidden. "
                    f"Fix {preceding_control}'s event handler so it shows {target}."
                )
            elif kind == "wait_for" and target and preceding_control:
                directives.append(
                    f"- HARD: after clicking {preceding_control}, {target} never became "
                    "visible. Repair that navigation/state transition before changing "
                    "the downstream detail behavior."
                )
            elif kind == "wait_for" and target:
                directives.append(
                    f"- HARD: required target {target} never became visible. If it is an "
                    "existing intended item, add that exact stable selector to the item; "
                    "do not create a duplicate UI surface just to satisfy the check."
                )
            elif kind == "click" and target:
                directives.append(
                    f"- HARD: {target} was not actionable at its required click step; "
                    "keep it visible, enabled, and outside any element hidden earlier in the check."
                )
            elif kind == "assert_scroll":
                observed = step.get("output") if isinstance(step.get("output"), dict) else {}
                actual = observed.get("actual")
                expected = observed.get("expected")
                directives.append(
                    f"- HARD: restored window.scrollY was {actual!r}, expected {expected!r}. "
                    "Preserve the exact click-time gallery scroll and restore it only after "
                    "the prior filter/layout is visible and stable. Do not change unrelated layout."
                )
            elif kind == "assert_value" and target:
                observed = step.get("output") if isinstance(step.get("output"), dict) else {}
                actual = observed.get("actual")
                expected = observed.get("expected")
                directives.append(
                    f"- HARD: {target} had value {actual!r}, expected {expected!r}. "
                    "If this follows reload or route navigation, encode the selected state in "
                    "the addressable route or accepted persistence mechanism and restore both "
                    "application state and the control value; do not only set the control before reload."
                )
    return "\n".join(dict.fromkeys(directives)) or "(no additional derived directive)"


def _recent_repair_runtime_errors(
    file_comm: FileComm,
    *,
    before_round: int,
    lookback_rounds: int = 8,
    max_errors: int = 8,
) -> list[dict[str, Any]]:
    """Keep the small causal runtime signal across unsuccessful repair attempts.

    A later candidate can mask a ReferenceError with a guard, causing the next
    browser packet to lose the only useful diagnosis while the user-visible
    failure remains unchanged.  Carry only deduplicated console errors and
    their source round, never whole historic grades or source code.
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    lower = max(0, before_round - max(1, lookback_rounds))
    for source_round in range(before_round, lower, -1):
        path = file_comm.dir / f"repair_packet_round_{source_round}.json"
        if not path.is_file():
            continue
        try:
            packet = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        for failure in packet.get("failed_checks") or []:
            if not isinstance(failure, dict):
                continue
            for error in failure.get("console_errors") or []:
                message = " ".join(str(error or "").split())[:400]
                if not message or message in seen:
                    continue
                seen.add(message)
                found.append({"source_round": source_round, "error": message})
                if len(found) >= max_errors:
                    return found
    return found


def _render_recent_runtime_error_directives(
    recent_errors: list[dict[str, Any]],
) -> str:
    packet = {
        "failed_checks": [
            {"console_errors": [item.get("error") for item in recent_errors]}
        ]
    }
    return _render_failed_action_directives(packet, [])


def _declared_source_functions(edit_context: dict[str, Any] | None) -> list[str]:
    """Return exact function names visible in Harness-selected source windows."""
    if not isinstance(edit_context, dict):
        return []
    names: set[str] = set()
    for item in edit_context.get("source_windows") or []:
        if not isinstance(item, dict):
            continue
        names.update(
            re.findall(
                r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+"
                r"([A-Za-z_$][\w$]*)\s*\(",
                str(item.get("content") or ""),
            )
        )
    return sorted(names)


def _unreferenced_near_duplicate_functions(
    edit_context: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    """Find likely failed-repair helpers shadowing an already wired function.

    A one-occurrence declaration such as ``handleRoute`` next to a referenced
    ``handleRouteChange`` is normally an unwired repair artifact.  Reporting
    this relationship to the model is source-grounded and avoids asking it to
    invent yet another router or renderer.
    """
    if not isinstance(edit_context, dict):
        return []
    source = "\n".join(
        str(item.get("content") or "")
        for item in edit_context.get("source_windows") or []
        if isinstance(item, dict)
    )
    names = _declared_source_functions(edit_context)
    pairs: list[tuple[str, str]] = []
    for shorter in names:
        if not re.match(r"^(?:handle|render|load|show|setup)", shorter):
            continue
        shorter_count = len(re.findall(rf"\b{re.escape(shorter)}\b", source))
        if shorter_count != 1:
            continue
        for longer in names:
            if longer != shorter and longer.startswith(shorter):
                longer_count = len(re.findall(rf"\b{re.escape(longer)}\b", source))
                if longer_count > 1:
                    pairs.append((shorter, longer))
    return sorted(set(pairs))


def _atomic_candidate_summary(payload: dict[str, Any]) -> str:
    """Describe a rejected candidate without echoing its code into the retry."""
    entries: list[str] = []
    total = 0
    for item in payload.get("operations") or []:
        if not isinstance(item, dict):
            continue
        operation = str(item.get("op") or item.get("operation") or "unknown")
        path = _atomic_frontend_path(item.get("path"))
        if operation == "replace_lines":
            replacement_lines = max(
                1, len(str(item.get("replacement") or "").splitlines())
            )
            old_lines = (
                int(item["end_line"]) - int(item["start_line"]) + 1
                if isinstance(item.get("start_line"), int)
                and isinstance(item.get("end_line"), int)
                else 0
            )
            lines = max(old_lines, replacement_lines)
            total += lines
            entries.append(
                f"{path} replace {item.get('start_line')}-{item.get('end_line')} "
                f"({lines} patch lines)"
            )
        elif operation == "insert_after":
            lines = max(1, len(str(item.get("content") or "").splitlines()))
            total += lines
            entries.append(
                f"{path} insert after {item.get('after_line')} ({lines} patch lines)"
            )
        elif operation == "copy_from":
            entries.append(
                f"{path} copy_from {_atomic_frontend_path(item.get('source'))}"
            )
    for item in payload.get("patches") or []:
        if not isinstance(item, dict):
            continue
        path = _atomic_frontend_path(item.get("path"))
        lines = max(
            len(str(item.get("old_text", item.get("search", ""))).splitlines()) or 1,
            len(str(item.get("new_text", item.get("replace", ""))).splitlines()) or 1,
        )
        total += lines
        entries.append(f"{path} legacy exact patch ({lines} patch lines)")
    return "; ".join(entries[:20]) + f". Estimated total: {total} patch lines."


def _render_inspiration_code(harness_dir: Path) -> str:
    path = harness_dir / "inspiration_code.json"
    if not path.is_file():
        return ""
    references = json.loads(path.read_text())
    if not references.get("snippets"):
        return ""
    return ("\n\n## Optional inspiration reference code\n"
            "These read-only snippets come from the inspirations selected for this Edit. "
            "Adapt useful behavior to the current source and requested feature. They are not host files, "
            "patch targets, extra requirements or proof of working behavior; do not copy unrelated code.\n"
            + json.dumps(references["snippets"], ensure_ascii=False))


def _atomic_executor_eligible(
    *, config: HarnessConfig, workdir: Path, round_num: int
) -> bool:
    if not _native_openai_runtime(config):
        return False
    context = read_edit_context(workdir / ".harness", round_num)
    plan_path = workdir / ".harness" / plan_name(round_num)
    if not context or not plan_path.is_file():
        return False
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    cone = plan.get("source_change_cone") or {}
    selected = {
        str(item.get("path"))
        for item in context.get("source_windows") or []
        if isinstance(item, dict) and item.get("path")
    }
    existing_candidates = {
        str(path)
        for path in cone.get("local_paths") or []
        if (workdir / str(path)).is_file()
    }
    planned_new = {str(path) for path in cone.get("planned_new_paths") or []}
    supplied = (workdir / ".harness/harness_state.json")
    supplied = supplied.is_file() and json.loads(supplied.read_text()).get("supplied_atomic_plan")
    if len(existing_candidates | planned_new) > 6 and not supplied:
        return False
    initial = {
        str(path)
        for path in cone.get("initial_paths") or []
        if (workdir / str(path)).is_file()
    }
    omitted = existing_candidates - selected
    requested_roles = set(
        (plan.get("target_contract") or {}).get("requested_source_roles") or []
    )
    omitted_only_unrequested_style = (
        "style" not in requested_roles
        and all(Path(path).suffix.lower() in {".css", ".scss", ".wxss"} for path in omitted)
    )
    if not (initial <= selected and (not omitted or omitted_only_unrequested_style)):
        return False
    # The compact executor has no tools. Partial files require the existing
    # tool-enabled path so the model can inspect missing functions/dependencies.
    for relative in selected & existing_candidates:
        source = (workdir / relative).read_text(encoding="utf-8")
        windows = [item for item in context.get("source_windows") or [] if item.get("path") == relative]
        if any(item.get("content") == source for item in windows):
            continue
        covered = set()
        for item in windows:
            start, end = item.get("start_line"), item.get("end_line")
            if isinstance(start, int) and isinstance(end, int):
                covered.update(range(start, end + 1))
        if not set(range(1, len(source.splitlines()) + 1)) <= covered:
            return False
    return True


async def _run_atomic_patch_executor(
    *,
    config: HarnessConfig,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
    mode: GeneratorMode,
    prompt: str,
    baseline_commit: str,
    mutation_policy: MinimalPathPolicy,
    semantic_attempt: int = 1,
    previous_candidate: str = "",
    correction_feedback: str = "",
    candidate_override: str = "",
) -> AgentRunStats:
    """One model request proposes exact patches; the Harness applies them transactionally."""
    trace_path = RoundArtifacts(file_comm, round_num).trace_path("generator")
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    supplied_plan = bool((file_comm.read_state() or {}).get("supplied_atomic_plan"))
    system_prompt = (
        (REPAIR_SYSTEM_PROMPT if mode == "repair" else EDIT_SYSTEM_PROMPT)
        + "\nReturn JSON only. Prefer the compact protocol: "
        "{\"operations\":[{\"op\":\"replace_lines\",\"path\":...,\"file_sha256\":...,"
        "\"start_line\":...,\"end_line\":...,\"replacement\":...},"
        "{\"op\":\"insert_after\",\"path\":...,\"file_sha256\":...,\"after_line\":...,"
        "\"content\":...}]}. All line numbers refer to the supplied immutable file SHA and "
        "all operations for one file are applied together. For a Harness-planned new page, use "
        "{\"op\":\"copy_from\",\"source\":...,\"path\":...,\"source_sha256\":...,"
        "\"line_edits\":[...]}; omit path/file_sha256 inside its nested line_edits. "
        "Do not echo old/search text. The Allowed source cone gives a hard total patch-line "
        "budget: count the larger of removed/replacement lines for every operation and keep "
        "their sum within it. Never replace an entire file or repeat unchanged functions. "
        "For additive JavaScript, prefer insert_after plus tiny wiring replacements at stable "
        "boundaries. Legacy patches/new_files remain accepted only when the compact protocol "
        "cannot express the change. Do not describe commands."
    )
    if supplied_plan:
        system_prompt += (
            " Product Session policy overrides minimal-path guidance: there is no patch-line "
            "or touched-file minimality budget. Implement this one requested capability "
            "completely in the target source; preserve unrelated behavior. "
            "Omit file_sha256/source_sha256 from your operations: Harness binds the exact "
            "source revisions itself. Use the supplied immutable line numbers and paths. "
            "For a localized change inside a long/minified line, prefer the existing exact "
            "text protocol {\"patches\":[{\"path\":...,\"old_text\":...,\"new_text\":...}]} "
            "instead of reproducing the entire line or function. This overrides the compact "
            "protocol preference and the no-search-text instruction above. Each old_text must "
            "occur exactly once; use enough unchanged context to make it unique. Do not mix "
            "patches and line operations for the same file. Preserve untouched function braces, "
            "callback closures and regex literals exactly. When changing form markup, preserve "
            "existing fields referenced by its handlers and wire new fields to actual elements."
        )
    if supplied_plan and mode == "repair":
        from src.orchestration.webcompass_protocol import REPAIR_TYPE_DEFINITIONS
        system_prompt += (
            " In the same JSON response as the code operations, also include "
            "repair_task_descriptions:[{task_type,description,evidence_ids:[failed check IDs], "
            "issue_locations:[{element_path,kind}]}]. "
            "Use one entry per independently fixable defect, not one per defect category: two "
            "different controls missing names are two issues; the same issue seen in several "
            "states is one. Group symptoms fixed by one shared root-cause change together. "
            "For deterministic risk findings copy exact element_path and kind into issue_locations. "
            "Describe only the defect already reproduced in the supplied browser evidence, "
            "not extra defects. Choose the exact applicable type from the following definitions. "
            "Return [] if none fits; never invent a category. This is repair metadata, not an "
            "additional evaluation or a request to run more checks. "
            + json.dumps(REPAIR_TYPE_DEFINITIONS, ensure_ascii=False)
        )
    system_prompt += (
        " Serialize code strings exactly once as JSON: after JSON decoding, content must be "
        "literal source code with real line breaks and ordinary quotes, not backslash-n "
        "or backslash-quote sequences outside JavaScript string literals."
    )
    content: str | list[dict[str, Any]] = prompt
    images = task_input_image_paths(workdir)
    if images:
        content = openai_user_content(prompt, images)
    client = OpenAIHTTPClient(config, config.agent_request_timeout_seconds)
    started = time.monotonic()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]
    if previous_candidate:
        messages.extend(
            [
                {"role": "assistant", "content": previous_candidate},
                {
                    "role": "user",
                    "content": (
                        "The candidate above was fully rolled back. Correct that same "
                        "candidate using the exact local evidence below. Return compact "
                        "JSON only; preserve complete syntax boundaries, do not broaden "
                        "files, and do not rewrite a whole file.\n\n"
                        + correction_feedback
                    ),
                },
            ]
        )
    elif correction_feedback:
        messages.append(
            {
                "role": "user",
                "content": (
                    "A trace-backed candidate was fully rolled back. Use this exact local "
                    "rejection as a hard constraint, but produce a fresh minimal candidate "
                    "instead of copying the rejected shape:\n\n" + correction_feedback
                ),
            }
        )
    source_revisions = None
    if supplied_plan:
        context = read_edit_context(file_comm.dir, round_num) or {}
        source_revisions = {str(item["path"]): str(item["file_sha256"])
                            for item in context.get("source_windows", [])}
    if candidate_override:
        raw = candidate_override
        usage: dict[str, Any] = {}
    else:
        response = await client.complete(
            model=config.generator_model,
            messages=messages,
            temperature=0,
            max_tokens=12000 if (file_comm.read_state() or {}).get("supplied_atomic_plan") else 4096,
            **({"_protocol": "responses"} if supplied_plan and
               int(os.environ.get("PRODUCT_SESSION_RECOVERY_ATTEMPT", "1")) >= 3 else {}),
        )
        message = (response.get("choices") or [{}])[0].get("message") or {}
        raw = str(message.get("content") or "")
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    frontend = workdir / "frontend"
    originals: dict[Path, str] = {}
    created_paths: set[Path] = set()
    changed_paths: list[str] = []
    policy_snapshot = {
        "observed_paths": set(mutation_policy.observed_paths),
        "tool_observed_paths": set(mutation_policy.tool_observed_paths),
        "touched_paths": set(mutation_policy.touched_paths),
        "mutation_revision": mutation_policy.mutation_revision,
        "validation_attempt_revision": mutation_policy.validation_attempt_revision,
        "validation_success_revision": mutation_policy.validation_success_revision,
        "validation_last_ok": mutation_policy.validation_last_ok,
    }
    ledger_before = (
        mutation_policy.ledger_path.read_bytes()
        if mutation_policy.ledger_path.is_file()
        else None
    )
    with trace_path.open("a", encoding="utf-8") as trace:
        trace.write(json.dumps({
            "event": "run_start",
            "model": config.generator_model,
            "phase": f"atomic_edit_executor_attempt_{semantic_attempt}",
            "prompt": prompt,
            "correction_feedback": correction_feedback,
            "candidate_source": "trace_replay" if candidate_override else "model",
            "image_paths": [str(path) for path in images],
        }, ensure_ascii=False) + "\n")
        trace.write(json.dumps({
            "event": "assistant_response",
            "content": raw,
        }, ensure_ascii=False) + "\n")
        trace.write(json.dumps({
            "event": "usage",
            "request_usage": usage,
            "attempt_usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "cumulative_usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "estimated_cost_usd": estimate_cost_usd(config.generator_model, usage),
        }, ensure_ascii=False) + "\n")
        trace.flush()
        payload = extract_json_object(raw)
        compact_operations = _normalize_atomic_operations(payload, source_revisions)
        patches = _normalize_atomic_patch_response(payload) if payload.get("patches") or any(
            isinstance(item, dict) and (item.get("op") or item.get("operation")) == "patches"
            for item in payload.get("operations") or []
        ) else []
        new_files = _normalize_atomic_new_files(payload)
        if not compact_operations and not patches and not new_files:
            raise ValueError("atomic Edit executor returned no exact patches or planned new files")
        minimal_plan = json.loads(
            (workdir / ".harness" / plan_name(round_num)).read_text(encoding="utf-8")
        )
        planned_new_paths = set(
            (minimal_plan.get("source_change_cone") or {}).get("planned_new_paths") or []
        )
        context = read_edit_context(workdir / ".harness", round_num) or {}
        visible_source_paths = {
            str(item.get("path"))
            for item in context.get("source_windows") or []
            if isinstance(item, dict) and item.get("path")
        }
        try:
            patch_line_count = 0
            for item in compact_operations:
                if item["op"] == "replace_lines":
                    old_lines = int(item["end_line"]) - int(item["start_line"]) + 1
                    new_lines = len(str(item.get("replacement") or "").splitlines()) or 1
                    patch_line_count += max(old_lines, new_lines)
                elif item["op"] == "insert_after":
                    patch_line_count += len(str(item.get("content") or "").splitlines()) or 1
                elif item["op"] == "copy_from":
                    for nested in item.get("line_edits") or []:
                        if nested.get("op") == "replace_lines":
                            old_lines = int(nested["end_line"]) - int(nested["start_line"]) + 1
                            new_lines = len(str(nested.get("replacement") or "").splitlines()) or 1
                            patch_line_count += max(old_lines, new_lines)
                        elif nested.get("op") == "insert_after":
                            patch_line_count += len(str(nested.get("content") or "").splitlines()) or 1
            patch_line_count += sum(
                max(
                    len(str(item.get("old_text") or "").splitlines()) or 1,
                    len(str(item.get("new_text") or "").splitlines()) or 1,
                )
                for item in patches
            )
            patch_line_count += sum(
                len(str(item.get("content") or "").splitlines()) or 1
                for item in new_files
            )
            max_patch_lines = int((minimal_plan.get("budgets") or {}).get("max_patch_lines", 120))
            effective_patch_lines = 0
            direct_line_operations: dict[str, list[dict[str, Any]]] = {}
            copy_operations: list[dict[str, Any]] = []
            for item in compact_operations:
                if item["op"] == "copy_from":
                    copy_operations.append(item)
                else:
                    direct_line_operations.setdefault(str(item["path"]), []).append(item)
            legacy_paths = {str(item["path"]) for item in patches}
            if legacy_paths & set(direct_line_operations):
                raise ValueError("cannot mix compact and legacy patches for one source file")
            write_paths = {str(item["path"]) for item in new_files}
            copy_paths = {str(item["path"]) for item in copy_operations}
            if write_paths & copy_paths:
                raise ValueError("cannot mix copy_from and new_files for one destination")

            transactions: list[tuple[str, str, Any]] = [
                ("line_edits", path, items)
                for path, items in direct_line_operations.items()
            ] + [
                ("apply_patch", item["path"], item) for item in patches
            ] + [
                ("copy_from", item["path"], item) for item in copy_operations
            ] + [
                ("write_file", item["path"], item) for item in new_files
            ]
            transactions.sort(
                key=lambda item: _atomic_transaction_sort_key(
                    item, mutation_policy.initial_paths
                )
            )
            for operation, relative, item in transactions:
                target = (workdir / relative).resolve()
                try:
                    target.relative_to(workdir.resolve())
                except ValueError as exc:
                    raise ValueError(f"atomic patch escapes workdir: {relative}") from exc
                policy_inputs: list[tuple[str, dict[str, Any]]] = []
                if operation == "line_edits":
                    if not target.is_file():
                        raise ValueError(f"atomic line edit targets missing source: {relative}")
                    expected_hashes = {str(entry.get("file_sha256") or "") for entry in item}
                    if len(expected_hashes) != 1:
                        raise ValueError(f"atomic line edits disagree on source SHA: {relative}")
                    current = target.read_text(encoding="utf-8")
                    updated, exact_inputs = _apply_sha_line_operations(
                        current,
                        item,
                        expected_path=relative,
                        expected_sha256=next(iter(expected_hashes)),
                        preserve_operations=supplied_plan,
                    )
                    policy_inputs = [("apply_patch", entry) for entry in exact_inputs]
                elif operation == "apply_patch":
                    if not target.is_file():
                        raise ValueError(f"atomic patch targets missing source: {relative}")
                    current = target.read_text(encoding="utf-8")
                    if not item["old_text"] or current.count(item["old_text"]) != 1:
                        raise ValueError(f"atomic patch is not unique in {relative}")
                    updated = current.replace(item["old_text"], item["new_text"], 1)
                    policy_inputs = [("apply_patch", {
                        "path": relative,
                        "old_text": item["old_text"],
                        "new_text": item["new_text"],
                    })]
                elif operation == "copy_from":
                    source_relative = str(item.get("source") or "")
                    source = (workdir / source_relative).resolve()
                    try:
                        source.relative_to(workdir.resolve())
                    except ValueError as exc:
                        raise ValueError(f"atomic copy source escapes workdir: {source_relative}") from exc
                    if source_relative not in visible_source_paths or not source.is_file():
                        raise ValueError(f"atomic copy source was not preloaded: {source_relative}")
                    if not _atomic_new_path_allowed(config, relative, planned_new_paths):
                        raise ValueError(f"atomic new file was not planned: {relative}")
                    if target.exists():
                        raise ValueError(f"atomic new file already exists: {relative}")
                    source_content = source.read_text(encoding="utf-8")
                    nested = list(item.get("line_edits") or [])
                    for nested_item in nested:
                        nested_item["path"] = relative
                    if nested:
                        updated, copy_pairs = _apply_sha_line_operations(
                            source_content,
                            nested,
                            expected_path=relative,
                            expected_sha256=str(item.get("source_sha256") or ""),
                            preserve_operations=supplied_plan,
                        )
                    else:
                        source_sha = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
                        if source_sha != str(item.get("source_sha256") or ""):
                            raise ValueError(f"atomic copy source SHA mismatch: {source_relative}")
                        updated, copy_pairs = source_content, []
                    policy_inputs = [("write_file", {
                        "path": relative,
                        "content": updated,
                        "_harness_copy_from": source_relative,
                        "_harness_copy_pairs": copy_pairs,
                    })]
                else:
                    if not _atomic_new_path_allowed(config, relative, planned_new_paths):
                        raise ValueError(f"atomic new file was not planned: {relative}")
                    if target.exists():
                        raise ValueError(f"atomic new file already exists: {relative}")
                    updated = str(item.get("content") or "")
                    if not updated:
                        raise ValueError(f"atomic new file is empty: {relative}")
                    policy_inputs = [("write_file", {"path": relative, "content": updated})]
                if target.exists() and updated == current:
                    raise ValueError(
                        f"atomic patch produces no source change in {relative}; patch the "
                        "still-failing behavior shown in the repair evidence"
                    )
                effective_patch_lines += max(
                    1, effective_patch_line_count(current if target.exists() else "", updated)
                )
                if not supplied_plan and effective_patch_lines > max_patch_lines:
                    raise ValueError(
                        f"Atomic candidate changes {effective_patch_lines} effective patch lines, "
                        f"exceeding the hard total budget of {max_patch_lines}; preserve unchanged "
                        "context and narrow the semantic edit."
                    )
                if supplied_plan:
                    if not relative.startswith("frontend/"):
                        raise ValueError(f"patch must target frontend source: {relative}")
                    if relative in mutation_policy.off_target_paths:
                        raise ValueError(f"patch targets a non-target page: {relative}")
                else:
                    for policy_operation, tool_input in policy_inputs:
                        denial = mutation_policy.check(policy_operation, tool_input)
                        if denial:
                            raise ValueError(denial)
                if target.exists():
                    originals.setdefault(target, current)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    created_paths.add(target)
                target.write_text(updated, encoding="utf-8")
                for policy_operation, tool_input in policy_inputs:
                    mutation_policy.observe_result(
                        policy_operation, tool_input, ok=True, output="applied"
                    )
                changed_paths.append(relative.removeprefix("frontend/"))
                trace.write(json.dumps({
                    "event": "tool",
                    "name": operation,
                    "ok": True,
                    "path": relative,
                    "compact_operation_count": len(item) if operation == "line_edits" else 1,
                }, ensure_ascii=False) + "\n")
                trace.flush()
                if supplied_plan:
                    continue  # The supplied browser check owns functional acceptance.
                diff_check = subprocess.run(
                    ["git", "diff", "--check"],
                    cwd=frontend,
                    text=True,
                    capture_output=True,
                )
                validation_ok = diff_check.returncode == 0
                validation_output = diff_check.stdout + diff_check.stderr
                if validation_ok and Path(relative).suffix.lower() in {".js", ".mjs", ".cjs"}:
                    validation_ok, syntax_output = _validate_javascript_syntax(
                        frontend, relative.removeprefix("frontend/")
                    )
                    validation_output += syntax_output
                mutation_policy.observe_validation(
                    ok=validation_ok,
                    output=validation_output,
                    tool="harness atomic mutation validation",
                )
                if not validation_ok:
                    raise RuntimeError(validation_output)
            subprocess.run(
                ["git", "add", "--", *sorted(set(changed_paths))],
                cwd=frontend,
                check=True,
                text=True,
                capture_output=True,
            )
            prefix = "fix" if mode == "repair" else "feat"
            commit = subprocess.run(
                [
                    "git", "-c", "commit.gpgsign=false", "commit", "-m",
                    f"{prefix}(atomic-edit): apply scoped frontend transition",
                ],
                cwd=frontend,
                check=True,
                text=True,
                capture_output=True,
            )
            trace.write(json.dumps({
                "event": "tool",
                "name": "run_command",
                "ok": True,
                "output": commit.stdout.strip(),
            }, ensure_ascii=False) + "\n")
        except Exception as exc:
            for target, original in originals.items():
                target.write_text(original, encoding="utf-8")
            for target in created_paths:
                target.unlink(missing_ok=True)
            if changed_paths:
                subprocess.run(
                    ["git", "reset", "--quiet", "HEAD", "--", *sorted(set(changed_paths))],
                    cwd=frontend,
                    text=True,
                    capture_output=True,
                )
            # The filesystem edit is transactional, so its policy evidence must
            # be transactional too.  Otherwise a rejected partial candidate can
            # unlock dependency files that were never actually changed.
            mutation_policy.observed_paths = set(policy_snapshot["observed_paths"])
            mutation_policy.tool_observed_paths = set(
                policy_snapshot["tool_observed_paths"]
            )
            mutation_policy.touched_paths = set(policy_snapshot["touched_paths"])
            mutation_policy.mutation_revision = int(
                policy_snapshot["mutation_revision"]
            )
            mutation_policy.validation_attempt_revision = int(
                policy_snapshot["validation_attempt_revision"]
            )
            mutation_policy.validation_success_revision = int(
                policy_snapshot["validation_success_revision"]
            )
            mutation_policy.validation_last_ok = policy_snapshot["validation_last_ok"]
            mutation_policy._persist_state()
            if ledger_before is None:
                mutation_policy.ledger_path.unlink(missing_ok=True)
            else:
                mutation_policy.ledger_path.write_bytes(ledger_before)
            trace.write(json.dumps({
                "event": "run_error",
                "error": f"{type(exc).__name__}: {exc}",
                "attempt_usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
                "cumulative_usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
                "estimated_cost_usd": estimate_cost_usd(
                    config.generator_model, usage
                ),
                "fallback": (
                    "compact_semantic_correction"
                    if semantic_attempt < _MAX_ATOMIC_SEMANTIC_ATTEMPTS
                    else "stop_after_compact_correction"
                ),
            }, ensure_ascii=False) + "\n")
            trace.flush()
            if candidate_override:
                raise AtomicCandidateRejected(str(exc)) from exc
            if semantic_attempt < _MAX_ATOMIC_SEMANTIC_ATTEMPTS:
                logger.warning(
                    "[bold yellow]Atomic candidate rejected[/]; requesting a "
                    f"compact semantic correction: {exc}"
                )
                trace.close()
                retry_policy = MinimalPathPolicy.load(workdir, round_num)
                retry = await _run_atomic_patch_executor(
                    config=config,
                    file_comm=file_comm,
                    workdir=workdir,
                    round_num=round_num,
                    mode=mode,
                    prompt=prompt,
                    baseline_commit=baseline_commit,
                    mutation_policy=retry_policy or mutation_policy,
                    semantic_attempt=semantic_attempt + 1,
                    previous_candidate=raw,
                    correction_feedback=(
                        "Hard patch budget and rejected candidate shape:\n"
                        + _atomic_candidate_summary(payload)
                        + "\n\nExact local rejection:\n"
                        + _atomic_rejection_feedback(exc)
                    ),
                )
                first_cost = estimate_cost_usd(config.generator_model, usage)
                return AgentRunStats(
                    cost_usd=round(first_cost + retry.cost_usd, 6),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    duration_api_ms=None,
                    token_usage={
                        "input_tokens": input_tokens
                        + int(retry.token_usage.get("input_tokens") or 0),
                        "output_tokens": output_tokens
                        + int(retry.token_usage.get("output_tokens") or 0),
                    },
                    usage={
                        "semantic_attempts": 1
                        + int(retry.usage.get("semantic_attempts") or 1),
                        "first_attempt": usage,
                        "correction_attempt": retry.usage,
                    },
                    model_usage={},
                )
            raise AtomicCandidateRejected(str(exc)) from exc
        trace.flush()
    if supplied_plan and mode == "repair":
        (file_comm.dir / f"repair_metadata_round_{round_num}.json").write_text(
            json.dumps({"source": "same_repair_model_response", "round": round_num,
                        "repair_task_descriptions": payload.get("repair_task_descriptions", [])},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    file_comm.write_build_log(
        f"# Atomic {mode.title()}\n\nRound: {round_num}\nCompact operations: "
        f"{len(compact_operations)}\nPatches: {len(patches)}\n"
        f"Planned new files: {len(new_files)}\n"
    )
    file_comm.append_progress_entry(
        f"## Round {round_num}\n\nApplied {len(compact_operations)} compact operations, "
        f"{len(patches)} exact patches, and {len(new_files)} planned new files in one bounded request."
    )
    return AgentRunStats(
        cost_usd=estimate_cost_usd(config.generator_model, usage),
        duration_ms=int((time.monotonic() - started) * 1000),
        duration_api_ms=None,
        token_usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
        usage=usage,
        model_usage={},
    )


class _ExternalRuntimeResourceParser(HTMLParser):
    """Collect remote URLs that a page fetches, excluding ordinary anchors."""

    _RESOURCE_ATTRIBUTES = {
        "audio": ("src",),
        "embed": ("src",),
        "iframe": ("src",),
        "img": ("src", "srcset"),
        "input": ("src",),
        "object": ("data",),
        "script": ("src",),
        "source": ("src", "srcset"),
        "track": ("src",),
        "video": ("src", "poster"),
    }

    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        candidates = [
            values.get(name, "")
            for name in self._RESOURCE_ATTRIBUTES.get(tag.lower(), ())
        ]
        if tag.lower() == "link":
            rel = {item.lower() for item in values.get("rel", "").split()}
            if rel & {
                "dns-prefetch",
                "icon",
                "modulepreload",
                "preconnect",
                "prefetch",
                "preload",
                "stylesheet",
            }:
                candidates.append(values.get("href", ""))
        for candidate in candidates:
            self.urls.extend(match.group(0) for match in _REMOTE_URL_RE.finditer(candidate))


def _external_runtime_urls(relative: str, content: str) -> list[tuple[str, str]]:
    suffix = Path(relative).suffix.lower()
    urls: list[str] = []
    if suffix in {".htm", ".html"}:
        parser = _ExternalRuntimeResourceParser()
        parser.feed(content)
        urls.extend(parser.urls)
    elif suffix == ".css":
        for match in re.finditer(
            r"(?:@import\s+(?:url\()?|url\()\s*['\"]?(https?://[^\s'\"\)]+)",
            content,
            re.IGNORECASE,
        ):
            urls.append(match.group(1))
    else:
        for match in re.finditer(
            r"\b(?:fetch|WebSocket|EventSource)\s*\(\s*['\"](https?://[^'\"]+)",
            content,
            re.IGNORECASE,
        ):
            urls.append(match.group(1))
    return [(relative, url) for url in urls]


def _working_tree_external_runtime_dependencies(
    frontend_dir: Path,
) -> set[tuple[str, str]]:
    violations: set[tuple[str, str]] = set()
    ignored_parts = {".git", "build", "dist", "node_modules"}
    for path in sorted(frontend_dir.rglob("*")):
        if (
            not path.is_file()
            or set(path.relative_to(frontend_dir).parts) & ignored_parts
            or path.stat().st_size > 1_000_000
        ):
            continue
        suffix = path.suffix.lower()
        if suffix not in {".css", ".htm", ".html", ".js", ".jsx", ".mjs", ".ts", ".tsx"}:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = path.relative_to(frontend_dir).as_posix()
        violations.update(_external_runtime_urls(relative, content))
    return violations


def _commit_external_runtime_dependencies(
    frontend_dir: Path, commit: str
) -> set[tuple[str, str]]:
    violations: set[tuple[str, str]] = set()
    paths = _git_output(frontend_dir, "ls-tree", "-r", "--name-only", commit).splitlines()
    for relative in paths:
        if Path(relative).suffix.lower() not in {
            ".css", ".htm", ".html", ".js", ".jsx", ".mjs", ".ts", ".tsx",
        }:
            continue
        content = _git_output(frontend_dir, "show", f"{commit}:{relative}")
        violations.update(_external_runtime_urls(relative, content))
    return violations


def _validate_no_external_runtime_dependencies(
    frontend_dir: Path, *, baseline_commit: str | None = None
) -> str | None:
    """Reject newly introduced runtime URLs while grandfathering accepted source."""
    violations = _working_tree_external_runtime_dependencies(frontend_dir)
    if baseline_commit is not None:
        try:
            violations -= _commit_external_runtime_dependencies(
                frontend_dir, baseline_commit
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            return f"Could not audit baseline runtime dependencies: {exc}"
    if not violations:
        return None
    evidence = ", ".join(
        f"frontend/{path} -> {url}" for path, url in sorted(violations)[:8]
    )
    return (
        "External runtime dependencies are forbidden for portable harness data: "
        + evidence
        + ". Vendor the asset locally or use system fonts/local source, then validate and commit."
    )


def _git_output(frontend_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=frontend_dir, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


async def _ensure_generator_baseline(frontend_dir: Path) -> str:
    frontend_dir.mkdir(parents=True, exist_ok=True)
    await ensure_repo(frontend_dir)
    try:
        return _git_output(frontend_dir, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=frontend_dir, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        subprocess.run(
            [
                "git", "-c", "commit.gpgsign=false", "commit", "--allow-empty",
                "-m", "chore: baseline",
            ],
            cwd=frontend_dir, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return _git_output(frontend_dir, "rev-parse", "HEAD")


def _validate_generator_commits(
    frontend_dir: Path, baseline_commit: str, mode: GeneratorMode
) -> str | None:
    expected = "feat" if mode == "generate" else "fix"
    try:
        subjects = _git_output(
            frontend_dir, "log", "--format=%s", f"{baseline_commit}..HEAD"
        ).splitlines()
        status = _git_output(frontend_dir, "status", "--porcelain")
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"Git validation failed: {exc}. Initialize and use the existing frontend Git repository."
    if not any(
        subject.lower().startswith(expected + ":")
        or subject.lower().startswith(expected + "(")
        for subject in subjects
    ):
        return (
            f"No `{expected}` commit was created during this run. Validate the work, then create "
            f"an atomic `{expected}(scope): description` commit before stopping."
        )
    if status:
        return "The frontend Git worktree is not clean. Commit the remaining intended changes before stopping."
    return None


def _trace_confirms_commit(trace_path: Path, commit_hash: str, subject: str) -> bool:
    """Return true only for a native-agent trace that recorded this Git commit.

    A process can die after Git has atomically committed the model's work but
    before the build checkpoint is written.  On resume, treating HEAD as a new
    baseline makes the model create needless changes just to satisfy the commit
    hook.  The trace is the required provenance: a pre-existing commit alone
    is never sufficient to enable recovery.
    """
    if not trace_path.is_file() or not commit_hash or not subject:
        return False
    short_hash = commit_hash[:7]
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") != "tool" or event.get("name") != "run_command":
                continue
            output = str(event.get("output", ""))
            if short_hash in output and subject in output:
                return True
    except (OSError, ValueError, TypeError):
        return False
    return False


def _last_replayable_atomic_candidate(trace_path: Path) -> str:
    """Return the last locally rejected candidate for zero-cost resume replay."""
    if not trace_path.is_file():
        return ""
    candidate = ""
    replayable = ""
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "assistant_response":
                candidate = str(event.get("content") or "")
                replayable = ""
            elif event.get("event") == "usage" and candidate:
                # A complete response can fail normalization before any mutation.
                replayable = candidate
            elif event.get("event") == "run_error" and candidate:
                replayable = candidate
            elif event.get("event") == "tool":
                replayable = ""
    except (OSError, ValueError, TypeError):
        return ""
    return replayable


def _recover_interrupted_commit(
    frontend_dir: Path, file_comm: FileComm, round_num: int, mode: GeneratorMode
) -> tuple[str, str] | None:
    """Find a checkpoint-missing model commit and its parent baseline."""
    try:
        head = _git_output(frontend_dir, "rev-parse", "HEAD")
        subject = _git_output(frontend_dir, "log", "-1", "--format=%s")
        parent = _git_output(frontend_dir, "rev-parse", "HEAD^")
    except (OSError, subprocess.CalledProcessError):
        return None
    expected_prefixes = ("feat:", "feat(") if mode == "generate" else ("fix:", "fix(")
    if not subject.lower().startswith(expected_prefixes):
        return None
    trace_path = RoundArtifacts(file_comm, round_num).trace_path("generator")
    if _trace_confirms_commit(trace_path, head, subject):
        return parent, head
    return None


def _trace_written_frontend_paths(trace_path: Path) -> set[str]:
    """Return frontend paths written successfully by the native model trace.

    This is intentionally narrower than looking for arbitrary tool output: an
    automatic checkpoint may only commit paths for which the trace contains a
    successful ``write_file`` or ``apply_patch`` call with an explicit path.
    """
    if not trace_path.is_file():
        return set()
    pending_paths: list[tuple[str, str]] = []
    written: set[str] = set()
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "assistant":
                calls = ((event.get("message") or {}).get("tool_calls") or [])
                for call in calls:
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict):
                        continue
                    if function.get("name") not in {"write_file", "apply_patch"}:
                        continue
                    raw_args = function.get("arguments", "{}")
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    path = args.get("path") if isinstance(args, dict) else None
                    if isinstance(path, str) and path.startswith("frontend/"):
                        pending_paths.append((str(function.get("name")), path.removeprefix("frontend/")))
            elif (
                event.get("event") == "tool"
                and event.get("name") in {"write_file", "apply_patch"}
                and pending_paths
            ):
                tool_name = str(event.get("name"))
                match_index = next(
                    (index for index, (name, _path) in enumerate(pending_paths) if name == tool_name),
                    None,
                )
                if match_index is not None:
                    _name, path = pending_paths.pop(match_index)
                    if event.get("ok") is True:
                        written.add(path)
            elif (
                event.get("event") == "harness_recovery_patch"
                and event.get("source_author") == "native_model_deferred_tool_call"
            ):
                path = str(event.get("path") or "")
                if path.startswith("frontend/"):
                    written.add(path.removeprefix("frontend/"))
    except (OSError, ValueError, TypeError, AttributeError):
        return set()
    return written


def _trace_has_successful_validation(trace_path: Path) -> bool:
    """Require a model-recorded validation command before auto-checkpointing.

    Source writes plus a valid diff only prove that the model started work.  They
    do not prove that it reached a coherent stopping point.  A successful
    syntax/diff/build validation is the minimum trace signal that makes a
    harness-authored checkpoint honest rather than a commit of a half-built UI.
    """
    if not trace_path.is_file():
        return False
    pending_validation_calls = 0
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "assistant":
                for call in ((event.get("message") or {}).get("tool_calls") or []):
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict) or function.get("name") != "run_command":
                        continue
                    raw_args = function.get("arguments", "{}")
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    command = str((args or {}).get("command", "")) if isinstance(args, dict) else ""
                    if any(marker in command for marker in ("git diff --check", "node --check", "npm run build", "npm test", "pnpm build", "yarn build")):
                        pending_validation_calls += 1
            elif event.get("event") == "tool" and event.get("name") == "run_command" and pending_validation_calls:
                pending_validation_calls -= 1
                if event.get("ok") is True:
                    return True
            elif (
                event.get("event") == "harness_recovery_validation"
                and event.get("ok") is True
                and event.get("source_author")
                == "native_model_deferred_tool_call"
            ):
                return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return False


def _validate_uncommitted_frontend_paths(
    frontend_dir: Path, paths: set[str]
) -> tuple[bool, str]:
    """Validate an existing model diff without changing product source."""

    try:
        diff_check = subprocess.run(
            ["git", "diff", "--check"],
            cwd=frontend_dir,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if diff_check.returncode != 0:
            return False, diff_check.stdout
        for path in sorted(paths):
            target = frontend_dir / path
            if target.suffix.lower() not in {".js", ".mjs", ".cjs"}:
                continue
            syntax_ok, syntax_output = _validate_javascript_syntax(
                frontend_dir, path
            )
            if not syntax_ok:
                return False, syntax_output
    except OSError as exc:
        return False, str(exc)
    return True, ""


def _recover_deferred_model_patches(
    frontend_dir: Path,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
) -> set[str]:
    """Replay exact model patches blocked only by our validation barrier.

    The recovery is deliberately narrow: the model must already have emitted
    an exact ``apply_patch`` call, the corresponding tool result must say that
    only the post-mutation validation barrier blocked it, and all currently
    modified files must be trace-proven model writes. The Harness supplies the
    missing local validation, never new product code.
    """

    trace_path = RoundArtifacts(file_comm, round_num).trace_path("generator")
    mutation_policy = MinimalPathPolicy.load(workdir, round_num)
    if mutation_policy is None or not trace_path.is_file():
        return set()
    try:
        changed_paths = set(
            filter(
                None,
                _git_output(frontend_dir, "diff", "HEAD", "--name-only").splitlines(),
            )
        )
        changed_paths.update(
            filter(
                None,
                _git_output(
                    frontend_dir, "ls-files", "--others", "--exclude-standard"
                ).splitlines(),
            )
        )
    except (OSError, subprocess.CalledProcessError):
        return set()
    if not changed_paths or not changed_paths.issubset(
        _trace_written_frontend_paths(trace_path)
    ):
        return set()
    # The controller normally persisted these writes at tool time. Rehydrate
    # only trace-proven dirty paths so recovery also survives a process exit
    # between the filesystem write and the state-file flush.
    mutation_policy.touched_paths.update(
        f"frontend/{path}" for path in changed_paths
    )
    if changed_paths and mutation_policy.mutation_revision == 0:
        mutation_policy.mutation_revision = 1
    valid, validation_output = _validate_uncommitted_frontend_paths(
        frontend_dir, changed_paths
    )
    if not valid:
        return set()
    mutation_policy.observe_validation(
        ok=True,
        output=validation_output,
        tool="harness_recovery_prevalidation",
    )

    pending: list[tuple[int, str, dict[str, Any]]] = []
    deferred: list[tuple[int, dict[str, Any]]] = []
    try:
        for sequence, line in enumerate(
            trace_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            event = json.loads(line)
            if event.get("event") == "assistant":
                for call in ((event.get("message") or {}).get("tool_calls") or []):
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict):
                        continue
                    name = str(function.get("name") or "")
                    if name != "apply_patch":
                        continue
                    raw_args = function.get("arguments", "{}")
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if isinstance(args, dict):
                        pending.append((sequence, name, args))
            elif event.get("event") == "tool" and pending:
                name = str(event.get("name") or "")
                match = next(
                    (
                        index
                        for index, (_sequence, pending_name, _args) in enumerate(pending)
                        if pending_name == name
                    ),
                    None,
                )
                if match is None:
                    continue
                source_sequence, _pending_name, args = pending.pop(match)
                output = str(event.get("output") or "")
                if (
                    event.get("ok") is False
                    and "A post-mutation validation attempt is required before the harness widens"
                    in output
                ):
                    deferred.append((source_sequence, args))
    except (OSError, ValueError, TypeError, AttributeError):
        return set()

    recovered: set[str] = set()
    recovery_events: list[dict[str, Any]] = []
    for source_sequence, args in deferred:
        relative = str(args.get("path") or "").replace("\\", "/")
        if not relative.startswith("frontend/"):
            continue
        target = (workdir / relative).resolve()
        try:
            target.relative_to(workdir.resolve())
        except ValueError:
            continue
        old_text = args.get("old_text")
        new_text = args.get("new_text")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            continue
        if not target.is_file():
            continue
        current = target.read_text(encoding="utf-8", errors="replace")
        if current.count(old_text) != 1:
            continue
        denial = mutation_policy.check("apply_patch", args)
        if denial:
            continue
        target.write_text(current.replace(old_text, new_text, 1), encoding="utf-8")
        mutation_policy.observe_result(
            "apply_patch", args, ok=True, output="replayed deferred model patch"
        )
        path = relative.removeprefix("frontend/")
        valid, validation_output = _validate_uncommitted_frontend_paths(
            frontend_dir, changed_paths | {path}
        )
        mutation_policy.observe_validation(
            ok=valid,
            output=validation_output,
            tool="harness_recovery_validation",
        )
        if not valid:
            target.write_text(current, encoding="utf-8")
            return set()
        changed_paths.add(path)
        recovered.add(path)
        recovery_events.append(
            {
                "event": "harness_recovery_patch",
                "ok": True,
                "path": relative,
                "source_event_sequence": source_sequence,
                "source_author": "native_model_deferred_tool_call",
                "reason": "replayed after required zero-token Harness validation",
            }
        )
    if recovered:
        recovery_events.append(
            {
                "event": "harness_recovery_validation",
                "ok": True,
                "paths": sorted(f"frontend/{path}" for path in changed_paths),
                "source_author": "native_model_deferred_tool_call",
            }
        )
        with trace_path.open("a", encoding="utf-8") as trace:
            for event in recovery_events:
                trace.write(json.dumps(event, ensure_ascii=False) + "\n")
            trace.flush()
    return recovered


def _trace_usage_totals(trace_path: Path) -> dict[str, Any]:
    """Sum the last cumulative usage snapshot from every appended agent run."""
    totals = {"input_tokens": 0, "output_tokens": 0}
    total_cost = 0.0
    segment_usage = {"input_tokens": 0, "output_tokens": 0}
    segment_cost = 0.0
    seen_segment = False
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "run_start":
                if seen_segment:
                    totals["input_tokens"] += segment_usage["input_tokens"]
                    totals["output_tokens"] += segment_usage["output_tokens"]
                    total_cost += segment_cost
                segment_usage = {"input_tokens": 0, "output_tokens": 0}
                segment_cost = 0.0
                seen_segment = True
            elif event.get("event") in {"usage", "run_error"}:
                usage = event.get("cumulative_usage")
                if isinstance(usage, dict):
                    segment_usage = {
                        "input_tokens": int(usage.get("input_tokens") or 0),
                        "output_tokens": int(usage.get("output_tokens") or 0),
                    }
                cost = event.get("estimated_cost_usd")
                if isinstance(cost, (int, float)) and cost >= 0:
                    segment_cost = float(cost)
    except (OSError, ValueError, TypeError):
        return {**totals, "estimated_cost_usd": round(total_cost, 6)}
    if seen_segment:
        totals["input_tokens"] += segment_usage["input_tokens"]
        totals["output_tokens"] += segment_usage["output_tokens"]
        total_cost += segment_cost
    return {**totals, "estimated_cost_usd": round(total_cost, 6)}


def _checkpoint_interrupted_model_work(
    frontend_dir: Path, file_comm: FileComm, workdir: Path, round_num: int, mode: GeneratorMode,
) -> str | None:
    """Atomically checkpoint a *previously model-written* uncommitted edit.

    This recovery never changes product source. For a forward Edit it requires
    the harness-owned scope contract; for a root Generate it accepts untracked
    model-written files. The harness independently validates exact provenance
    and syntax before committing; browser evaluation still decides acceptance.
    """
    if mode != "generate":
        return None
    is_forward_edit = (workdir / "seed_manifest.json").is_file()
    if is_forward_edit and _validate_edit_scope(workdir, round_num) is not None:
        return None
    try:
        changed_paths = set(filter(
            None,
            _git_output(frontend_dir, "diff", "HEAD", "--name-only").splitlines(),
        ))
        changed_paths.update(filter(
            None,
            _git_output(
                frontend_dir, "ls-files", "--others", "--exclude-standard"
            ).splitlines(),
        ))
        if not changed_paths:
            return None
        subprocess.run(["git", "diff", "--check"], cwd=frontend_dir, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    trace_path = RoundArtifacts(file_comm, round_num).trace_path("generator")
    # ``ensure_repo`` creates this fixed scaffold file before the model runs.
    # All other recovered files must be explicitly model-authored in the trace.
    harness_scaffold_paths = {".gitignore"} if (frontend_dir / ".gitignore").is_file() else set()
    model_changed_paths = changed_paths - harness_scaffold_paths
    if not model_changed_paths or not model_changed_paths.issubset(
        _trace_written_frontend_paths(trace_path)
    ):
        return None
    # A timeout after a few writes is an infrastructure interruption, not yet a
    # natural completed edit.  Do not manufacture a repair seed by committing
    # that partial state.  The trace must show that the model itself reached a
    # successful syntax/diff/build validation before recovery may checkpoint
    # its untouched diff.
    if not _trace_has_successful_validation(trace_path):
        return None
    # A syntax check is cheap for static seeds and prevents checkpointing a
    # visibly broken script merely because the model ran out of tool calls.
    try:
        for path in sorted(changed_paths):
            if path.endswith((".js", ".mjs", ".cjs")):
                syntax_ok, syntax_output = _validate_javascript_syntax(frontend_dir, path)
                if not syntax_ok:
                    raise subprocess.CalledProcessError(1, ["node", "--check", path], syntax_output)
        subprocess.run(["git", "add", "--all"], cwd=frontend_dir, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        prefix = "feat" if mode == "generate" else "fix"
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-m",
             f"{prefix}(recovery): checkpoint interrupted model implementation"],
            cwd=frontend_dir, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        commit = _git_output(frontend_dir, "rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError):
        return None

    file_comm.write_build_log(
        "# Interrupted model implementation recovery\n\n"
        f"Round: {round_num}\nMode: {mode}\nCommit: {commit}\n\n"
        "The native model trace created the committed frontend diff, but exhausted its "
        "tool-call budget before its own checkpoint. The harness verified `git diff --check` "
        "and JavaScript syntax, then committed the unchanged model-written diff. "
        "No product source was synthesized or modified by recovery.\n"
    )
    file_comm.append_progress_entry(
        f"## Round {round_num} recovery\n\n"
        f"Harness checkpointed the trace-proven model diff at `{commit[:12]}` after tool-budget exhaustion."
    )
    trace_usage = _trace_usage_totals(trace_path)
    (file_comm.dir / f"recovery_commit_round_{round_num}.json").write_text(
        json.dumps({
            "status": "ok", "commit_mode": "harness_checkpoint", "round": round_num,
            "commit": commit, "source_change_author": "native_model_trace",
            "source_files": sorted(model_changed_paths),
            "harness_scaffold_files": sorted(changed_paths - model_changed_paths),
            "precheckpoint_usage": trace_usage,
            "cost_status": "recovered_from_append_only_trace",
        }, indent=2) + "\n", encoding="utf-8"
    )
    return commit


def _is_harness_checkpoint_for_round(
    frontend_dir: Path, file_comm: FileComm, round_num: int, mode: GeneratorMode,
) -> bool:
    """Recognize only the exact, trace-proven checkpoint created above."""
    path = file_comm.dir / f"recovery_commit_round_{round_num}.json"
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        head = _git_output(frontend_dir, "rev-parse", "HEAD")
        subject = _git_output(frontend_dir, "log", "-1", "--format=%s")
        committed_paths = set(filter(None, _git_output(frontend_dir, "diff", "--name-only", "HEAD^..HEAD").splitlines()))
        status = _git_output(frontend_dir, "status", "--porcelain")
    except (OSError, ValueError, TypeError, subprocess.CalledProcessError):
        return False
    expected_prefix = "feat" if mode == "generate" else "fix"
    return (
        metadata.get("status") == "ok"
        and metadata.get("commit_mode") == "harness_checkpoint"
        and metadata.get("round") == round_num
        and metadata.get("commit") == head
        and metadata.get("source_change_author") == "native_model_trace"
        and set(metadata.get("source_files") or []) == committed_paths
        and subject == f"{expected_prefix}(recovery): checkpoint interrupted model implementation"
        and not status
    )


def _validate_repair_scope(
    frontend_dir: Path,
    baseline_commit: str,
    *,
    max_files: int,
    max_changed_lines: int,
) -> str | None:
    """Reject broad repair commits before they become accepted trajectory states."""
    try:
        output = _git_output(
            frontend_dir, "diff", "--numstat", f"{baseline_commit}..HEAD", "--"
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"Repair scope validation failed: {exc}."
    changed_files = 0
    changed_lines = 0
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if Path(path).name in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock"}:
            continue
        changed_files += 1
        if added.isdigit():
            changed_lines += int(added)
        if removed.isdigit():
            changed_lines += int(removed)
    max_files = max(1, int(max_files))
    max_changed_lines = max(1, int(max_changed_lines))
    if changed_files > max_files or changed_lines > max_changed_lines:
        return (
            "Repair diff is too broad for an atomic repair: "
            f"{changed_files} source files and {changed_lines} changed lines; allowed maximum is "
            f"{max_files} files and {max_changed_lines} changed lines. "
            "Reduce the committed diff to the evaluator-confirmed defect only. Preserve all "
            "unrelated code and formatting byte-for-byte, then create a corrective fix commit."
        )
    return None


def _validate_minimal_path_final_diff(
    frontend_dir: Path,
    baseline_commit: str,
    mutation_policy: MinimalPathPolicy | None,
) -> str | None:
    """Reject source changes that bypassed the online minimal-path controller.

    Tool-time denials are necessary but insufficient: an allowed build script or
    provider-specific tool can still mutate a protected file indirectly. The
    final committed diff must therefore be explained by successful, recorded
    source mutations in the harness ledger.
    """
    if mutation_policy is None:
        return None
    try:
        changed = {
            f"frontend/{path}"
            for path in _git_output(
                frontend_dir, "diff", "--name-only", f"{baseline_commit}..HEAD", "--"
            ).splitlines()
            if path
        }
    except (OSError, subprocess.CalledProcessError) as exc:
        return f"Minimal-path final diff validation failed: {exc}."
    code_changed = {
        path for path in changed
        if Path(path).suffix.lower() in {
            ".html", ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx",
            ".vue", ".svelte", ".json", ".json5", ".svg", ".qml", ".ets",
            ".wxml", ".wxss",
        }
    }
    unsupported_asset_changes = sorted(changed - code_changed)
    unexplained = sorted(code_changed - mutation_policy.touched_paths)
    guarded_shared_paths = set(mutation_policy.guarded_shared_regions)
    protected = sorted(
        code_changed & (
            mutation_policy.protected_paths
            | mutation_policy.off_target_paths
            | (mutation_policy.cross_route_shared_paths - guarded_shared_paths)
        )
    )
    if protected:
        return (
            "Committed Edit changed protected multi-page source: "
            + ", ".join(protected)
            + ". Restore those files exactly; only target-route source may change."
        )
    for path in sorted(code_changed & guarded_shared_paths):
        frontend_relative = Path(path).relative_to("frontend").as_posix()
        try:
            before = subprocess.run(
                ["git", "show", f"{baseline_commit}:{frontend_relative}"],
                cwd=frontend_dir,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
            after = (frontend_dir / frontend_relative).read_text(
                encoding="utf-8", errors="replace"
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            return f"Guarded shared-source final validation failed for {path}: {exc}."
        region_error = mutation_policy.validate_guarded_shared_file(
            path, before=before, after=after
        )
        if region_error:
            return region_error
    if unsupported_asset_changes:
        return (
            "Committed Edit changed non-code assets that are outside the current portable "
            "patch contract: " + ", ".join(unsupported_asset_changes) + ". Preserve existing "
            "assets and use supplied images as references; asset mutation needs an explicit "
            "resource-manifest contract."
        )
    if unexplained:
        return (
            "Committed source changes bypassed the harness minimal-path ledger: "
            + ", ".join(unexplained)
            + ". Restore them or apply the intended exact patch through the selected path."
        )
    return None


def _is_forward_static_seed(workdir: Path) -> bool:
    manifest = workdir / "seed_manifest.json"
    if not manifest.is_file():
        return False
    try:
        import json
        source = Path(json.loads(manifest.read_text(encoding="utf-8"))["source_frontend"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return (source / "index.html").is_file() and not (source / "package.json").is_file()


def _validate_generator_runnable_files(frontend_dir: Path, workdir: Path) -> str | None:
    if _is_forward_static_seed(workdir):
        return None
    package_json = frontend_dir / "package.json"
    if not package_json.is_file():
        return (
            "The frontend is missing package.json. Create a runnable frontend package with "
            "at least a dev script, validate it, and commit it inside frontend/.git."
        )
    return None


def _validate_edit_scope(
    workdir: Path,
    round_num: int,
    *,
    required: bool = False,
    baseline_filename: str | None = None,
) -> str | None:
    """Make the declared edit boundary an explicit generator deliverable."""
    if not required and not (workdir / "seed_manifest.json").is_file():
        return None
    path = workdir / ".harness" / f"edit_scope_round_{round_num}.json"
    if not path.is_file():
        return f"Scoped edit/repair requires `{path.relative_to(workdir)}` before stopping."
    try:
        import json
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"Forward edit scope is not valid JSON: {exc}"
    roots = payload.get("allowed_root_keys") if isinstance(payload, dict) else None
    if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
        return "Forward edit scope must contain a string list `allowed_root_keys`."
    if len(set(roots)) != len(roots):
        return "Forward edit scope root keys must be distinct."
    harness_baseline = payload.get("baseline") if isinstance(payload, dict) else None
    if baseline_filename is None and isinstance(harness_baseline, str):
        candidate = Path(harness_baseline)
        if (
            candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.parent.as_posix() not in {".", ".harness"}
        ):
            return "Forward edit scope contains an invalid harness baseline reference."
        baseline_filename = candidate.name
    baseline_path = workdir / ".harness" / (
        baseline_filename or "edit_dom_baseline.json"
    )
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        valid_roots = {str(item["key"]) for item in baseline.get("roots", [])}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"Forward edit baseline roots are unavailable: {exc}"
    unknown = sorted(set(roots) - valid_roots)
    if unknown:
        return "Forward edit scope contains unknown baseline roots: " + ", ".join(unknown)
    if baseline.get("version") == 4:
        if baseline.get("stable") is not True:
            return "Semantic edit baseline is unstable; source mutation is blocked."
        fragments = payload.get("allowed_fragment_keys")
        if not isinstance(fragments, list) or not all(
            isinstance(item, str) for item in fragments
        ):
            return "Semantic edit scope must contain a string list `allowed_fragment_keys`."
        valid_fragments = {
            str(item["key"])
            for item in baseline.get("fragments", [])
            if isinstance(item, dict) and item.get("key")
        }
        unknown_fragments = sorted(set(fragments) - valid_fragments)
        if unknown_fragments:
            return "Semantic edit scope contains unknown baseline fragments: " + ", ".join(
                unknown_fragments
            )
        expected_new = payload.get("expected_new_fragments")
        if not isinstance(expected_new, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("route"), str)
            or not isinstance(item.get("selector"), str)
            or not item.get("selector")
            or not isinstance(item.get("max_count"), int)
            or isinstance(item.get("max_count"), bool)
            or not 1 <= item.get("max_count") <= 50
            for item in expected_new
        ):
            return (
                "Semantic edit scope `expected_new_fragments` must use explicit "
                "route/selector contracts with max_count in 1..50."
            )
    if baseline.get("version") in {3, 4}:
        target_routes = payload.get("target_routes")
        protected_routes = payload.get("protected_routes")
        if (
            not isinstance(target_routes, list)
            or not target_routes
            or not all(isinstance(item, str) for item in target_routes)
            or not isinstance(protected_routes, list)
            or not all(isinstance(item, str) for item in protected_routes)
        ):
            return "Multi-route edit scope requires string lists `target_routes` and `protected_routes`."
        target_set = set(target_routes)
        protected_set = set(protected_routes)
        if target_set & protected_set:
            return "Multi-route edit scope target and protected routes must be disjoint."
        root_routes = {
            str(item.get("key")): str(item.get("route", ""))
            for item in baseline.get("roots", [])
            if isinstance(item, dict) and item.get("key")
        }
        counts: dict[str, int] = {}
        for root in roots:
            route = root_routes.get(root, "")
            if not route or route not in target_set or route in protected_set:
                return "Multi-route edit scope may only allow roots owned by target routes."
            counts[route] = counts.get(route, 0) + 1
        if any(count > 2 for count in counts.values()):
            return "Multi-route edit scope may declare at most two roots per target route."
        if baseline.get("version") == 4:
            fragment_routes = {
                str(item.get("key")): str(item.get("route", "/"))
                for item in baseline.get("fragments", [])
                if isinstance(item, dict) and item.get("key")
            }
            fragment_counts: dict[str, int] = {}
            for fragment in payload.get("allowed_fragment_keys", []):
                route = fragment_routes.get(fragment, "")
                if not route or route not in target_set or route in protected_set:
                    return "Multi-route edit scope may only allow fragments owned by target routes."
                fragment_counts[route] = fragment_counts.get(route, 0) + 1
            for contract in payload.get("expected_new_fragments", []):
                route = str(contract.get("route", ""))
                if not route or route not in target_set or route in protected_set:
                    return "Multi-route edit scope may only expect fragments on target routes."
                if expected_fragment_consumes_scope_slot(contract):
                    fragment_counts[route] = fragment_counts.get(route, 0) + 1
            limits = payload.get("max_fragments_per_route", {})
            if not isinstance(limits, dict) or any(
                route not in target_set or type(limit) is not int or limit < 0
                for route, limit in limits.items()
            ):
                return "Invalid per-route fragment budget."
            if any(count > limits.get(route, 4) for route, count in fragment_counts.items()):
                return "Multi-route edit scope exceeds its declared fragment budget."
    elif len(roots) > 2:
        return "Forward edit scope may declare at most two distinct root keys."
    if not isinstance(payload.get("allow_new_roots", False), bool):
        return "Forward edit scope field `allow_new_roots` must be boolean."
    return None


def _is_scope_contract_only_repair(grades: dict[str, Any]) -> bool:
    """Whether a repair needs only the forward-edit declaration artifact."""
    if grades.get("edit_scope_audit") != "fail":
        return False
    if grades.get("sprint_passed") is not True:
        return False
    if grades.get("regression_passed") is not False:
        return False
    for check in grades.get("ui_checks", []):
        if not isinstance(check, dict) or str(check.get("status", "")).lower() != "pass":
            return False
    for criterion in grades.get("target_exit_criteria_results", []):
        if not isinstance(criterion, dict) or criterion.get("passed") is not True:
            return False
    return True


def _make_generator_stop_hook(
    frontend_dir: Path, baseline_commit: str, mode: GeneratorMode, workdir: Path, round_num: int,
    target_profile: dict[str, Any] | None = None, scope_contract_only: bool = False,
    mutation_policy: MinimalPathPolicy | None = None,
):
    async def _hook(_input: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        state_path = workdir / ".harness/harness_state.json"
        if state_path.is_file() and json.loads(state_path.read_text()).get("supplied_atomic_plan"):
            # The patch transaction has already checked paths and source revision.
            # The single frozen browser check decides whether Repair is needed.
            return {"continue_": True}
        error = _validate_generator_runnable_files(frontend_dir, workdir)
        if error is None:
            error = _validate_no_external_runtime_dependencies(
                frontend_dir, baseline_commit=baseline_commit
            )
        if error is None:
            error = validate_target_submission(frontend_dir, target_profile)
        if error is None and not scope_contract_only:
            error = _validate_generator_commits(frontend_dir, baseline_commit, mode)
        if error is None and not scope_contract_only:
            error = _validate_minimal_path_final_diff(
                frontend_dir, baseline_commit, mutation_policy
            )
        if error is None:
            is_forward = (workdir / "seed_manifest.json").is_file()
            repair_baseline = workdir / ".harness" / repair_baseline_name(round_num)
            error = _validate_edit_scope(
                workdir,
                round_num,
                required=(
                    is_forward
                    or mutation_policy is not None
                    or (mode == "repair" and repair_baseline.is_file())
                ),
                baseline_filename=(
                    None
                    if is_forward or mutation_policy is not None
                    else repair_baseline_name(round_num)
                ),
            )
        if error is None and mode == "repair":
            max_files = (
                mutation_policy.max_touched_files
                if mutation_policy is not None
                else _MAX_REPAIR_FILES
            )
            max_changed_lines = (
                mutation_policy.max_patch_lines * max_files
                if mutation_policy is not None
                else _MAX_REPAIR_CHANGED_LINES
            )
            error = _validate_repair_scope(
                frontend_dir,
                baseline_commit,
                max_files=max_files,
                max_changed_lines=max_changed_lines,
            )
        if error:
            return {"decision": "block", "reason": error, "stopReason": error}
        return {"continue_": True}
    return _hook


def _ensure_local_claude_skills(workdir: Path) -> None:
    """将仓库内置 skills 暴露到 generator 的工作目录。"""
    expose_local_claude_skills(workdir, _LOCAL_CLAUDE_SKILLS_DIR)


def _describe_failures(grades: dict[str, Any], sprint_context: dict[str, Any]) -> str:
    """将上一轮失败项整理成 repair prompt 可直接引用的 Markdown 列表。

    数据来源有三类：

    * ``criteria`` 中低于阈值的评分项；
    * ``ui_checks`` 中状态为 ``fail`` 或 ``partial`` 的检查项；
    * ``target_exit_criteria_results`` 中 ``passed=False`` 的退出条件。

    如果没有识别到失败项，则返回一条保守的兜底说明。
    """
    lines: list[str] = []

    criteria = grades.get("criteria") or {}
    failed_criteria: list[tuple[str, dict[str, Any]]] = []
    if isinstance(criteria, dict):
        for name, payload in criteria.items():
            if not isinstance(payload, dict):
                continue
            threshold = criterion_threshold(name, default=6.0)
            score = payload.get("score")
            if isinstance(score, bool):
                continue
            if isinstance(score, (int, float)) and score < threshold:
                failed_criteria.append((name, payload))

    if failed_criteria:
        lines.append("### Failed criteria")
        for name, payload in failed_criteria:
            notes = str(payload.get("notes", "") or "").strip()
            lines.append(
                f"- **{name}** (score {payload.get('score')}): {notes}"
            )

    target_ids: set[str] = set()
    for fid in sprint_context.get("feature_ids", []) or []:
        text = str(fid).strip()
        if text:
            target_ids.add(text)

    failed_checks = [
        check
        for check in (grades.get("ui_checks") or [])
        if isinstance(check, dict)
        and str(check.get("status", "")).strip().lower() in {"fail", "partial"}
        and (not target_ids or str(check.get("feature_id", "")).strip() in target_ids)
    ]
    if failed_checks:
        if lines:
            lines.append("")
        lines.append("### Failed UI checks")
        for check in failed_checks:
            fid = check.get("feature_id", "?")
            status = check.get("status", "")
            notes = str(check.get("notes", "") or check.get("task", "") or "").strip()
            lines.append(f"- {fid} [{status}]: {notes}")

    failed_exits = [
        result
        for result in (grades.get("target_exit_criteria_results") or [])
        if isinstance(result, dict)
        and result.get("passed") is False
        and (not target_ids or str(result.get("feature_id", "")).strip() in target_ids)
    ]
    if failed_exits:
        if lines:
            lines.append("")
        lines.append("### Failed exit criteria")
        for result in failed_exits:
            fid = result.get("feature_id", "?")
            notes = str(result.get("notes", "") or result.get("criterion", "") or "").strip()
            lines.append(f"- {fid}: {notes}")

    regressions = [
        str(item).strip() for item in (grades.get("regressions_found") or [])
        if str(item).strip()
    ]
    instructions = [
        str(item).strip() for item in (grades.get("repair_instructions") or [])
        if str(item).strip()
    ]
    if regressions:
        if lines:
            lines.append("")
        lines.append("### Reproduced regressions")
        lines.extend(f"- {item}" for item in regressions)
    if instructions:
        if lines:
            lines.append("")
        lines.append("### Required repair actions")
        lines.extend(f"- {item}" for item in instructions)

    return "\n".join(lines) if lines else "(no specific failures found in previous grades)"


def _build_generator_prompt(
    *,
    mode: GeneratorMode,
    file_comm: FileComm,
    round_num: int,
    sprint_num: int,
    sprint_context: dict,
    accepted_sprints: dict,
    resume_uncommitted_work: bool = False,
    recovered_commit: str | None = None,
) -> str:
    """构造 generator 单轮提示词，按 generate/repair 两种模式切换细节。"""
    round_artifacts = RoundArtifacts(file_comm, round_num)
    design_contract = DesignContractContext.load(file_comm)
    accepted = accepted_sprints.get("accepted", [])
    target_profile = file_comm.read_target_profile()
    prior_grades = file_comm.read_grades(round_num - 1) if mode == "repair" else None
    scope_contract_only = isinstance(prior_grades, dict) and _is_scope_contract_only_repair(prior_grades)
    target_guidance = target_profile_guidance(target_profile)
    is_forward_edit = (file_comm.dir.parent / "seed_manifest.json").is_file()
    repair_frame_path = file_comm.dir / repair_baseline_name(round_num)
    has_repair_frame = mode == "repair" and repair_frame_path.is_file()
    minimal_path_ref = f".harness/{plan_name(round_num)}"
    minimal_path_owned = (file_comm.dir / plan_name(round_num)).is_file()
    edit_context = read_edit_context(file_comm.dir, round_num)
    if mode == "generate" and is_forward_edit and minimal_path_owned and edit_context:
        plan = json.loads(
            (file_comm.dir / plan_name(round_num)).read_text(encoding="utf-8")
        )
        checks = []
        verification_plan = file_comm.read_ui_verification_plan() or {}
        for sprint in verification_plan.get("sprints") or []:
            if int(sprint.get("sprint") or 0) == sprint_num:
                checks.extend(sprint.get("checks") or [])
        compact_checks = [
            {
                "id": item.get("id"),
                "route": item.get("route") or "/",
                "actions": item.get("actions") or [],
            }
            for item in checks
        ]
        topology_invariants = _control_topology_invariants(compact_checks)
        compact_scope = {
            "target_routes": (plan.get("route_scope") or {}).get("target_routes") or [],
            "initial_paths": (plan.get("source_change_cone") or {}).get("initial_paths") or [],
            "max_patch_lines": (plan.get("budgets") or {}).get("max_patch_lines"),
            "max_touched_files": (plan.get("budgets") or {}).get("max_touched_files"),
        }
        visible_paths = set((edit_context.get("exposure") or {}).get("selected_paths") or [])
        dependencies = (plan.get("source_change_cone") or {}).get("dependency_paths") or []
        dependencies = [path for path in dependencies if path in visible_paths]
        guarded = (plan.get("source_change_cone") or {}).get("guarded_shared_regions") or []
        if dependencies:
            compact_scope["dependency_paths"] = dependencies
        if guarded:
            compact_scope["guarded_shared_regions"] = guarded
        return (
            "Mode: edit\nTrajectory Role: incremental_edit\n"
            f"Round: {round_num}\nSprint: {sprint_num}\n"
            f"Goal: {sprint_context.get('goal')}\n"
            "\n## Target browser checks\n\n"
            + json.dumps(compact_checks, ensure_ascii=False, separators=(",", ":"))
            + "\n\n## Hard DOM topology invariants\n\n"
            + json.dumps(topology_invariants, ensure_ascii=False, separators=(",", ":"))
            + "\n"
            + _render_control_topology_directives(topology_invariants)
            + "\n\n## Allowed source cone\n\n"
            + json.dumps(compact_scope, ensure_ascii=False, separators=(",", ":"))
            + "\n\n"
            + render_edit_context(edit_context)
            + _render_inspiration_code(file_comm.dir)
            + "\n\nDo not read planning artifacts: their complete relevant content is above. "
            "Only paths shown in Harness-selected source context may be mutated; omitted "
            "dependencies remain closed. Make the smallest exact patch inside the shown "
            "source; do not read the whole file. "
            "If a control is clicked again after another selector is asserted hidden, the control "
            "must remain outside that hidden target so the later click stays actionable."
        )
    if mode == "repair" and minimal_path_owned and edit_context:
        if not isinstance(prior_grades, dict):
            raise RuntimeError(
                f"Generator repair mode requires grade_round_{round_num - 1}.json"
            )
        plan = json.loads(
            (file_comm.dir / plan_name(round_num)).read_text(encoding="utf-8")
        )
        repair_packet_path = file_comm.dir / f"repair_packet_round_{round_num - 1}.json"
        repair_packet = (
            json.loads(repair_packet_path.read_text(encoding="utf-8"))
            if repair_packet_path.is_file()
            else {}
        )
        recent_runtime_errors = _recent_repair_runtime_errors(
            file_comm,
            before_round=round_num - 1,
        )
        compact_packet = {
            "failed_checks": [
                {
                    "check_id": item.get("check_id"),
                    "route": item.get("route") or "/",
                    "console_errors": [
                        str(error)[:400]
                        for error in (item.get("console_errors") or [])[:8]
                        if str(error).strip()
                    ],
                    "steps": [
                        {
                            key: (
                                str(step.get(key) or "")[:400]
                                if key == "error"
                                else step.get(key)
                            )
                            for key in (
                                "action", "ok", "output", "error",
                                "visibility_diagnostic",
                            )
                            if step.get(key) is not None
                        }
                        for step in item.get("steps") or []
                        if isinstance(step, dict)
                    ],
                }
                for item in repair_packet.get("failed_checks") or []
                if isinstance(item, dict)
            ],
            "bugs": repair_packet.get("bugs") or [],
            "regressions": [
                str(item)[:1_600]
                for item in (repair_packet.get("regressions") or [])[:8]
            ],
            "failed_regressions": [
                item
                for item in (repair_packet.get("failed_regressions") or [])[:8]
                if isinstance(item, dict)
            ],
            "required_actions": repair_packet.get("required_actions") or [],
            "repair_task_descriptions": repair_packet.get("repair_task_descriptions") or [],
            "allowed_source_paths": repair_packet.get("allowed_source_paths") or [],
            "recent_runtime_errors": recent_runtime_errors,
        }
        checks = []
        verification_plan = file_comm.read_ui_verification_plan() or {}
        for sprint in verification_plan.get("sprints") or []:
            if int(sprint.get("sprint") or 0) == sprint_num:
                checks.extend(sprint.get("checks") or [])
        compact_checks = [
            {
                "id": item.get("id"),
                "route": item.get("route") or "/",
                "actions": item.get("actions") or [],
            }
            for item in checks
        ]
        topology_invariants = _control_topology_invariants(compact_checks)
        failure_directives = _render_failed_action_directives(
            repair_packet, compact_checks
        )
        recent_error_directives = _render_recent_runtime_error_directives(
            recent_runtime_errors
        )
        if recent_error_directives != "(no additional derived directive)":
            failure_directives += (
                "\n- HARD: a later candidate may have masked, but did not repair, the "
                "following earlier real-browser runtime failure. Preserve this causal "
                "diagnosis until the user-visible contract passes.\n"
                + recent_error_directives
            )
        if "disappeared only after reload" in failure_directives:
            source_functions = [
                name
                for name in _declared_source_functions(edit_context)
                if re.match(r"^(?:handle|load|render|save|setup|show)", name)
            ][:16]
            if source_functions:
                failure_directives += (
                    "\n- HARD: exact existing functions in the bounded source are: "
                    + ", ".join(source_functions)
                    + ". Wire persistence through these exact functions; do not invent a "
                    "near-duplicate router, loader, or renderer name."
                )
            duplicate_pairs = _unreferenced_near_duplicate_functions(edit_context)
            if duplicate_pairs:
                failure_directives += "\n- HARD: source-grounded unwired near-duplicates: " + "; ".join(
                    f"{shorter} (one declaration only) vs {longer} (already referenced)"
                    for shorter, longer in duplicate_pairs
                ) + (
                    ". Remove the unwired shorter repair artifact and patch the already "
                    "referenced function; do not replace the active router."
                )
        compact_scope = {
            "target_routes": (plan.get("route_scope") or {}).get("target_routes") or [],
            "initial_paths": (plan.get("source_change_cone") or {}).get("initial_paths") or [],
            "max_patch_lines": (plan.get("budgets") or {}).get("max_patch_lines"),
            "max_touched_files": (plan.get("budgets") or {}).get("max_touched_files"),
        }
        dependencies = (plan.get("source_change_cone") or {}).get("dependency_paths") or []
        guarded = (plan.get("source_change_cone") or {}).get("guarded_shared_regions") or []
        if dependencies:
            compact_scope["dependency_paths"] = dependencies
        if guarded:
            compact_scope["guarded_shared_regions"] = guarded
        return (
            f"Mode: repair\nTrajectory Role: repair\nRound: {round_num}\nSprint: {sprint_num}\n"
            f"Goal: {sprint_context.get('goal')}\n\n"
            "## Reproduced failure packet\n\n"
            + json.dumps(compact_packet, ensure_ascii=False, separators=(",", ":"))
            + "\n\n## Derived repair directives\n\n"
            + failure_directives
            + "\n\n## Target browser checks\n\n"
            + json.dumps(compact_checks, ensure_ascii=False, separators=(",", ":"))
            + "\n\n## Hard DOM topology invariants\n\n"
            + json.dumps(topology_invariants, ensure_ascii=False, separators=(",", ":"))
            + "\n"
            + _render_control_topology_directives(topology_invariants)
            + "\n\n## Allowed source cone\n\n"
            + json.dumps(compact_scope, ensure_ascii=False, separators=(",", ":"))
            + "\n\n"
            + render_edit_context(edit_context)
            + "\n\nThe code above is already inspected for exact patches inside the shown windows. "
            "Do not reread planning, grade, or shown code. "
            "If a required patch is outside a shown window, read only that focused missing range. "
            "Fix ALL reproduced defects in this packet together in this single Repair response, "
            "including multiple issues of the same type. Preserve unrelated behavior. "
            "Run the smallest validation and create one atomic fix commit. "
            "The next Harness evaluation verifies the result; do not self-report success or add new features."
        )
    trajectory_role = (
        "repair"
        if mode == "repair"
        else "incremental_edit"
        if minimal_path_owned
        else "generate_root"
    )
    feature_ids = ", ".join(sprint_context.get("feature_ids", []))
    common_lines = [
        f"Mode: {mode}\n",
        f"Trajectory Role: {trajectory_role}\n",
        f"Round: {round_num}\n"
        f"Sprint: {sprint_num}\n"
        f"Sprint Title: {sprint_context.get('title')}\n"
        f"Target Feature IDs: {feature_ids}\n"
        f"Accepted Sprints: {accepted}\n"
    ]
    if resume_uncommitted_work:
        common_lines.extend([
            "\nInterrupted-attempt recovery:\n",
            "- A prior invocation for this exact sprint already left intended uncommitted changes in `frontend/`.\n",
            "- FIRST inspect `git -C frontend status --short`. New Generate files are untracked, so `git -C frontend diff --stat` alone can falsely report no changes. Then inspect only the targeted tracked diff or the exact untracked files named by status. Do not reread whole source files, restart the design, or reopen earlier accepted sprint scope.\n",
            "- Verify the targeted diff against `.harness/ui_verification_plan.json`. If a required selector or behavior is missing, finish only that missing work with a focused patch before validation; do not restart the design or read unrelated source.\n",
            "- Once the action contract is complete, keep only changes needed for this sprint; validate them, update the required harness artifacts, and make the required atomic commit.\n",
        ])
    if recovered_commit:
        common_lines.extend([
            "\nVerified interrupted-commit recovery:\n",
            f"- The native-model trace already records the current atomic commit `{recovered_commit[:12]}` for this exact sprint.\n",
            "- Do NOT call tools, edit files, or create another commit. Respond immediately that this committed implementation is ready for browser evaluation.\n",
        ])
    if minimal_path_owned:
        common_lines.extend([
            "\nHarness-owned minimal-path channel:\n",
            "- This round extends an accepted product checkpoint and is a natural incremental Edit "
            "inside the parent Generate trajectory, even when the low-level generator mode is `generate`.\n",
            f"- FIRST read `{minimal_path_ref}`. The harness already materialized "
            f"`.harness/edit_scope_round_{round_num}.json` and a live minimal-path state; do not "
            "create, copy, or edit those harness-owned artifacts.\n",
            "- The Harness-selected source window below is already inspected. Exact patches wholly "
            "inside it do not need another source read; inspect only a focused missing line range "
            "when the required patch falls outside the preloaded window.\n",
            "- Read `design_system_context` in the same plan. Reuse its existing CSS custom "
            "properties for target-local styling before introducing literal visual values or "
            "new tokens. This is guidance; browser evidence still decides behavior and state.\n",
            "- Treat `route_scope.target_routes` as the only page owners in scope. "
            "`off_target_paths` are closed outright. A cross-route shared file is closed unless "
            "`source_change_cone.guarded_shared_regions` names an exact target-route object, "
            "class, or function; any admitted patch must stay wholly inside it. A shared file "
            "also opens normally when every owning route is targeted by this sprint.\n",
            "- A guarded shared stylesheet uses `mutation_mode=target_scoped_css`. Change only "
            "complete CSS rules whose every comma-separated selector branch contains one of its "
            "`allowed_anchors`. Keep the anchor outside functional pseudo-classes such as "
            "`:is()`/`:where()`/`:not()`/`:has()`, and do not escape it with `+` or `~`. Prefer "
            "an exact target ID or `[data-testid]`/`[data-page]` root; a generic component class "
            "does not authorize a shared-style change. This guard is fail-closed for edits inside "
            "at-rules or modern nested selector blocks, so use an already target-local stylesheet "
            "when responsive nesting is required.\n",
            "- After every successful source mutation, run the smallest applicable syntax, diff, "
            "build, or test validation. Only then can a path connected by a recorded dependency "
            "edge be unlocked; protected and unplanned new source paths remain rejected.\n",
            "- If the initial path completes the contract, do not widen. A successful validation "
            "after the latest mutation is required before commit.\n",
            "- Existing source overwrites are rejected. Use exact, unique patches within the plan's "
            "line and touched-file budgets. Reads, applied mutations, validation transitions, denials, "
            "and dependency widening are recorded in the minimal-path ledger.\n",
            "- If `source_change_cone.route_isolation_strategy.status` is `recommended`, follow it "
            "as the preferred path: patch only its `entry_path` to load the "
            "`planned_companion_path` and, when present, `planned_style_path`; run a focused "
            "validation to unlock those dependencies, then create the small route-local files. "
            "Reuse the accepted page's persistence "
            "protocol, but do not wrap or rewrite an accepted page script to make the new route run.\n",
            "- This is an execution policy enforced by the harness. The later counterfactual "
            "certificate remains an independent final check.\n",
            "\n" + render_edit_context(edit_context) + "\n" if edit_context else "",
        ])
    if is_forward_edit and not minimal_path_owned:
        try:
            baseline = json.loads((file_comm.dir / "edit_dom_baseline.json").read_text(encoding="utf-8"))
            root_keys = [str(item["key"]) for item in baseline.get("roots", [])]
        except (OSError, ValueError, KeyError, TypeError):
            root_keys = []
        common_lines.extend([
            "\nForward edit safety contract:\n",
            f"- Before your final commit, write `.harness/edit_scope_round_{round_num}.json`.\n",
            "- It must contain `allowed_root_keys` (at most two exact root keys from the baseline) and `allow_new_roots` (boolean).\n",
            f"- The ONLY valid baseline root keys are: {', '.join(root_keys) or '(unavailable; read the baseline file)'}. Copy one or two of these exact strings; file paths such as `frontend/index.html` are invalid.\n",
            "- Decide the new-root policy from the implementation you are about to commit: set `allow_new_roots` to true when the sprint intentionally adds a top-level interactive surface (for example a sidebar, modal, or floating action region); otherwise set it to false. Do not leave it false merely because the new surface is visually associated with an allowed baseline root.\n",
            "- Write this scope artifact before the first frontend source edit, not at the end of the turn. After source edits, run the smallest applicable validation and commit immediately; do not spend late tool calls rereading build logs or planning artifacts.\n",
            "- The harness independently rejects semantic DOM/ARIA changes outside this declared scope.\n",
            "- If the frozen seed is a plain HTML/CSS/JS site, do not create package.json, lockfiles, dev servers, or dependencies; the harness serves it statically.\n",
        ])
    elif has_repair_frame and not minimal_path_owned:
        try:
            baseline = json.loads(repair_frame_path.read_text(encoding="utf-8"))
            root_keys = [str(item["key"]) for item in baseline.get("roots", [])]
        except (OSError, ValueError, KeyError, TypeError):
            root_keys = []
        common_lines.extend([
            "\nRepair semantic safety contract:\n",
            f"- FIRST write `.harness/edit_scope_round_{round_num}.json` before editing frontend source.\n",
            "- It must contain `allowed_root_keys` (at most two exact roots from the failed-source baseline) and `allow_new_roots` (boolean).\n",
            f"- The ONLY valid failed-source roots are: {', '.join(root_keys) or '(unavailable; read the repair baseline file)'}.\n",
            f"- Read `.harness/{repair_baseline_name(round_num)}` only to choose that narrow footprint. The harness protects every other semantic DOM/ARIA surface.\n",
        ])

    if mode == "generate":
        required_reads = list(_GENERATE_REQUIRED_READS)
        if minimal_path_owned:
            required_reads.append(minimal_path_ref)
        if target_profile:
            required_reads.append(".harness/target_profile.json")
        required_reads.extend(design_contract.required_refs())
        required_reads.extend(round_artifacts.previous_existing_refs())
        required_reads_text = "\n".join(f"- {path}" for path in required_reads)
        deliverables = "\n".join(f"- {item}" for item in sprint_context.get("deliverables", []))
        exit_criteria = "\n".join(f"- {item}" for item in sprint_context.get("exit_criteria", []))
        design_guidance = design_contract.generator_guidance()
        design_guidance_block = f"{design_guidance}\n\n" if design_guidance else ""
        mode_lines = [
            f"Sprint Goal: {sprint_context.get('goal')}\n"
            f"Deliverables:\n{deliverables}\n"
            f"Exit Criteria:\n{exit_criteria}\n"
            f"Required Reads:\n{required_reads_text}\n\n"
            "The sprint goal, target feature IDs, deliverables, and accepted-sprint state are already "
            "included above. Do not reread feature_list.json or accepted_sprints.json unless a concrete "
            "conflict requires it.\n"
            f"{design_guidance_block}"
            f"{target_guidance}\n"
            f"Implement only sprint {sprint_num}.\n"
            f"Set up or update the runnable browser preview in `frontend/`.\n"
            "For an existing project, inspect only the relevant line range or selector before editing; "
            "do not repeatedly reread a truncated whole source file. Preserve its current stack and "
            "use one focused patch per affected file.\n"
            f"Do not implement future sprint functionality or unrelated refactors.\n"
            f"If previous-round feedback or grades are present, read them to preserve accepted work, "
            f"avoid regressions, and carry forward non-blocking polish notes without re-opening already accepted sprint scope.\n"
        ]
        allowed_examples = (
            "Allowed examples: `ls frontend`, `npm create vite@latest frontend -- --template react`, "
            "`npm install --prefix frontend`, `npm --prefix frontend run build`, "
            "`cd frontend && npm run build`, `find frontend/src -type f | head -40`.\n"
        )
        build_log_instruction = (
            "When done, update `.harness/build_log.md` with round, sprint, mode, implemented features, "
            "and a short summary of what was completed.\n"
        )
    else:
        feedback_round = round_num - 1
        previous_artifacts = RoundArtifacts(file_comm, feedback_round)
        previous_grades = file_comm.read_grades(feedback_round)
        if previous_grades is None:
            raise RuntimeError(
                f"Generator repair mode requires {previous_artifacts.grade_ref} "
                f"from the previous round, but it was not found. The previous round may "
                f"have crashed before writing grades."
            )
        failures_text = _describe_failures(previous_grades, sprint_context)
        design_guidance = design_contract.generator_guidance()
        design_guidance_block = f"{design_guidance}\n\n" if design_guidance else ""
        # The sprint goal and normalized failure findings are already inlined
        # below.  Making Qwen reread their source artifacts costs several
        # long tool turns on every natural repair, yet adds no new evidence.
        # Scope plus the browser-selector contract are the only mandatory
        # repair reads; source inspection then stays targeted to the failure.
        required_reads = [
            *(
                [minimal_path_ref, f".harness/edit_scope_round_{round_num}.json"]
                if minimal_path_owned
                else [f".harness/edit_scope_round_{feedback_round}.json"]
                if is_forward_edit
                else [f".harness/{repair_baseline_name(round_num)}"]
                if has_repair_frame
                else []
            ),
            ".harness/ui_verification_plan.json",
            *(
                [f".harness/repair_packet_round_{feedback_round}.json"]
                if (file_comm.dir / f"repair_packet_round_{feedback_round}.json").is_file()
                else []
            ),
            # Design-stage files are not duplicated in the repair prompt and
            # must remain available when a visual/regression repair depends on
            # their placement or responsive contract.
            *design_contract.required_refs(),
        ]
        minimality = previous_grades.get("minimality_certificate") or {}
        edit_certificate = minimality.get("edit") if isinstance(minimality, dict) else None
        if isinstance(edit_certificate, dict) and edit_certificate.get("status") == "non_minimal":
            required_reads.append(
                str(edit_certificate.get("artifact") or f".harness/minimality_round_{feedback_round}_edit.json")
            )
        required_reads_text = "\n".join(f"- {path}" for path in required_reads)
        if minimal_path_owned:
            scope_first_action = (
                f"FIRST ACTION: read `{minimal_path_ref}` and inspect only its single initial "
                "source path. The scope is already computed and immutable.\n"
            )
            scope_preservation_guidance = (
                "Follow the harness-selected state transition returned by the tools: inspect, make "
                "one exact patch, validate, and only then follow a recorded dependency edge when "
                "the contract still requires it. Do not cross from a target route into shared or "
                "off-target page source. If a mutation is denied, use its returned next "
                "action instead of expanding to an unrelated file.\n"
            )
        elif is_forward_edit:
            scope_first_action = (
                f"FIRST ACTION: copy `.harness/edit_scope_round_{feedback_round}.json` to "
                f"`.harness/edit_scope_round_{round_num}.json` before any investigation. This is a "
                "required trajectory artifact, not a conclusion about the repair.\n"
            )
            scope_preservation_guidance = (
                "Preserve the previous edit scope whenever the repair remains within its declared "
            "surfaces/new-root policy. Do not replace a valid allow_new_roots contract with a narrower "
            "one merely because the changed element is visually near a different page region. If the "
            "previous grade's scope audit reports an undeclared new root, correct the copied scope "
            "artifact before your final commit: retain the declared baseline roots and set "
            "allow_new_roots to true only when the repair still genuinely needs that root. A new root "
            "can NEVER be added to allowed_root_keys because that list only permits baseline keys. If "
            "all product checks passed and scope audit is the only failure, do not modify frontend source "
            "or invent an empty Git commit: update only the scope artifact and required logs.\n"
            )
        elif has_repair_frame:
            scope_first_action = (
                f"FIRST ACTION: write `.harness/edit_scope_round_{round_num}.json` from the "
                "failed-source semantic roots listed above, before investigating or editing source.\n"
            )
            scope_preservation_guidance = (
                "Keep the repair inside the newly declared failed-source roots. Do not change, remove, "
                "or restyle semantic surfaces outside that repair footprint.\n"
            )
        else:
            scope_first_action = (
                "The failed source did not render, so no DOM/ARIA repair frame is available; "
                "start from the exact startup evidence and keep the source diff atomic.\n"
            )
            scope_preservation_guidance = (
                "Because the source could not render, preserve every unrelated file and line "
                "byte-for-byte and change only the startup defect.\n"
            )
        mode_lines = [
            "Repair Scope: Fix evaluator-reported issues for the current sprint only\n"
            f"{scope_first_action}"
            f"Required minimal reads:\n{required_reads_text}\n\n"
            f"{design_guidance_block}"
            f"{target_guidance}\n"
            "## Previous evaluation findings\n\n"
            f"{failures_text}\n\n"
            f"The harness-owned `.harness/repair_packet_round_{feedback_round}.json` is the "
            "bounded failure handoff when present. Use its failed checks, allowed source paths, "
            "and dynamic budgets; do not reopen unrelated project exploration.\n\n"
            "## Your task\n"
            "Address every failure above. Do not stop until each one is fixed. "
            "There is no self-report file; the next evaluation round verifies your work.\n"
            "Use the inlined findings and grade/feedback as the primary evidence; do not reread their "
            "files unless their inlined content is truncated. Read the previous "
            "evaluator trace only when a finding names a failed normal browser_click or an exact "
            "runtime error; then inspect only the matching trace lines. A failed normal browser_click "
            "is a reproduced usability defect: fix its cause; do not treat a forced or programmatic "
            "click as a substitute. A merely partial or unverified check is not by itself proof of a defect.\n"
            "For a reported source or runtime defect, start from the exact failing behavior and use "
            "targeted line-range reads (for example `sed -n`) around the implicated handler or markup; "
            "do not repeatedly reread a truncated whole file. Before committing JavaScript changes, "
            "verify the edited script is syntactically valid (for example `node --check`) and preserve "
            "the previously working behavior outside the reported defect.\n"
            "For interaction state that closes, dismisses, cancels, or resets incorrectly, trace any "
            "pending asynchronous work (debounce timers, delayed callbacks, promises, animation completion, "
            "or stale requests) that can reapply the old state. Cancel or invalidate that work before "
            "adding redundant event handlers or compatibility fallbacks.\n"
            "When a browser API such as Web Share or clipboard is the reported failure, do not await an "
            "unbounded native prompt before updating the user-visible feedback state. Give the aria-live "
            "feedback synchronously or through a bounded fallback, then preserve the native API as a "
            "best-effort enhancement.\n"
            "After the required scope declaration, make the smallest repair, commit it, and update the build "
            "log/progress artifacts. These required artifacts take priority over additional exploratory "
            "tool calls when turns are limited.\n"
            "Visual evidence is captured independently by the harness in both top and scrolled states. "
            "Never alter required product visibility or interaction behavior merely to make a screenshot show a control.\n"
            f"{scope_preservation_guidance}"
            "Fix ONLY the issues needed for sprint acceptance or regression recovery.\n"
            "Use localized patches and preserve untouched code exactly; broad rewrites or "
            "formatting churn make the repair unusable as training data. Normally touch no "
            "more than four source files.\n"
            "Do not implement new features from future sprints.\n"
            "Do not start work for the next sprint.\n"
        ]
        allowed_examples = (
            f"Allowed examples: `ls frontend/src`, `grep -n \"pattern\" frontend/src/App.jsx`, "
            f"`npm install --prefix frontend`, `npm --prefix frontend run build`, "
            f"`cd frontend && npm run build`, `find frontend/src -type f | head -40`.\n"
        )
        build_log_instruction = (
            "When done, update `.harness/build_log.md` with round, sprint, mode, addressed issues, "
            "and a short summary of what was repaired.\n"
        )

    common_tail = (
        "The native OpenAI runner has no `.claude/skills/` directory. Do not attempt to read that path.\n"
        "Use paths relative to the workdir when calling tools; do not use absolute paths.\n"
        "For Bash, command chains and pipelines are allowed when each segment stays inside the workdir.\n"
        "For every planner-authored UI action, implement the exact stable selector specified in `.harness/ui_verification_plan.json`; these selectors are part of the acceptance contract, not optional test metadata.\n"
        "For every check with a non-empty `fixtures` list, materialize those exact literals in the owning route's initial content before implementing the action flow; fixtures are executable data dependencies, not suggestions.\n"
        "Keep the frontend portable and offline: do not load remote fonts, scripts, stylesheets, media, iframes, or API/WebSocket/EventSource URLs. Ordinary external anchor links are allowed; runtime dependencies must be local.\n"
        "Do not use background execution, redirection, or command substitution such as `&`, `>`, `<`, `$(`, or backticks.\n"
        "For package-manager and build commands, target `frontend/` explicitly with "
        "`npm --prefix frontend ...` or `cd frontend && ...`; never run `npm run build` from the workdir root.\n"
        f"{allowed_examples}"
        f"{build_log_instruction}"
        "Also append a short progress entry to `.harness/progress.md`.\n"
        "Treat `.` as the workdir root."
    )

    return "".join(common_lines + mode_lines) + common_tail


def _validate_generator_outputs(file_comm: FileComm, workdir: Path, result_summary: str) -> None:
    """校验 generator 的最低交付物，避免空跑后继续后续阶段。"""
    frontend_dir = workdir / "frontend"
    package_json = frontend_dir / "package.json"

    if frontend_dir.exists() and (package_json.exists() or _is_forward_static_seed(workdir)):
        target_error = validate_target_submission(
            frontend_dir, file_comm.read_target_profile()
        )
        if target_error is None:
            return
        raise RuntimeError(target_error)

    if result_summary and not file_comm.read_build_log():
        file_comm.write_build_log(result_summary)

    if not frontend_dir.exists():
        existing_dirs = sorted(
            path.relative_to(workdir).as_posix()
            for path in workdir.iterdir()
            if path.is_dir() and path.name != ".harness"
        )
        raise RuntimeError(
            "Generator completed without creating the expected frontend directory "
            f"('frontend'). Found directories: {existing_dirs or 'none'}."
        )

    raise RuntimeError(
        "Generator created 'frontend/' but it is missing 'package.json'. "
        "An empty frontend directory cannot serve a dev server, so the round "
        "is treated as a failed build instead of waiting 90s for the dev "
        "server to time out."
    )


async def run_generator(
    config: HarnessConfig,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
    sprint_num: int,
    mode: GeneratorMode,
) -> AgentRunStats:
    """运行 generator，并返回统一的执行统计信息。"""
    logger.info(
        f"[bold green]Generator[/] starting mode={mode} round={round_num} sprint={sprint_num}"
    )
    # These long Claude-oriented skill documents are useful to the Claude SDK,
    # but Qwen repeatedly reads them verbatim and spends repair turns before
    # inspecting the actual failing project.  Sprint/design artifacts are the
    # authoritative guidance for the native OpenAI runner.
    if config.agent_runtime.strip().lower() != "openai":
        _ensure_local_claude_skills(workdir)

    frontend_dir = workdir / "frontend"
    baseline_commit = await _ensure_generator_baseline(frontend_dir)
    if _is_harness_checkpoint_for_round(frontend_dir, file_comm, round_num, mode):
        logger.info(
            "[bold green]Generator[/] found verified harness checkpoint for round %s; proceeding to evaluation without a duplicate model call.",
            round_num,
        )
        return AgentRunStats(
            cost_usd=0.0,
            duration_ms=0,
            duration_api_ms=0,
            token_usage={},
            usage={"recovery": "harness_checkpoint", "precheckpoint_model_cost": "unknown"},
            model_usage={},
        )
    recovered = _recover_interrupted_commit(frontend_dir, file_comm, round_num, mode)
    recovered_commit = None
    if recovered is not None:
        baseline_commit, recovered_commit = recovered
        logger.info(
            "[bold green]Generator[/] recovered trace-backed commit %s; requesting only completion confirmation.",
            recovered_commit[:12],
        )
    if recovered_commit is None:
        recovered_paths = _recover_deferred_model_patches(
            frontend_dir, file_comm, workdir, round_num
        )
        if recovered_paths:
            logger.info(
                "[bold green]Generator[/] replayed %s exact model patch(es) that the "
                "Harness had deferred only for local validation.",
                len(recovered_paths),
            )
    resume_uncommitted_work = bool(_git_output(frontend_dir, "status", "--porcelain"))
    if resume_uncommitted_work:
        checkpoint = _checkpoint_interrupted_model_work(
            frontend_dir, file_comm, workdir, round_num, mode,
        )
        if checkpoint is not None:
            recovered_usage = _trace_usage_totals(
                RoundArtifacts(file_comm, round_num).trace_path("generator")
            )
            logger.info(
                "[bold green]Generator[/] checkpointed trace-proven interrupted model work at %s; no new model call made.",
                checkpoint[:12],
            )
            return AgentRunStats(
                cost_usd=float(recovered_usage["estimated_cost_usd"]),
                duration_ms=0,
                duration_api_ms=0,
                token_usage={
                    "input_tokens": int(recovered_usage["input_tokens"]),
                    "output_tokens": int(recovered_usage["output_tokens"]),
                },
                usage={
                    "recovery": "harness_checkpoint",
                    "precheckpoint_model_cost": "recovered_from_append_only_trace",
                    **recovered_usage,
                },
                model_usage={},
            )
        if not config.allow_paid_resume_call:
            raise RuntimeError(
                "Trace-proven interrupted work could not be recovered without another "
                "paid Generator request. Set ALLOW_PAID_RESUME_CALL=1 only after explicit "
                "authorization for that new request."
            )

    if recovered_commit is not None:
        # The native trace already proves this exact clean HEAD was committed
        # by the model.  Asking the model to merely acknowledge completion
        # adds tokens but no new product evidence; browser evaluation is the
        # authoritative next step for both generation and repair commits.
        logger.info(
            "[bold green]Generator[/] reusing trace-backed commit %s; no new model call made.",
            recovered_commit[:12],
        )
        return AgentRunStats(
            cost_usd=0.0,
            duration_ms=0,
            duration_api_ms=0,
            token_usage={},
            usage={"recovery": "trace_backed_commit", "commit": recovered_commit},
            model_usage={},
        )

    sprint_run_context = SprintState.load(file_comm).required_run_context(
        sprint_num,
        owner="Generator",
    )
    user_msg = _build_generator_prompt(
        mode=mode,
        file_comm=file_comm,
        round_num=round_num,
        sprint_num=sprint_num,
        sprint_context=sprint_run_context.sprint_context,
        accepted_sprints=sprint_run_context.accepted_sprints,
        resume_uncommitted_work=resume_uncommitted_work,
        recovered_commit=recovered_commit,
    )
    obligations = chain_obligations(read_edit_task_contract(workdir) or {})
    if obligations:
        user_msg += "\nChain host-state obligations:\n" + json.dumps(obligations, ensure_ascii=False)
    input_context = task_input_prompt_context(workdir)
    if input_context:
        user_msg += (
            "\n\n" + input_context +
            "\nUse attached reference images as user requirements for the target surface only. "
            "Do not restyle protected routes or unrelated components to make the whole project "
            "match a reference intended for one page.\n"
        )
    target_profile = file_comm.read_target_profile()
    prior_grades = file_comm.read_grades(round_num - 1) if mode == "repair" else None
    scope_contract_only = isinstance(prior_grades, dict) and _is_scope_contract_only_repair(prior_grades)
    mutation_policy = (
        MinimalPathPolicy.load(workdir, round_num)
        if config.minimal_path_guidance_enabled
        else None
    )
    if mutation_policy is not None and _atomic_executor_eligible(
        config=config, workdir=workdir, round_num=round_num
    ):
        replay_candidate = _last_replayable_atomic_candidate(
            RoundArtifacts(file_comm, round_num).trace_path("generator")
        )
        if replay_candidate:
            logger.info(
                "[bold green]Generator[/] replaying the last trace-backed rejected "
                "candidate against current deterministic guards; no model call made."
            )
            try:
                stats = await _run_atomic_patch_executor(
                    config=config,
                    file_comm=file_comm,
                    workdir=workdir,
                    round_num=round_num,
                    mode=mode,
                    prompt=user_msg,
                    baseline_commit=baseline_commit,
                    mutation_policy=mutation_policy,
                    semantic_attempt=2,
                    candidate_override=replay_candidate,
                )
            except AtomicCandidateRejected as exc:
                if (file_comm.read_state() or {}).get("supplied_atomic_plan"):
                    raise
                logger.info(
                    "Trace-backed candidate still fails current guards; requesting one "
                    "new bounded candidate."
                )
                mutation_policy = MinimalPathPolicy.load(workdir, round_num) or mutation_policy
                stats = await _run_atomic_patch_executor(
                    config=config,
                    file_comm=file_comm,
                    workdir=workdir,
                    round_num=round_num,
                    mode=mode,
                    prompt=user_msg,
                    baseline_commit=baseline_commit,
                    mutation_policy=mutation_policy,
                    correction_feedback=(
                        "Exact local rejection from the replayed, fully rolled-back candidate:\n"
                        + _atomic_rejection_feedback(exc)
                    ),
                )
        else:
            stats = await _run_atomic_patch_executor(
                config=config,
                file_comm=file_comm,
                workdir=workdir,
                round_num=round_num,
                mode=mode,
                prompt=user_msg,
                baseline_commit=baseline_commit,
                mutation_policy=mutation_policy,
            )
        # A compact correction is executed recursively with a freshly loaded
        # policy after the rejected candidate was rolled back.  Reload before
        # the final gate so only the accepted transaction explains the commit.
        mutation_policy = MinimalPathPolicy.load(workdir, round_num) or mutation_policy
        completion_gate = _make_generator_stop_hook(
            frontend_dir,
            baseline_commit,
            mode,
            workdir,
            round_num,
            target_profile,
            scope_contract_only=scope_contract_only,
            mutation_policy=mutation_policy,
        )
        gate_result = await completion_gate(None, None, None)
        if gate_result.get("decision") == "block":
            raise RuntimeError(str(gate_result.get("reason") or "atomic Edit validation failed"))
        _validate_generator_outputs(file_comm, workdir, "atomic Edit applied")
        return stats

    if (file_comm.read_state() or {}).get("supplied_atomic_plan"):
        raise RuntimeError("Product Session requires complete source context for its single-call patch executor")
    result, cost, _assistant_text, permission_denials = await run_sdk_agent(
        prompt=user_msg,
        config=config,
        workdir=workdir,
        model=config.generator_model,
        system_prompt=(
            REPAIR_SYSTEM_PROMPT
            if mode == "repair" and read_edit_context(file_comm.dir, round_num)
            else EDIT_SYSTEM_PROMPT
            if mode == "generate"
            and (workdir / "seed_manifest.json").is_file()
            and read_edit_context(file_comm.dir, round_num)
            else GENERATOR_SYSTEM_PROMPT
        ),
        max_turns=config.generator_max_turns,
        allow_bash=True,
        stop_hooks=[_make_generator_stop_hook(
            frontend_dir, baseline_commit, mode, workdir, round_num, target_profile,
            scope_contract_only=scope_contract_only,
            mutation_policy=mutation_policy,
        )],
        trace_path=RoundArtifacts(file_comm, round_num).trace_path("generator"),
        mutation_policy=mutation_policy,
        image_paths=task_input_image_paths(workdir),
    )

    _validate_generator_outputs(file_comm, workdir, (result.result or "").strip())

    if permission_denials:
        logger.warning(
            f"[bold green]Generator[/] completed with permission denials: {permission_denials}"
        )

    logger.info(
        f"[bold green]Generator[/] mode={mode} round={round_num} sprint={sprint_num} "
        f"done. Cost: ${cost:.4f}"
    )
    return build_agent_run_stats(result, model=config.generator_model)
