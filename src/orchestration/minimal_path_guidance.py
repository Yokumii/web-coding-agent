"""Harness-owned guidance for the shortest defensible edit path.

The counterfactual minimality certificate answers a post-hoc question: could a
finished patch be made smaller?  This module answers the earlier operational
question: where may an editor mutate in the first place?

For scoped edits/repairs the harness derives a change cone from executable UI
checks, the frozen semantic DOM, source anchors, and explicit dependency edges.
The resulting policy is consumed by mutation tools, so it is an execution
boundary rather than a prompt preference.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


PLAN_VERSION = "minimal-path-plan-v1"
CODE_EXTENSIONS = {
    ".css",
    ".ets",
    ".htm",
    ".html",
    ".js",
    ".json",
    ".json5",
    ".jsx",
    ".qml",
    ".scss",
    ".svelte",
    ".svg",
    ".ts",
    ".tsx",
    ".vue",
    ".wxml",
    ".wxss",
}
IGNORED_PARTS = {
    ".git",
    ".next",
    ".nuxt",
    ".output",
    "build",
    "coverage",
    "dist",
    "node_modules",
}
_QUERY_SELECTOR_RE = re.compile(
    r"(?:querySelector|closest|matches)\(\s*['\"]([^'\"]+)['\"]"
)
_GET_BY_ID_RE = re.compile(r"getElementById\(\s*['\"]([^'\"]+)['\"]")
_IMPORT_RE = re.compile(
    r"(?:import\s+(?:[^'\"]+?\s+from\s+)?|require\(\s*)['\"]([^'\"]+)['\"]"
)
_HTML_REF_RE = re.compile(r"(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\()?\s*['\"]([^'\"]+)['\"]", re.I)
_MUTATING_SHELL_RE = re.compile(
    r"(?:^|(?:&&|\|\||;)\s*|\s)(?:cp|mv|touch|mkdir)\s|"
    r"(?:^|\s)(?:npm|pnpm|yarn)\s+(?:add|install|create)\b"
)


def plan_name(round_num: int) -> str:
    return f"minimal_path_plan_round_{round_num}.json"


def ledger_name(round_num: int) -> str:
    return f"minimal_path_ledger_round_{round_num}.jsonl"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _walk_strings(value: Any) -> Iterable[tuple[str | None, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                yield str(key), item
            else:
                yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _target_checks(plan: dict[str, Any], sprint_num: int) -> list[dict[str, Any]]:
    for sprint in plan.get("sprints", []):
        if isinstance(sprint, dict) and sprint.get("sprint") == sprint_num:
            return [item for item in sprint.get("checks", []) if isinstance(item, dict)]
    return []


def _extract_selectors(checks: list[dict[str, Any]]) -> list[str]:
    selectors: set[str] = set()
    for key, value in _walk_strings(checks):
        if key in {"selector", "locator"} and value.strip():
            selectors.add(value.strip())
        for match in _QUERY_SELECTOR_RE.finditer(value):
            selectors.add(match.group(1).strip())
        for match in _GET_BY_ID_RE.finditer(value):
            selectors.add("#" + match.group(1).strip())
    return sorted(item for item in selectors if item)


def _selector_tokens(selectors: list[str]) -> list[str]:
    tokens: set[str] = set(selectors)
    patterns = (
        re.compile(r"#([A-Za-z_][\w:-]*)"),
        re.compile(r"\.([A-Za-z_][\w:-]*)"),
        re.compile(
            r"\[(?:data-testid|aria-label|name|role)\s*=\s*['\"]?([^'\"\]]+)"
        ),
    )
    for selector in selectors:
        for pattern in patterns:
            tokens.update(match.group(1).strip() for match in pattern.finditer(selector))
    return sorted(token for token in tokens if len(token) >= 2)


def _code_files(frontend: Path) -> list[Path]:
    if not frontend.is_dir():
        return []
    return sorted(
        path
        for path in frontend.rglob("*")
        if path.is_file()
        and path.suffix.lower() in CODE_EXTENSIONS
        and not (set(path.relative_to(frontend).parts) & IGNORED_PARTS)
        and path.stat().st_size <= 1_000_000
    )


def _relative_to_workdir(path: Path, workdir: Path) -> str:
    return path.resolve().relative_to(workdir.resolve()).as_posix()


def _source_hotspots(
    files: list[Path], selectors: list[str], tokens: list[str], workdir: Path
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    hotspots: list[dict[str, Any]] = []
    scores: dict[str, int] = {}
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = _relative_to_workdir(path, workdir)
        matches: list[dict[str, Any]] = []
        score = 0
        lines = content.splitlines()
        for selector in selectors:
            for index, line in enumerate(lines, start=1):
                if selector in line:
                    matches.append(
                        {"line": index, "anchor": selector, "strength": "selector"}
                    )
                    score += 8
        for token in tokens:
            if token in selectors:
                continue
            for index, line in enumerate(lines, start=1):
                if token in line:
                    matches.append(
                        {"line": index, "anchor": token, "strength": "token"}
                    )
                    score += 3
        if score:
            scores[relative] = score
            hotspots.append(
                {
                    "path": relative,
                    "score": score,
                    "matches": matches[:24],
                }
            )
    hotspots.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    return hotspots, scores


def _resolve_reference(frontend: Path, source: Path, reference: str) -> Path | None:
    clean = reference.split("?", 1)[0].split("#", 1)[0]
    if not clean or clean.startswith(("http://", "https://", "//", "data:")):
        return None
    if clean.startswith("/"):
        base = frontend / clean.lstrip("/")
    else:
        base = source.parent / clean
    candidates = [base]
    if not base.suffix:
        candidates.extend(base.with_suffix(ext) for ext in sorted(CODE_EXTENSIONS))
        candidates.extend((base / f"index{ext}") for ext in sorted(CODE_EXTENSIONS))
    frontend_resolved = frontend.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(frontend_resolved)
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved.suffix.lower() in CODE_EXTENSIONS:
            return resolved
    return None


def _dependency_graph(
    files: list[Path], frontend: Path, workdir: Path
) -> tuple[dict[str, set[str]], list[dict[str, str]]]:
    graph: dict[str, set[str]] = {}
    edges: set[tuple[str, str]] = set()
    for source in files:
        try:
            content = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        references = [match.group(1) for match in _IMPORT_RE.finditer(content)]
        references += [match.group(1) for match in _HTML_REF_RE.finditer(content)]
        references += [match.group(1) for match in _CSS_IMPORT_RE.finditer(content)]
        source_rel = _relative_to_workdir(source, workdir)
        for reference in references:
            target = _resolve_reference(frontend, source, reference)
            if target is None:
                continue
            target_rel = _relative_to_workdir(target, workdir)
            graph.setdefault(source_rel, set()).add(target_rel)
            edges.add((source_rel, target_rel))
    return graph, [
        {"from": source, "to": target} for source, target in sorted(edges)
    ]


def _entrypoints(files: list[Path], frontend: Path, workdir: Path) -> list[str]:
    preferred = (
        "index.html",
        "src/App.tsx",
        "src/App.jsx",
        "src/App.vue",
        "src/App.svelte",
        "src/main.tsx",
        "src/main.jsx",
        "src/main.ts",
        "src/main.js",
    )
    existing = {path.resolve(): path for path in files}
    output: list[str] = []
    for name in preferred:
        candidate = (frontend / name).resolve()
        if candidate in existing:
            output.append(_relative_to_workdir(candidate, workdir))
    return output


def _dom_scope(
    baseline: dict[str, Any], selectors: list[str], tokens: list[str]
) -> tuple[list[str], bool, list[dict[str, Any]]]:
    matches: list[dict[str, Any]] = []
    for root in baseline.get("roots", []):
        if not isinstance(root, dict) or not isinstance(root.get("key"), str):
            continue
        key = str(root["key"])
        anchors = {
            str(item) for item in root.get("anchors", []) if isinstance(item, str)
        }
        evidence = sorted(
            selector
            for selector in selectors
            if selector in anchors
            or selector.lstrip("#.") == key
            or any(token in key or token in anchors for token in _selector_tokens([selector]))
        )
        if evidence:
            matches.append({"root": key, "evidence": evidence})
    allowed = [item["root"] for item in matches[:2]]
    baseline_has_anchors = any(
        isinstance(root, dict) and bool(root.get("anchors"))
        for root in baseline.get("roots", [])
    )
    # With v2 baselines, an action selector absent from every root is positive
    # evidence that the task introduces a new semantic surface.  Old baselines
    # lack anchor provenance, so preserve the conservative legacy default.
    strong_new_surface_selector = any(
        selector.startswith("#")
        or "data-testid" in selector
        or "aria-label" in selector
        for selector in selectors
    )
    allow_new = bool(
        selectors
        and baseline_has_anchors
        and not allowed
        and strong_new_surface_selector
    )
    return allowed, allow_new, matches


def _scope_payload(plan: dict[str, Any]) -> dict[str, Any]:
    dom = plan["dom_change_cone"]
    return {
        "schema_version": "edit-scope-v2",
        "owner": "harness",
        "plan": f".harness/{plan_name(int(plan['round']))}",
        "baseline": dom.get("baseline"),
        "allowed_root_keys": list(dom.get("allowed_root_keys", [])),
        "allow_new_roots": bool(dom.get("allow_new_roots", False)),
    }


def ensure_minimal_path_plan(
    *,
    workdir: Path,
    harness_dir: Path,
    round_num: int,
    sprint_num: int,
    mode: str,
    max_patch_lines: int,
    max_touched_files: int,
) -> dict[str, Any]:
    """Create one immutable harness-owned plan for an edit/repair round."""
    path = harness_dir / plan_name(round_num)
    existing = _read_json(path, None)
    if isinstance(existing, dict) and existing.get("schema_version") == PLAN_VERSION:
        scope_path = harness_dir / f"edit_scope_round_{round_num}.json"
        if not scope_path.exists():
            _write_json(scope_path, _scope_payload(existing))
        return existing

    ui_plan = _read_json(harness_dir / "ui_verification_plan.json", {})
    checks = _target_checks(ui_plan, sprint_num)
    selectors = _extract_selectors(checks)
    tokens = _selector_tokens(selectors)
    baseline_candidates = (
        harness_dir / f"repair_dom_source_round_{round_num}.json",
        harness_dir / f"edit_dom_source_sprint_{sprint_num}.json",
        harness_dir / "edit_dom_baseline.json",
    )
    baseline_path = next((item for item in baseline_candidates if item.is_file()), None)
    baseline = _read_json(baseline_path, {}) if baseline_path else {}
    allowed_roots, allow_new_roots, root_evidence = _dom_scope(
        baseline, selectors, tokens
    )

    frontend = workdir / "frontend"
    files = _code_files(frontend)
    hotspots, scores = _source_hotspots(files, selectors, tokens, workdir)
    graph, edges = _dependency_graph(files, frontend, workdir)
    entries = _entrypoints(files, frontend, workdir)

    ranked = [str(item["path"]) for item in hotspots]
    seeds = ranked[:max_touched_files]
    if not seeds:
        seeds = entries[:1]
    local: list[str] = list(dict.fromkeys(seeds))
    # A fallback entry point is useful only together with its direct imports;
    # that is the smallest executable source unit for common static/SPA seeds.
    if not ranked:
        for seed in list(local):
            for dependency in sorted(graph.get(seed, set())):
                if dependency not in local and len(local) < max_touched_files:
                    local.append(dependency)
    # The hotspot table retains rank/score evidence.  The executable allowlist
    # is sorted so repeated runs expose a stable path order to models/tools.
    local = sorted(local)

    dependencies: list[str] = []
    for seed in local:
        for dependency in sorted(graph.get(seed, set())):
            if dependency not in local and dependency not in dependencies:
                dependencies.append(dependency)
    all_paths = [_relative_to_workdir(item, workdir) for item in files]
    protected = sorted(set(all_paths) - set(local) - set(dependencies))
    status = "ready" if local and checks else "advisory"
    plan = {
        "schema_version": PLAN_VERSION,
        "owner": "harness",
        "round": round_num,
        "sprint": sprint_num,
        "mode": mode,
        "status": status,
        "target_contract": {
            "check_ids": [str(item.get("id", "")) for item in checks],
            "selectors": selectors,
            "selector_tokens": tokens,
        },
        "dom_change_cone": {
            "baseline": (
                f".harness/{baseline_path.name}" if baseline_path is not None else None
            ),
            "allowed_root_keys": allowed_roots,
            "allow_new_roots": allow_new_roots,
            "root_evidence": root_evidence,
        },
        "source_change_cone": {
            "local_paths": local,
            "dependency_paths": dependencies,
            "protected_paths": protected,
            "dependency_edges": edges,
            "hotspots": hotspots,
            "entrypoints": entries,
            "path_scores": scores,
        },
        "budgets": {
            "max_patch_lines": max(1, int(max_patch_lines)),
            "max_touched_files": max(1, int(max_touched_files)),
            "existing_source_requires_exact_patch": True,
        },
        "widening_policy": {
            "dependency_tier": "automatic_only_for_recorded_dependency_edges",
            "protected_tier": "denied",
            "whole_file_overwrite": "denied_for_existing_frontend_source",
        },
    }
    _write_json(path, plan)
    _write_json(harness_dir / f"edit_scope_round_{round_num}.json", _scope_payload(plan))
    return plan


class MinimalPathPolicy:
    """Stateful pre-mutation gate backed by a harness-owned plan."""

    def __init__(self, workdir: Path, plan: dict[str, Any]) -> None:
        self.workdir = workdir.resolve()
        self.plan = plan
        self.round_num = int(plan["round"])
        cone = plan.get("source_change_cone") or {}
        self.local_paths = set(cone.get("local_paths") or [])
        self.dependency_paths = set(cone.get("dependency_paths") or [])
        self.protected_paths = set(cone.get("protected_paths") or [])
        self.dependency_edges = {
            (str(item.get("from")), str(item.get("to")))
            for item in cone.get("dependency_edges") or []
            if isinstance(item, dict)
        }
        budgets = plan.get("budgets") or {}
        self.max_patch_lines = max(1, int(budgets.get("max_patch_lines", 120)))
        self.max_touched_files = max(1, int(budgets.get("max_touched_files", 3)))
        self.touched_paths: set[str] = set()
        self.ledger_path = (
            self.workdir / ".harness" / ledger_name(self.round_num)
        )

    @classmethod
    def from_plan(cls, workdir: Path, plan: dict[str, Any]) -> "MinimalPathPolicy":
        return cls(workdir, plan)

    @classmethod
    def load(cls, workdir: Path, round_num: int) -> "MinimalPathPolicy | None":
        plan = _read_json(workdir / ".harness" / plan_name(round_num), None)
        if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_VERSION:
            return None
        return cls(workdir, plan)

    def _record(
        self,
        *,
        decision: str,
        tool: str,
        path: str | None,
        reason: str,
        scope_tier: str | None = None,
        patch_lines: int | None = None,
        expansion_reason: str | None = None,
    ) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        existing = 0
        if self.ledger_path.is_file():
            with self.ledger_path.open(encoding="utf-8") as handle:
                existing = sum(1 for _ in handle)
        payload: dict[str, Any] = {
            "sequence": existing + 1,
            "round": self.round_num,
            "decision": decision,
            "tool": tool,
            "path": path,
            "reason": reason,
        }
        if scope_tier is not None:
            payload["scope_tier"] = scope_tier
        if patch_lines is not None:
            payload["patch_lines"] = patch_lines
        if expansion_reason is not None:
            payload["expansion_reason"] = expansion_reason
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()

    def _path(self, tool_input: dict[str, Any]) -> tuple[Path, str] | None:
        raw = tool_input.get("path") or tool_input.get("file_path")
        if not isinstance(raw, str) or not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workdir / candidate
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(self.workdir).as_posix()
        except ValueError:
            return None
        return resolved, relative

    @staticmethod
    def _patch_pairs(tool: str, tool_input: dict[str, Any]) -> list[tuple[str, str]]:
        normalized = tool.lower()
        if normalized in {"write", "write_file"}:
            return [("", str(tool_input.get("content", "")))]
        if normalized in {"apply_patch", "edit"}:
            old = tool_input.get("old_text", tool_input.get("old_string", ""))
            new = tool_input.get("new_text", tool_input.get("new_string", ""))
            return [(str(old), str(new))]
        if normalized == "multiedit":
            output: list[tuple[str, str]] = []
            for item in tool_input.get("edits") or []:
                if isinstance(item, dict):
                    output.append(
                        (
                            str(item.get("old_string", item.get("old_text", ""))),
                            str(item.get("new_string", item.get("new_text", ""))),
                        )
                    )
            return output
        return []

    def _deny(
        self, tool: str, path: str | None, reason: str, *, patch_lines: int | None = None
    ) -> str:
        self._record(
            decision="deny",
            tool=tool,
            path=path,
            reason=reason,
            patch_lines=patch_lines,
        )
        return reason

    def check(self, tool: str, tool_input: dict[str, Any]) -> str | None:
        """Return a denial message, or ``None`` when the mutation is admissible."""
        normalized = tool.lower()
        if normalized in {"bash", "run_command"}:
            command = str(tool_input.get("command", ""))
            if _MUTATING_SHELL_RE.search(command):
                return self._deny(
                    tool,
                    None,
                    "Minimal-path mode routes source changes through mutation tools; "
                    "filesystem/package mutations through Bash are denied.",
                )
            return None

        mutation_tools = {"write", "write_file", "edit", "apply_patch", "multiedit"}
        if normalized not in mutation_tools:
            return None
        resolved_path = self._path(tool_input)
        if resolved_path is None:
            return None
        absolute, relative = resolved_path

        owned_artifacts = {
            f".harness/{plan_name(self.round_num)}",
            f".harness/edit_scope_round_{self.round_num}.json",
        }
        if relative in owned_artifacts:
            return self._deny(
                tool,
                relative,
                "This scope artifact is harness-owned and cannot be changed by the model.",
            )
        if not relative.startswith("frontend/"):
            return None
        if absolute.suffix.lower() not in CODE_EXTENSIONS:
            return None

        if normalized in {"write", "write_file"} and absolute.exists():
            return self._deny(
                tool,
                relative,
                "Existing frontend source cannot be overwritten in minimal-path mode; "
                "use one exact patch against the harness-selected source location.",
            )

        if relative in self.local_paths:
            tier = "local"
            expansion_reason = None
        elif relative in self.dependency_paths and any(
            target == relative and source in self.local_paths
            for source, target in self.dependency_edges
        ):
            tier = "dependency"
            expansion_reason = "recorded_dependency_edge"
        elif not absolute.exists() and any(
            Path(item).parent == Path(relative).parent
            for item in self.local_paths | self.dependency_paths
        ):
            tier = "new_local_file"
            expansion_reason = "same_directory_as_change_cone"
        else:
            candidates = sorted(self.local_paths | self.dependency_paths)
            return self._deny(
                tool,
                relative,
                "Source path is outside the harness change cone. Start with: "
                + (", ".join(candidates) if candidates else "no mechanically supported path"),
            )

        if relative not in self.touched_paths and len(self.touched_paths) >= self.max_touched_files:
            return self._deny(
                tool,
                relative,
                f"Minimal-path touched-file budget is {self.max_touched_files}; "
                "finish within the already authorized files.",
            )

        pairs = self._patch_pairs(tool, tool_input)
        patch_lines = sum(
            max(len(old.splitlines()) or 1, len(new.splitlines()) or 1)
            for old, new in pairs
        )
        if pairs and patch_lines > self.max_patch_lines:
            return self._deny(
                tool,
                relative,
                f"Patch exceeds the {self.max_patch_lines}-line patch-line budget; "
                "split it at the target source boundary.",
                patch_lines=patch_lines,
            )
        if pairs and absolute.is_file():
            content = absolute.read_text(encoding="utf-8", errors="replace")
            for old, _new in pairs:
                if not old or content.count(old) != 1:
                    return self._deny(
                        tool,
                        relative,
                        "Minimal-path exact patch must identify one unique source occurrence.",
                        patch_lines=patch_lines,
                    )

        self.touched_paths.add(relative)
        self._record(
            decision="allow",
            tool=tool,
            path=relative,
            reason="mutation stays inside the smallest mechanically supported cone",
            scope_tier=tier,
            patch_lines=patch_lines,
            expansion_reason=expansion_reason,
        )
        return None
