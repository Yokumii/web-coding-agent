from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

import httpx


WEBCOMPASS_EDIT_TYPES = (
    "Data Table", "Rich Text Editor", "Drag & Drop Interface", "Tree View",
    "Real-time Dashboard", "Infinite Scroll", "Async Form Validation",
    "File Upload with Progress", "Parallax Scrolling", "Page Transitions",
    "Particle Effects", "Skeleton Loading", "Shopping Cart", "User Authentication",
    "Multi-step Wizard", "Notification Center", "Dark Mode Toggle", "Accordion",
    "Modal Dialog", "Tooltip", "Breadcrumb Navigation", "Tabs",
    "Toast Notifications", "Star Rating", "Copy to Clipboard", "Back to Top",
    "Cookie Consent", "Responsive Navigation", "Sticky Header", "Search Autocomplete",
    "Image Lightbox", "Countdown Timer", "Color Picker", "Date Picker", "Carousel",
    "Keyboard Shortcuts", "Context Menu", "Lazy Loading Images", "Print Stylesheet",
    "Undo Redo",
)

WEBCOMPASS_REPAIR_TYPES = (
    "Occlusion", "Crowding", "Text Overlap", "Alignment", "Color Contrast", "Overflow",
    "Sizing Proportion", "Loss of Interactivity", "Semantic Error", "Nesting Error",
    "Missing Attributes",
)

AIR_CONSTRAINT_TYPES = (
    "Inclusion", "Exclusion", "Prior Condition", "Interaction Sequence",
    "Responsive State", "Accessibility", "Preservation", "Visual Integration",
)

_SOURCE_SUFFIXES = {".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".vue"}
_IGNORED_PARTS = {"node_modules", ".git", ".harness", "dist", "build"}


def load_seed_code(root: Path, *, max_files: int = 12, max_chars: int = 24_000) -> list[dict[str, str]]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"seed directory does not exist: {root}")
    selected: list[dict[str, str]] = []
    used = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or path.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        if any(part.startswith(".") or part in _IGNORED_PARTS for part in relative.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        remaining = max_chars - used
        if remaining <= 0 or len(selected) >= max_files:
            break
        text = text[:remaining]
        selected.append({"path": relative.as_posix(), "code": text})
        used += len(text)
    if not selected:
        raise ValueError(f"no supported frontend source files under {root}")
    return selected


def _string_list(value: Any, name: str, *, minimum: int, maximum: int) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} must contain {minimum}..{maximum} items")
    result = [str(item).strip() for item in value]
    if any(not item for item in result):
        raise ValueError(f"{name} contains an empty item")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} contains duplicate items")
    return result


def validate_initial_candidate(payload: dict[str, Any], expected_task_types: Iterable[str]) -> dict[str, Any]:
    expected = tuple(expected_task_types)
    if not expected or any(item not in WEBCOMPASS_EDIT_TYPES for item in expected):
        raise ValueError("expected task types must come from the WebCompass edit taxonomy")
    query = str(payload.get("query", "")).strip()
    if not 30 <= len(query) <= 1600:
        raise ValueError("query must contain 30..1600 characters")
    descriptions = payload.get("task_descriptions")
    if not isinstance(descriptions, list) or not descriptions:
        raise ValueError("task_descriptions must be a non-empty list")
    actual = tuple(str(item.get("task_type", "")).strip() for item in descriptions if isinstance(item, dict))
    if actual != expected:
        raise ValueError(f"task types must exactly match {expected}, got {actual}")
    for item in descriptions:
        if not str(item.get("description", "")).strip():
            raise ValueError("every task description must be non-empty")
    result = dict(payload)
    result["query"] = query
    result["task_descriptions"] = descriptions
    result["acceptance_criteria"] = _string_list(
        payload.get("acceptance_criteria"), "acceptance_criteria", minimum=2, maximum=8
    )
    result["preservation_requirements"] = _string_list(
        payload.get("preservation_requirements"), "preservation_requirements", minimum=1, maximum=6
    )
    return result


def validate_refinement(
    payload: dict[str, Any], *, original_query: str, allowed_evidence_ids: set[str]
) -> dict[str, Any]:
    selected_type = str(payload.get("selected_type", "")).strip()
    if selected_type not in AIR_CONSTRAINT_TYPES:
        raise ValueError(f"selected_type must be one of {AIR_CONSTRAINT_TYPES}")
    constraint = str(payload.get("constraint", "")).strip()
    if not 15 <= len(constraint) <= 300:
        raise ValueError("constraint must contain 15..300 characters")
    evidence_ids = _string_list(payload.get("evidence_ids"), "evidence_ids", minimum=1, maximum=4)
    unknown = sorted(set(evidence_ids) - allowed_evidence_ids)
    if unknown:
        raise ValueError(f"unknown evidence ids: {unknown}")
    refined_query = str(payload.get("refined_query", "")).strip()
    if len(refined_query) <= len(original_query) or len(refined_query) > 1900:
        raise ValueError("refined_query must add one bounded constraint to the original query")
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        raise ValueError("reason must be non-empty")
    return {
        "selected_type": selected_type,
        "constraint": constraint,
        "evidence_ids": evidence_ids,
        "refined_query": refined_query,
        "reason": reason,
    }


def build_repair_training_input(files: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return the code-only WebCompass repair input, deliberately dropping all diagnostics."""
    result: list[dict[str, str]] = []
    for item in files:
        path = str(item.get("path", "")).strip()
        code = item.get("code")
        if not path or not isinstance(code, str):
            raise ValueError("repair input files require path and code")
        result.append({"path": path, "code": code})
    if not result:
        raise ValueError("repair input must contain at least one code file")
    return result


def append_jsonl_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def parse_sse_content(lines: Iterable[str]) -> str:
    chunks: list[str] = []
    for line in lines:
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        event = json.loads(data)
        choices = event.get("choices") or []
        if not choices:
            continue
        content = (choices[0].get("delta") or {}).get("content")
        if content:
            chunks.append(str(content))
    return "".join(chunks)


def initial_generation_prompt(files: list[dict[str, str]], task_types: tuple[str, ...]) -> tuple[str, str]:
    system = (
        "You generate natural frontend edit requests grounded in an existing page. Return one JSON object only. "
        "The requested WebCompass task types are allowed design targets, not phrases that must appear verbatim. "
        "Do not request bug injection, a redesign, a framework migration, fake data, or backend services."
    )
    schema = {
        "query": "one cohesive natural user request",
        "task_descriptions": [
            {"task_type": task_types[0], "description": "one observable user-facing change"}
        ],
        "acceptance_criteria": ["browser-observable criterion", "preservation-aware criterion"],
        "preservation_requirements": ["existing behavior or content that must remain"],
    }
    user = (
        "Create an initial edit instruction by back-translating from this real frontend seed. "
        "Ground every requested behavior in elements or content that already exist. Keep the request concise, "
        "but make each task directly testable with mouse or keyboard in a browser. Output JSON matching this "
        f"shape: {json.dumps(schema, ensure_ascii=False)}\n"
        f"Required task types, in this exact order: {json.dumps(task_types)}\n"
        f"Seed files: {json.dumps(files, ensure_ascii=False)}"
    )
    return system, user


def refinement_prompt(
    *, original_query: str, evidence: list[dict[str, str]], files: list[dict[str, str]]
) -> tuple[str, str]:
    system = (
        "You are the judge in an AIR-style frontend instruction refinement loop. Return one JSON object only. "
        "Select exactly one failure-backed, user-visible constraint. Do not add implementation details, selectors, "
        "test jargon, defect labels, or unrelated features. Preserve the original request and merge the new "
        "constraint naturally."
    )
    schema = {
        "selected_type": "one allowed constraint type",
        "constraint": "one concise browser-observable constraint",
        "evidence_ids": ["one or more supplied failed evidence ids"],
        "refined_query": "the original request with exactly one constraint integrated",
        "reason": "why this is the most critical demonstrated gap",
    }
    user = (
        "Compare the implemented page evidence with the original request and seed. Choose the single most critical "
        "constraint that the real implementation failed to satisfy. The constraint must be supported by supplied "
        "evidence and must matter to a normal user. Output JSON matching this shape: "
        f"{json.dumps(schema, ensure_ascii=False)}\n"
        f"Allowed constraint types: {json.dumps(AIR_CONSTRAINT_TYPES)}\n"
        f"Original request: {original_query}\n"
        f"Failed evidence: {json.dumps(evidence, ensure_ascii=False)}\n"
        f"Seed files: {json.dumps(files, ensure_ascii=False)}"
    )
    return system, user


class OpenAICompatibleJSONClient:
    def __init__(
        self, *, base_url: str, api_key: str, model: str, timeout: float = 120,
        trust_env: bool = True, retries: int = 2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.trust_env = trust_env
        self.retries = retries

    @classmethod
    def from_env(cls) -> "OpenAICompatibleJSONClient":
        return cls(
            base_url=os.environ["AIR_API_BASE_URL"],
            api_key=os.environ["AIR_API_KEY"],
            model=os.getenv("AIR_MODEL", "deepseek-chat"),
            timeout=float(os.getenv("AIR_REQUEST_TIMEOUT", "120")),
            trust_env=os.getenv("AIR_TRUST_ENV", "1") != "0",
        )

    def complete(self, system: str, user: str) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system + " Your response must be valid json."},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.7,
            "max_tokens": 1800,
            "stream": False,
        }
        last_error: Exception | None = None
        with httpx.Client(
            timeout=httpx.Timeout(self.timeout),
            verify=os.getenv("SSL_NO_VERIFY") != "1",
            trust_env=self.trust_env,
        ) as client:
            for attempt in range(self.retries + 1):
                try:
                    response = client.post(
                        self.base_url + "/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                    response.raise_for_status()
                    body = response.json()
                    content = body["choices"][0]["message"]["content"]
                    if not str(content).strip():
                        raise ValueError("provider returned empty JSON content")
                    return json.loads(content), dict(body.get("usage") or {})
                except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    last_error = exc
                    if attempt >= self.retries:
                        break
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"JSON completion failed after {self.retries + 1} attempts: {last_error}")
