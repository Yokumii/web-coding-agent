"""One-artifact Planner path for explicit atomic Edit tasks."""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from src.agents.openai_runner import OpenAIHTTPClient
from src.agents.sdk_runner import AgentRunStats, build_agent_run_stats, run_sdk_agent
from src.config import HarnessConfig
from src.orchestration.accepted_tapes import accepted_obligation_summary
from src.orchestration.atomic_edit_plan import (
    ATOMIC_EDIT_PLAN_NAME,
    materialize_atomic_edit_compatibility_bundle,
    read_atomic_edit_plan,
    write_atomic_edit_plan,
)
from src.orchestration.edit_task_contract import read_edit_task_contract, chain_obligations
from src.orchestration.file_comm import FileComm
from src.orchestration.pricing import estimate_cost_usd
from src.orchestration.task_inputs import (
    openai_user_content,
    task_input_image_paths,
    task_input_prompt_context,
)
from src.prompts.edit_planner import ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT
from src.utils.llm_json import extract_json_object


_SELECTOR_ATTRIBUTE_RE = re.compile(
    r"\[\s*([\w:-]+)\s*=\s*(['\"])([^'\"]+)\2\s*\]",
    re.IGNORECASE,
)

_PLANNED_TARGET_SUFFIXES = {
    "button", "card", "control", "entry", "field", "item", "link", "list",
    "input", "option", "panel", "row", "section", "select", "target", "value", "heading",
    "title", "label", "error", "message", "validation",
}

_VISUAL_CHECK_CATEGORIES = {
    "appearance", "canvas", "image", "layout", "responsive", "style", "visual",
}
_VISUAL_ACTIONS = {"assert_computed_style", "set_viewport", "emulate_media"}
_VISUAL_INSTRUCTION_RE = re.compile(
    r"(?:"
    r"\b(?:appearance|background|border|box[- ]?shadow|canvas|color|colour|"
    r"desktop|font|gradient|icon|layout|logo|margin|mobile|opacity|"
    r"padding|pixel|radius|responsive|shadow|spacing|style|theme|transition|"
    r"typography|viewport|visual|width|height)\b"
    r"|外观|背景|边框|阴影|颜色|字体|渐变|图标|图片|图像|布局|间距|"
    r"内边距|外边距|圆角|响应式|移动端|桌面端|样式|主题|动画|视觉|像素"
    r")",
    re.IGNORECASE,
)


class _AddressableItemParser(HTMLParser):
    """Extract bounded stable item addresses from an accepted HTML baseline."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        data_name = " ".join(values.get("data-name", "").split())[:120]
        classes = [
            item for item in values.get("class", "").split()
            if re.fullmatch(r"[A-Za-z_][\w-]*", item)
        ]
        if not data_name or not classes or len(self.items) >= 30:
            return
        escaped = data_name.replace("\\", "\\\\").replace('"', '\\"')
        self.items.append({
            "tag": tag,
            "data_name": data_name,
            "selector": f'{tag}.{classes[0]}[data-name="{escaped}"]',
        })


class _AcceptedSourceControlParser(HTMLParser):
    """Extract stable controls from the immutable baseline for inline Edits."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.controls: list[dict[str, str]] = []
        self.surfaces: list[dict[str, str]] = []
        self.navigation: list[dict[str, str]] = []
        self._open_control: dict[str, Any] | None = None

    @staticmethod
    def _selector(tag: str, values: dict[str, str]) -> str:
        test_id = values.get("data-testid", "")
        identifier = values.get("id", "")
        if test_id:
            escaped = test_id.replace("\\", "\\\\").replace("'", "\\'")
            return f"[data-testid='{escaped}']"
        if identifier and re.fullmatch(r"[A-Za-z_][\w-]*", identifier):
            return f"#{identifier}"
        aria = values.get("aria-label", "")
        if aria:
            escaped = aria.replace("\\", "\\\\").replace("'", "\\'")
            return f"{tag}[aria-label='{escaped}']"
        return ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        selector = self._selector(tag, values)
        if tag in {"button", "input", "select", "textarea"} or (
            tag == "a" and values.get("href")
        ):
            if selector:
                control = {
                    "tag": tag,
                    "selector": selector,
                    **({"type": values["type"]} if values.get("type") else {}),
                    **(
                        {"aria_label": values["aria-label"][:120]}
                        if values.get("aria-label")
                        else {}
                    ),
                    "source": "accepted-baseline",
                    **({"aria_controls": values["aria-controls"]} if values.get("aria-controls") else {}),
                }
                self.controls.append(control)
                if tag in {"button", "a"}:
                    self._open_control = {"tag": tag, "control": control, "text": []}
        if tag == "a" and values.get("href"):
            self.navigation.append({"href": values["href"][:160]})
        if tag in {"main", "nav", "section", "form", "aside"} and selector:
            self.surfaces.append({"tag": tag, "selector": selector,
                                  **({"id": values["id"]} if values.get("id") else {}),
                                  **({"initially_hidden": True} if "hidden" in values else {})})

    def handle_data(self, data: str) -> None:
        if self._open_control is not None and str(data).strip():
            self._open_control["text"].append(" ".join(str(data).split()))

    def handle_endtag(self, tag: str) -> None:
        if self._open_control is None or tag != self._open_control["tag"]:
            return
        text = " ".join(self._open_control["text"]).strip()[:120]
        if text:
            self._open_control["control"]["text"] = text
        self._open_control = None


def _accepted_source_ui_contract(
    workdir: Path, baseline_commit: str
) -> dict[str, Any]:
    """Build bounded source hints from Git without reading a later candidate."""
    frontend = workdir / "frontend"
    if not baseline_commit or not (frontend / ".git").is_dir():
        return {}
    pages: list[dict[str, Any]] = []
    observed_controls: list[dict[str, str]] = []
    try:
        names = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", baseline_commit],
            cwd=frontend, check=True, text=True, capture_output=True,
        ).stdout.splitlines()
        for name in sorted(item for item in names if item.lower().endswith(".html"))[:20]:
            source = subprocess.run(
                ["git", "show", f"{baseline_commit}:{name}"],
                cwd=frontend, check=True, text=True, capture_output=True,
            ).stdout
            parser = _AcceptedSourceControlParser()
            parser.feed(source)
            route = "/" if name == "index.html" else "/" + name
            pages.append({
                "route": route,
                "navigation": parser.navigation[:24],
                "surfaces": parser.surfaces[:24],
                "controls": parser.controls[:24],
                "outputs": [],
                "addressable_items": [],
            })
            observed_controls.extend(parser.controls)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return {}
    return {
        "schema_version": "accepted-source-ui-contract-v1",
        "pages": pages,
        "observed_controls": observed_controls[:30],
        "observed_collections": [],
    }


def _add_baseline_addressable_items(
    workdir: Path, contract: dict[str, Any], baseline_commit: str
) -> dict[str, Any]:
    """Upgrade older manifests from the immutable accepted baseline, not the candidate."""
    output = json.loads(json.dumps(contract))
    pages = [item for item in output.get("pages") or [] if isinstance(item, dict)]
    if any(page.get("addressable_items") for page in pages):
        return output
    frontend = workdir / "frontend"
    if not baseline_commit or not (frontend / ".git").is_dir():
        return output
    try:
        names = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", baseline_commit],
            cwd=frontend, check=True, text=True, capture_output=True,
        ).stdout.splitlines()
        by_route = {str(page.get("route") or "/"): page for page in pages}
        for name in sorted(item for item in names if item.lower().endswith(".html"))[:20]:
            source = subprocess.run(
                ["git", "show", f"{baseline_commit}:{name}"],
                cwd=frontend, check=True, text=True, capture_output=True,
            ).stdout
            parser = _AddressableItemParser()
            parser.feed(source)
            route = "/" if name == "index.html" else "/" + name
            page = by_route.get(route)
            if page is not None:
                page["addressable_items"] = parser.items
    except (OSError, subprocess.CalledProcessError, ValueError):
        return output
    return output


def _read_source_ui_contract(workdir: Path) -> dict[str, Any]:
    try:
        seed_manifest = json.loads(
            (workdir / "seed_manifest.json").read_text(encoding="utf-8")
        )
        contract = seed_manifest.get("source_ui_contract") or {}
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(contract, dict):
        contract = {}
    if not contract:
        contract = _accepted_source_ui_contract(
            workdir, str(seed_manifest.get("baseline_commit") or "")
        )
    else:
        baseline = _accepted_source_ui_contract(workdir, str(seed_manifest.get("baseline_commit") or ""))
        by_route = {p.get("route", "/"): p for p in contract.get("pages", [])}
        for source_page in baseline.get("pages", []):
            page = by_route.get(source_page.get("route", "/"))
            if page is None:
                continue
            for key in ("controls", "surfaces"):
                existing = page.setdefault(key, [])
                for item in source_page.get(key, []):
                    if item.get("aria_controls") or item.get("initially_hidden"):
                        existing.append(item)
    return _add_baseline_addressable_items(
        workdir, contract, str(seed_manifest.get("baseline_commit") or "")
    )


def _ensure_source_surface_entry(payload: dict[str, Any], source_ui_contract: dict[str, Any]) -> dict[str, Any]:
    """Supply an unambiguous source-declared opener for an initial hidden view."""
    output = json.loads(json.dumps(payload))
    pages = source_ui_contract.get("pages", [])
    for check in output.get("checks", []):
        if re.search(r"__part(?:[2-9]|\d{2,})$", str(check.get("id", ""))):
            continue
        route = check.get("route", "/")
        page = next((p for p in pages if p.get("route") == route or
                     (route == "/index.html" and p.get("route") == "/")), {})
        actions = check.get("actions", [])
        for index, action in enumerate(actions):
            if action.get("action") in {"set_viewport", "scroll", "emulate_media"}:
                continue
            if action.get("action") != "assert_visible":
                break
            surfaces = [s for s in page.get("surfaces", []) if s.get("initially_hidden") and
                        action.get("selector") in {s.get("selector"), "#" + s.get("id", "")}]
            if len(surfaces) == 1:
                controls = {c["selector"] for c in page.get("controls", [])
                            if c.get("aria_controls") == surfaces[0].get("id")}
                if len(controls) == 1:
                    actions.insert(index, {"action": "click", "selector": next(iter(controls))})
            break
    return output


def _rewrite_observed_setup_control_selectors(
    payload: dict[str, Any], source_ui_contract: dict[str, Any]
) -> dict[str, Any]:
    """Map an invented setup test id onto one unique accepted-source control.

    Only action controls are rewritten. New result surfaces remain Planner-owned
    postconditions, while setup clicks/fills must address something that already
    exists in the accepted source.
    """
    output = json.loads(json.dumps(payload))
    controls = [
        item
        for item in source_ui_contract.get("observed_controls") or []
        if isinstance(item, dict) and str(item.get("selector") or "").strip()
    ]
    generic = {
        "button", "control", "field", "input", "link", "select", "toggle",
        "trigger",
    }

    def semantic_words(value: str) -> set[str]:
        return {
            word[:-1] if len(word) > 3 and word.endswith("s") else word
            for word in _normalized_words(value)
            if word not in generic
        }

    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        for action in check.get("actions") or []:
            if not isinstance(action, dict) or action.get("action") not in {
                "click", "fill", "hover", "key_press", "select_option",
                "set_input_files",
            }:
                continue
            selector = str(action.get("selector") or "")
            match = re.fullmatch(
                r"\[data-testid\s*=\s*(['\"])([^'\"]+)\1\]", selector
            )
            if not match:
                continue
            requested = semantic_words(match.group(2))
            if not requested:
                continue
            matches: list[tuple[int, str]] = []
            for control in controls:
                description = " ".join(
                    str(control.get(key) or "")
                    for key in ("selector", "text", "aria_label")
                )
                observed = semantic_words(description)
                if requested <= observed or observed <= requested:
                    matches.append(
                        (len(requested.symmetric_difference(observed)), str(control["selector"]))
                    )
            if matches:
                best_distance = min(distance for distance, _selector in matches)
                unique = list(dict.fromkeys(
                    candidate
                    for distance, candidate in matches
                    if distance == best_distance
                ))
                if len(unique) == 1:
                    action["selector"] = unique[0]
    return output


def _drop_harness_owned_preservation_presence_checks(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Remove weak duplicate preservation checks authored by the Planner.

    The DOM guard and accepted-tape replay own preservation.  A Planner check
    that merely asks an existing surface to be present/hidden can be false at a
    requested responsive viewport (for example, a desktop nav hidden on
    mobile), and invented test ids make that false failure even more likely.
    Stateful preservation checks remain because they exercise behavior that a
    structural guard cannot establish.
    """
    output = json.loads(json.dumps(payload))
    preservation_re = re.compile(
        r"\b(?:preserv(?:e|ed|ation|ing)|regression|sentinel|unchanged|"
        r"non[- ]?target)\b|保持|保留|不变|回归|非目标",
        re.IGNORECASE,
    )
    weak_assertions = {
        "assert_count",
        "assert_hidden",
        "assert_no_console_errors",
        "assert_visible",
    }
    kept: list[dict[str, Any]] = []
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        label = " ".join(
            str(check.get(key) or "")
            for key in ("id", "task", "expected_result", "category")
        )
        assertions = [
            action
            for action in check.get("actions") or []
            if isinstance(action, dict)
            and str(action.get("action") or "").startswith("assert_")
        ]
        if (
            preservation_re.search(label)
            and assertions
            and all(action.get("action") in weak_assertions for action in assertions)
        ):
            continue
        kept.append(check)
    output["checks"] = kept
    return output


def _slug_words(value: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _rewrite_observed_item_selectors(
    payload: dict[str, Any], source_ui_contract: dict[str, Any]
) -> dict[str, Any]:
    """Map invented per-item test ids onto stable addresses already in the source DOM."""
    items: list[tuple[str, str]] = []
    for page in source_ui_contract.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for item in page.get("addressable_items") or []:
            if not isinstance(item, dict):
                continue
            slug = _slug_words(str(item.get("data_name") or ""))
            selector = str(item.get("selector") or "").strip()
            if slug and selector:
                items.append((slug, selector))
    output = json.loads(json.dumps(payload))
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        for action in check.get("actions") or []:
            if not isinstance(action, dict):
                continue
            selector = str(action.get("selector") or "").strip()
            match = re.fullmatch(
                r"\[\s*data-testid\s*=\s*(['\"])([^'\"]+)\1\s*\]"
                r"((?:\[[^\]]+\]|:[A-Za-z_-][\w-]*(?:\([^)]*\))?)*)",
                selector,
                re.IGNORECASE,
            )
            if match is None:
                continue
            planned_slug = _slug_words(match.group(2))
            matches = {
                observed for slug, observed in items
                if planned_slug == slug or planned_slug.endswith("-" + slug)
            }
            if len(matches) == 1:
                action["selector"] = next(iter(matches)) + match.group(3)
    return output


def _ensure_direct_hash_setup(
    payload: dict[str, Any], *, user_prompt: str
) -> dict[str, Any]:
    """Turn a source-blind deep-link check into an executable same-page setup."""
    output = json.loads(json.dumps(payload))
    if not re.search(
        r"(?:\b(?:deep[ -]?link|direct link|hash fragment|url hash)\b|"
        r"深链|直接链接|哈希|片段)",
        user_prompt,
        re.IGNORECASE,
    ):
        return output
    literals = re.findall(r"#[A-Za-z0-9][A-Za-z0-9._~-]*", user_prompt)
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        if any(item.get("action") == "set_hash" for item in actions):
            continue
        data_name = ""
        for action in actions:
            selector = str(action.get("selector") or "")
            match = re.search(
                r"\[data-name\s*=\s*(['\"])([^'\"]+)\1\]", selector,
                re.IGNORECASE,
            )
            if match is not None:
                data_name = match.group(2)
                break
        value = "#" + _slug_words(data_name) if data_name else (literals[0] if literals else "")
        if value and value != "#":
            actions.insert(0, {"action": "set_hash", "value": value, "settle_ms": 150})
            check["actions"] = actions
    return output


def _normalize_hash_filtered_summary_flow(
    payload: dict[str, Any], *, user_prompt: str, source_ui_contract: dict[str, Any]
) -> dict[str, Any]:
    """Create a falsifiable public-state oracle for hash-filtered summaries.

    One accepted download first makes the global count non-zero. Selecting a
    different addressable driver must then reduce that driver's contextual
    count to zero. This avoids source-private storage fixtures and distinguishes
    real filtering from a panel that merely remains visible with global totals.
    """
    if not (
        re.search(r"\b(?:summary|statistics|totals?)\b", user_prompt, re.IGNORECASE)
        and re.search(
            r"\b(?:hash|deep[ -]?link|filter|matching|specific driver)\b",
            user_prompt,
            re.IGNORECASE,
        )
    ):
        return json.loads(json.dumps(payload))
    controls = {
        str(item.get("selector") or "")
        for item in source_ui_contract.get("observed_controls") or []
        if isinstance(item, dict)
    }
    download_control = next(
        (
            selector
            for selector in sorted(controls)
            if re.search(r"download", selector, re.IGNORECASE)
        ),
        "",
    )
    outputs = [
        str(item.get("selector") or "")
        for page in source_ui_contract.get("pages") or []
        if isinstance(page, dict)
        for item in page.get("outputs") or []
        if isinstance(item, dict)
    ]
    count_output = next(
        (
            selector
            for selector in outputs
            if "download" in selector.casefold()
            and re.search(r"(?:count|total)", selector, re.IGNORECASE)
        ),
        "",
    )
    volume_output = next(
        (selector for selector in outputs if "volume" in selector.casefold()),
        "",
    )
    if not download_control or not count_output:
        return json.loads(json.dumps(payload))

    output = json.loads(json.dumps(payload))
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        hash_index = next(
            (
                index
                for index, item in enumerate(actions)
                if item.get("action") == "set_hash"
            ),
            None,
        )
        if hash_index is None:
            continue
        if not any(
            str(item.get("selector") or "") == count_output
            for item in actions
        ):
            continue
        if not any(
            item.get("action") == "click"
            and "data-name" in str(item.get("selector") or "")
            for item in actions[hash_index + 1 :]
        ):
            continue
        if not any(
            item.get("action") == "click"
            and str(item.get("selector") or "") == download_control
            for item in actions[:hash_index]
        ):
            setup = [
                {
                    "action": "click",
                    "selector": download_control,
                    "settle_ms": 2_000,
                },
                {
                    "action": "assert_text",
                    "selector": count_output,
                    "value": "1",
                    "match": "exact",
                },
            ]
            actions[hash_index:hash_index] = setup
            hash_index += len(setup)
        contextual_count_asserted = False
        for item in actions[hash_index + 1 :]:
            selector = str(item.get("selector") or "")
            if item.get("action") == "assert_text" and selector == count_output:
                item["value"] = "0"
                item["match"] = "exact"
                contextual_count_asserted = True
            elif (
                volume_output
                and item.get("action") == "assert_text"
                and selector == volume_output
            ):
                item["value"] = "0"
                item["match"] = "contains"
        if not contextual_count_asserted:
            actions.append({
                "action": "assert_text",
                "selector": count_output,
                "value": "0",
                "match": "exact",
            })
        check["actions"] = actions
    return output


def _rewrite_observed_positional_selectors(
    payload: dict[str, Any], source_ui_contract: dict[str, Any]
) -> dict[str, Any]:
    """Prefer a unique observed href over a fragile nth-of-type selector."""
    navigation_by_text: dict[str, set[str]] = {}
    for page in source_ui_contract.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for item in page.get("navigation") or []:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").split()).casefold()
            href = str(item.get("href") or "").strip()
            if text and href:
                navigation_by_text.setdefault(text, set()).add(href)
    rewrites: dict[str, str] = {}
    for control in source_ui_contract.get("observed_controls") or []:
        if not isinstance(control, dict):
            continue
        selector = str(control.get("selector") or "").strip()
        text = " ".join(str(control.get("text") or "").split()).casefold()
        hrefs = navigation_by_text.get(text) or set()
        if re.fullmatch(r"a:nth-of-type\(\d+\)", selector) and len(hrefs) == 1:
            href = next(iter(hrefs)).replace("\\", "\\\\").replace("'", "\\'")
            rewrites[selector] = f"a[href='{href}']"
    grounding = json.dumps(payload, ensure_ascii=False).casefold()
    collection_candidates: list[tuple[int, str]] = []
    for item in source_ui_contract.get("observed_collections") or []:
        if not isinstance(item, dict):
            continue
        selector = str(item.get("selector") or "").strip()
        match = re.fullmatch(r"\.([A-Za-z_][\w-]*)", selector)
        if match is None:
            continue
        tokens = [token for token in re.split(r"[-_]", match.group(1).casefold()) if token]
        score = sum(1 for token in tokens if token in grounding)
        if score:
            collection_candidates.append((score, selector))
    best_collection = ""
    if collection_candidates:
        collection_candidates.sort(key=lambda item: (-item[0], item[1]))
        if (
            len(collection_candidates) == 1
            or collection_candidates[0][0] > collection_candidates[1][0]
        ):
            best_collection = collection_candidates[0][1]
    output = json.loads(json.dumps(payload))
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        for action in check.get("actions") or []:
            if not isinstance(action, dict):
                continue
            selector = str(action.get("selector") or "")
            if selector in rewrites:
                action["selector"] = rewrites[selector]
            elif best_collection and re.fullmatch(
                r"[^,]+>\s*[A-Za-z][\w-]*:first-child", selector.strip()
            ):
                action["selector"] = best_collection
    return output


def _drop_unowned_storage_assertions(
    payload: dict[str, Any], *, grounding: str
) -> dict[str, Any]:
    """Do not let a source-blind Planner prescribe an internal storage key."""
    output = json.loads(json.dumps(payload))
    folded = grounding.casefold()
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        retained: list[Any] = []
        for action in check.get("actions") or []:
            if not isinstance(action, dict):
                retained.append(action)
                continue
            if action.get("action") not in {
                "assert_storage_value",
                "set_storage_value",
            }:
                retained.append(action)
                continue
            key = str(action.get("key") or action.get("name") or "").strip()
            if key and key.casefold() in folded:
                retained.append(action)
        check["actions"] = retained
    return output


def _relax_unowned_hash_encodings(
    payload: dict[str, Any], *, grounding: str
) -> dict[str, Any]:
    """Verify requested hash state without inventing a full router encoding."""
    output = json.loads(json.dumps(payload))
    folded_grounding = grounding.casefold()
    generic = {"all", "filter", "gallery", "hash", "route", "type", "view"}
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        for action in check.get("actions") or []:
            if not isinstance(action, dict) or action.get("action") != "assert_hash":
                continue
            expected = str(action.get("value") or "")
            if not expected or expected.casefold() in folded_grounding:
                continue
            tokens = sorted(
                {
                    token
                    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]*", expected)
                    if token.casefold() not in generic
                    and token.casefold() in folded_grounding
                },
                key=len,
                reverse=True,
            )
            if tokens:
                action["value"] = tokens[0]
                action["match"] = "contains"
    return output


def _preserve_observed_hash_route_for_default_filter(
    payload: dict[str, Any], *, user_prompt: str, source_ui_contract: dict[str, Any]
) -> dict[str, Any]:
    """Keep an existing hash-routed view active when a filter returns to All."""
    if re.search(
        r"(?:clear|empty|remove|reset)\s+(?:the\s+)?(?:url\s+)?hash|"
        r"清空(?:URL)?哈希|移除(?:URL)?哈希",
        user_prompt,
        re.IGNORECASE,
    ):
        return json.loads(json.dumps(payload))
    observed_hash_routes = {
        str(item.get("href") or "")
        for page in source_ui_contract.get("pages") or []
        if isinstance(page, dict)
        for item in page.get("navigation") or []
        if isinstance(item, dict) and str(item.get("href") or "").startswith("#")
    }
    observed_surface_ids = {
        str(item.get("id") or "")
        for page in source_ui_contract.get("pages") or []
        if isinstance(page, dict)
        for item in page.get("surfaces") or []
        if isinstance(item, dict)
    }
    output = json.loads(json.dumps(payload))
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        active_hash_route = ""
        selected_all = False
        normalized: list[dict[str, Any]] = []
        for action in actions:
            if action.get("action") == "click":
                match = re.fullmatch(
                    r"a\[href=(['\"])(#[^'\"]+)\1\]",
                    str(action.get("selector") or "").strip(),
                )
                if match and match.group(2) in observed_hash_routes:
                    active_hash_route = match.group(2)
            if action.get("action") == "select_option":
                selected_all = str(action.get("value") or "").casefold() == "all"
            if (
                action.get("action") == "assert_hash"
                and action.get("value") == ""
                and selected_all
                and active_hash_route
            ):
                normalized.append({
                    "action": "assert_hash",
                    "value": active_hash_route,
                    "match": "exact",
                })
                surface_selector = f"#view-{active_hash_route[1:]}"
                if surface_selector[1:] in observed_surface_ids:
                    normalized.append(
                        {"action": "assert_visible", "selector": surface_selector}
                    )
                continue
            normalized.append(action)
        check["actions"] = normalized
    return output


def _drop_unowned_source_anchors(
    payload: dict[str, Any], *, grounding: str
) -> dict[str, Any]:
    """Keep source anchors as observations, never as names for new targets."""
    output = json.loads(json.dumps(payload))
    folded = grounding.casefold()
    output["source_anchors"] = [
        anchor
        for anchor in output.get("source_anchors") or []
        if str(anchor).strip().casefold() in folded
        or str(anchor).strip().lstrip("#.").casefold() in folded
    ]
    return output


def _downgrade_unowned_text_assertions(
    payload: dict[str, Any], *, grounding: str
) -> dict[str, Any]:
    """Do not turn an unrelated observed label into a requested output literal."""
    output = json.loads(json.dumps(payload))
    folded = grounding.casefold()
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        normalized: list[Any] = []
        for action in check.get("actions") or []:
            if (
                isinstance(action, dict)
                and action.get("action") == "assert_text"
                and str(action.get("value") or "").strip().casefold() not in folded
            ):
                selector = re.sub(
                    r":nth-(?:child|of-type)\(1\)$",
                    "",
                    str(action.get("selector") or ""),
                )
                normalized.append(
                    {
                        "action": "assert_visible",
                        "selector": selector,
                        **(
                            {"settle_ms": action["settle_ms"]}
                            if "settle_ms" in action
                            else {}
                        ),
                    }
                )
            else:
                normalized.append(action)
        check["actions"] = normalized
    return output


def _normalize_unspecified_value_assertions(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn runtime-value placeholders into evidence-bearing assertions."""
    output = json.loads(json.dumps(payload))
    explicit_nonempty_attribute_selectors = {
        str(action.get("selector") or "")
        for check in output.get("checks") or []
        if isinstance(check, dict)
        for action in check.get("actions") or []
        if isinstance(action, dict)
        and action.get("action") == "assert_attribute"
        and (
            action.get("match") == "nonempty"
            or (
                action.get("name") == "match"
                and action.get("value") == "nonempty"
            )
        )
        and str(action.get("selector") or "")
    }
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        for action in check.get("actions") or []:
            if not isinstance(action, dict):
                continue
            kind = action.get("action")
            if kind == "assert_text" and action.get("value") == "":
                # Empty substring containment is vacuous; planners use this as
                # a runtime text placeholder.
                action["match"] = "nonempty"
            elif (
                kind == "assert_attribute"
                and "value" not in action
                and action.get("match") == "nonempty"
            ):
                action["value"] = ""
                action["match"] = "nonempty"
            elif (
                kind == "assert_attribute"
                and action.get("value") == ""
                and action.get("match") in {"contains", "nonempty"}
            ):
                action["match"] = "nonempty"
            elif (
                kind == "assert_attribute"
                and action.get("value") == ""
                and "match" not in action
                and str(action.get("selector") or "")
                in explicit_nonempty_attribute_selectors
            ):
                # The same field is empty in the initial state and becomes
                # non-empty after an event. Browser evidence otherwise treats
                # bare empty values as a runtime non-empty placeholder.
                action["match"] = "exact"
    return output


def _drop_duplicate_pre_effect_assertions(payload: dict[str, Any]) -> dict[str, Any]:
    """Do not require an effect before the action that is meant to create it.

    OpenAI-compatible planners sometimes duplicate the same target assertion on
    both sides of a state-producing action such as drag-and-drop.  The first
    copy makes a fresh accepted Seed impossible to pass, while the identical
    post-action copy already verifies the requested effect.  Remove only exact
    duplicate assertions that straddle the effect; unrelated source
    preconditions and container waits remain intact.
    """
    output = json.loads(json.dumps(payload))
    effect_actions = {
        "drag_and_drop", "fill", "key_press", "select_option",
        "set_input_files", "set_storage_value",
    }
    assertion_actions = {
        "assert_aria", "assert_attribute", "assert_computed_style",
        "assert_count", "assert_focus", "assert_hidden", "assert_in_view",
        "assert_property", "assert_scroll", "assert_storage_value",
        "assert_text", "assert_url", "assert_value", "assert_visible",
        "wait_for",
    }

    def produced_collection_wait(
        action: dict[str, Any], later_actions: list[dict[str, Any]]
    ) -> bool:
        if action.get("action") not in {"assert_visible", "wait_for"}:
            return False
        selector = str(action.get("selector") or "").casefold()
        if not re.search(r"history[^'\"]*(?:list|container)|(?:list|container)[^'\"]*history", selector):
            return False
        return any(
            later.get("action") in assertion_actions
            and "history" in str(later.get("selector") or "").casefold()
            and re.search(r"history[^'\"]*(?:item|entry)|(?:item|entry)[^'\"]*history", str(later.get("selector") or "").casefold())
            for later in later_actions
        )

    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        produced_wait_indices: set[int] = set()
        for index, action in enumerate(actions):
            if action.get("action") not in {"assert_visible", "wait_for"}:
                continue
            later_effect_index = next(
                (
                    later_index
                    for later_index in range(index + 1, len(actions))
                    if actions[later_index].get("action") in effect_actions | {"click"}
                ),
                None,
            )
            if (
                later_effect_index is not None
                and produced_collection_wait(
                    action, actions[later_effect_index + 1 :]
                )
            ):
                produced_wait_indices.add(index)
        effect_index = next(
            (
                index for index, action in enumerate(actions)
                if action.get("action") in effect_actions
            ),
            None,
        )
        if effect_index is None:
            check["actions"] = [
                action
                for index, action in enumerate(actions)
                if index not in produced_wait_indices
            ]
            continue
        post_effect = {
            json.dumps(action, ensure_ascii=False, sort_keys=True)
            for action in actions[effect_index + 1 :]
            if action.get("action") in assertion_actions
        }
        check["actions"] = [
            action
            for index, action in enumerate(actions)
            if not (
                index in produced_wait_indices
                or (
                    index < effect_index
                    and action.get("action") in assertion_actions
                    and json.dumps(action, ensure_ascii=False, sort_keys=True) in post_effect
                )
            )
        ]
    return output


def _drop_adjacent_duplicate_assertions(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove no-op repeated assertions without crossing a state transition."""
    output = json.loads(json.dumps(payload))
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        normalized: list[Any] = []
        previous_key = ""
        for action in check.get("actions") or []:
            key = (
                json.dumps(action, ensure_ascii=False, sort_keys=True)
                if isinstance(action, dict)
                and str(action.get("action") or "").startswith("assert_")
                else ""
            )
            if key and key == previous_key:
                continue
            normalized.append(action)
            previous_key = key
        check["actions"] = normalized
    return output


def _ensure_explicit_reorder_history_fields(
    payload: dict[str, Any], *, user_prompt: str
) -> dict[str, Any]:
    """Keep explicit before/after position fields observable in reorder checks.

    A source-blind planner can correctly exercise drag-and-drop yet omit one of
    the fields the instruction explicitly requires the durable event to record.
    Add only the two missing, instruction-grounded field assertions to the
    existing post-drag history item; do not infer a history UI when the planner
    did not already create one.
    """
    if not (
        re.search(r"\b(?:previous|prior|old)\b[^.\n]{0,80}\b(?:new|next)\b[^.\n]{0,40}\bposition", user_prompt, re.IGNORECASE)
        or re.search(r"\bfrom\s*index\b[^.\n]{0,80}\bto\s*index\b", user_prompt, re.IGNORECASE)
    ):
        return json.loads(json.dumps(payload))

    output = json.loads(json.dumps(payload))
    field_specs = (
        ("previous-position", ("previous-position", "prior-position", "from-index", "from-position")),
        ("new-position", ("new-position", "next-position", "to-index", "to-position")),
    )
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        for action in actions:
            selector = str(action.get("selector") or "")
            if "data-testid" not in selector:
                continue

            def canonicalize_field(match: re.Match[str]) -> str:
                quote, value = match.group(1), match.group(2)
                words = set(_normalized_words(value))
                position_word = bool(words & {"pos", "position", "index"})
                if position_word and words & {"prev", "previous", "prior", "old", "from"}:
                    return f"[data-testid={quote}previous-position{quote}]"
                if position_word and words & {"new", "next", "to"}:
                    return f"[data-testid={quote}new-position{quote}]"
                return match.group(0)

            action["selector"] = re.sub(
                r"\[data-testid\s*=\s*(['\"])([^'\"]+)\1\]",
                canonicalize_field,
                selector,
                flags=re.IGNORECASE,
            )
        drag_index = next(
            (index for index, item in enumerate(actions) if item.get("action") == "drag_and_drop"),
            None,
        )
        if drag_index is None:
            continue
        post_drag = actions[drag_index + 1 :]
        history_item = ""
        for action in post_drag:
            selector = str(action.get("selector") or "")
            match = re.search(
                r"(\[data-testid=(['\"])[^'\"]*(?:history-item|history-entry)[^'\"]*\2\])",
                selector,
                re.IGNORECASE,
            )
            if match:
                history_item = match.group(1)
                break
        if not history_item:
            continue
        serialized = json.dumps(post_drag, ensure_ascii=False).casefold()
        for testid, aliases in field_specs:
            if any(alias in serialized for alias in aliases):
                continue
            actions.append(
                {
                    "action": "assert_attribute",
                    "selector": (
                        f"{history_item}:first-child "
                        f"[data-testid='{testid}']"
                    ),
                    "name": "data-value",
                    "value": "",
                    "match": "nonempty",
                }
            )
        check["actions"] = actions
    return output


def _drop_incidental_summary_dependency_waits(
    payload: dict[str, Any], *, user_prompt: str
) -> dict[str, Any]:
    """Do not make an empty source history box a precondition for its summary.

    The summary target assertions already wait for the requested new surface.
    Requiring a prior history list to have positive geometry rejects valid empty
    states and sends Repair toward unrelated CSS.
    """
    output = json.loads(json.dumps(payload))
    if not (
        re.search(r"\bsummary\b", user_prompt, re.IGNORECASE)
        and re.search(r"\bhistor(?:y|ies)\b", user_prompt, re.IGNORECASE)
    ):
        return output
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        if not any(
            "summary" in str(item.get("selector") or "").casefold()
            for item in actions
        ):
            continue
        check["actions"] = [
            item
            for item in actions
            if not (
                item.get("action") == "wait_for"
                and re.search(
                    r"history[^'\"]*(?:list|container)|(?:list|container)[^'\"]*history",
                    str(item.get("selector") or ""),
                    re.IGNORECASE,
                )
            )
        ]
    return output


def _normalize_named_view_flows(
    payload: dict[str, Any], *, user_prompt: str
) -> dict[str, Any]:
    """Make source-blind named-view checks self-contained and identity-stable."""
    output = json.loads(json.dumps(payload))
    if not re.search(
        r"\b(?:named|saved)\s+views?\b|\bsave[^.\n]{0,80}\bviews?\b",
        user_prompt,
        re.IGNORECASE,
    ):
        return output

    template: dict[str, str] = {}
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        name_action = next(
            (
                item
                for item in actions
                if item.get("action") == "fill"
                and "view" in str(item.get("selector") or "").casefold()
                and "name" in str(item.get("selector") or "").casefold()
                and str(item.get("value") or "").strip()
            ),
            None,
        )
        save_action = next(
            (
                item
                for item in actions
                if item.get("action") == "click"
                and "save" in str(item.get("selector") or "").casefold()
                and "view" in str(item.get("selector") or "").casefold()
            ),
            None,
        )
        filter_action = next(
            (
                item
                for item in actions
                if item.get("action") == "select_option"
                and "filter" in str(item.get("selector") or "").casefold()
            ),
            None,
        )
        if name_action and save_action and filter_action:
            template = {
                "name_selector": str(name_action["selector"]),
                "name": str(name_action["value"]),
                "save_selector": str(save_action["selector"]),
                "filter_selector": str(filter_action["selector"]),
                "filter_value": str(filter_action["value"]),
            }
            break
    if not template:
        return output

    dynamic_item = re.compile(
        r"^\[data-testid=(['\"])saved-view-item-([^'\"]+)\1\]$",
        re.IGNORECASE,
    )
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        identity = " ".join(
            [
                str(check.get("id") or ""),
                str(check.get("task") or ""),
                str(check.get("expected_result") or ""),
            ]
        ).casefold()
        local_save_index = next(
            (
                index
                for index, item in enumerate(actions)
                if item.get("action") == "click"
                and str(item.get("selector") or "") == template["save_selector"]
            ),
            None,
        )
        if (
            local_save_index is not None
            and "persist" in identity
            and not any(
                item.get("action") in {"assert_text", "assert_visible"}
                and "saved-view" in str(item.get("selector") or "").casefold()
                for item in actions[local_save_index + 1 :]
            )
        ):
            actions = [
                item
                for item in actions
                if item.get("action") not in {
                    "assert_storage_value", "set_storage_value"
                }
            ]
            if not any(item.get("action") == "reload" for item in actions):
                actions.append({"action": "reload"})
            leading_navigation = next(
                (
                    item
                    for item in actions[:local_save_index]
                    if item.get("action") == "click"
                    and str(item.get("selector") or "").startswith("a[href=")
                ),
                None,
            )
            if leading_navigation is not None:
                actions.append(json.loads(json.dumps(leading_navigation)))
            actions.extend(
                [
                    {
                        "action": "assert_visible",
                        "selector": "[data-testid='saved-views-list']",
                    },
                    {
                        "action": "assert_text",
                        "selector": "[data-testid='saved-views-list']",
                        "value": template["name"],
                        "match": "contains",
                    },
                ]
            )
        consumer_index: int | None = None
        for index, action in enumerate(actions):
            selector = str(action.get("selector") or "")
            match = dynamic_item.fullmatch(selector)
            if not match:
                continue
            candidate_name = match.group(2).replace("-", " ")
            chosen_name = (
                candidate_name
                if candidate_name.casefold() in user_prompt.casefold()
                else template["name"]
            )
            escaped_name = chosen_name.replace("\\", "\\\\").replace('"', '\\"')
            action["selector"] = (
                "[data-testid='saved-view-item']"
                f'[data-name="{escaped_name}"]'
            )
            consumer_index = index
        if consumer_index is None:
            check["actions"] = actions
            continue

        has_local_producer = any(
            item.get("action") == "fill"
            and str(item.get("selector") or "") == template["name_selector"]
            for item in actions[:consumer_index]
        )
        producer_filter = next(
            (
                str(item.get("value") or "")
                for item in actions[:consumer_index]
                if item.get("action") == "select_option"
                and str(item.get("selector") or "") == template["filter_selector"]
            ),
            "",
        )
        if not has_local_producer:
            later_expected_filter = next(
                (
                    str(item.get("value"))
                    for item in actions[consumer_index + 1 :]
                    if item.get("action") == "assert_value"
                    and str(item.get("selector") or "") == template["filter_selector"]
                ),
                "",
            )
            desired_filter = (
                "All"
                if re.search(r"\b(?:layout|order)\b", identity)
                else later_expected_filter or template["filter_value"]
            )
            setup = [
                {
                    "action": "select_option",
                    "selector": template["filter_selector"],
                    "value": desired_filter,
                },
                {
                    "action": "fill",
                    "selector": template["name_selector"],
                    "value": template["name"],
                },
                {"action": "click", "selector": template["save_selector"]},
            ]
            insert_at = 1 if actions and actions[0].get("action") == "click" else 0
            actions[insert_at:insert_at] = setup
            consumer_index += len(setup)
            producer_filter = desired_filter

        if re.search(r"\b(?:layout|order)\b", identity):
            snapshot_name = "saved-view-layout-first-id"
            for index, action in enumerate(actions[:consumer_index]):
                if (
                    action.get("action") == "assert_attribute"
                    and action.get("name") == "data-id"
                    and "artifact-card" in str(action.get("selector") or "")
                ):
                    actions[index] = {
                        "action": "capture_attribute",
                        "selector": str(action["selector"]),
                        "name": "data-id",
                        "snapshot": snapshot_name,
                    }
                    break
            if not any(
                item.get("action") == "capture_attribute"
                for item in actions[:consumer_index]
            ):
                capture_index = next(
                    (
                        index
                        for index, item in enumerate(actions[:consumer_index])
                        if item.get("action") == "assert_visible"
                        and "gallery-grid" in str(item.get("selector") or "")
                    ),
                    consumer_index,
                )
                capture = {
                    "action": "capture_attribute",
                    "selector": ".artifact-card:nth-child(1)",
                    "name": "data-id",
                    "snapshot": snapshot_name,
                }
                if capture_index < consumer_index:
                    actions[capture_index] = capture
                else:
                    actions.insert(consumer_index, capture)
                    consumer_index += 1
            if producer_filter:
                for item in actions[consumer_index + 1 :]:
                    if (
                        item.get("action") == "assert_value"
                        and str(item.get("selector") or "")
                        == template["filter_selector"]
                    ):
                        item["value"] = producer_filter
            if any(
                item.get("action") == "capture_attribute"
                for item in actions[:consumer_index]
            ):
                actions.append(
                    {
                        "action": "assert_attribute",
                        "selector": ".artifact-card:nth-child(1)",
                        "name": "data-id",
                        "snapshot": snapshot_name,
                    }
                )
        check["actions"] = actions
    return output


def _stabilize_persistence_checks(payload: dict[str, Any]) -> dict[str, Any]:
    """Wait for an observable state transition before testing its persistence.

    A generic ``wait_for`` can already be satisfied by an older list item. If
    the plan later reasserts a previously observed value after reload, waiting
    for that exact value before reload proves the write completed and avoids a
    spurious Repair caused by racing an accepted async interaction.
    """
    output = json.loads(json.dumps(payload))
    previously_observed_selectors: set[str] = set()
    value_actions = {
        "assert_aria",
        "assert_attribute",
        "assert_computed_style",
        "assert_count",
        "assert_property",
        "assert_storage_value",
        "assert_text",
        "assert_value",
    }
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        reload_index = next(
            (index for index, item in enumerate(actions) if item.get("action") == "reload"),
            None,
        )
        if reload_index is not None and any(
            item.get("action") in {"click", "fill", "key_press", "select_option", "set_storage_value"}
            for item in actions[:reload_index]
        ):
            convergence = next(
                (
                    item
                    for item in actions[reload_index + 1 :]
                    if item.get("action") in value_actions
                    and str(item.get("selector") or "") in previously_observed_selectors
                ),
                None,
            )
            if convergence is not None and not (
                reload_index > 0 and actions[reload_index - 1] == convergence
            ):
                actions.insert(reload_index, json.loads(json.dumps(convergence)))
                check["actions"] = actions
        previously_observed_selectors.update(
            str(item.get("selector") or "")
            for item in actions
            if item.get("action") in value_actions and item.get("selector")
        )
    return output


def _normalize_bounded_planner_actions(payload: dict[str, Any]) -> dict[str, Any]:
    """Map common model spellings onto the harness-owned typed action set."""
    output = json.loads(json.dumps(payload))
    known_attribute_by_selector: dict[str, str] = {}
    for candidate_check in output.get("checks") or []:
        if not isinstance(candidate_check, dict):
            continue
        for candidate in candidate_check.get("actions") or []:
            if not isinstance(candidate, dict) or candidate.get("action") != "assert_attribute":
                continue
            name = str(candidate.get("name") or candidate.get("attribute") or "")
            selector = str(candidate.get("selector") or "")
            if selector and name and name not in {"match", "nonempty"}:
                known_attribute_by_selector.setdefault(selector, name)
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        normalized: list[Any] = []
        viewport_prepared = any(
            isinstance(item, dict) and item.get("action") == "set_viewport"
            for item in check.get("actions") or []
        )
        for action in check.get("actions") or []:
            if not isinstance(action, dict):
                normalized.append(action)
                continue
            if action.get("action_detail") in ({}, None):
                action.pop("action_detail", None)
            kind = action.get("action")
            if kind == "drag_and_drop":
                for alias, canonical in (("source", "source_selector"), ("destination", "target_selector"), ("target", "target_selector")):
                    if alias in action and (canonical not in action or action[canonical] == action[alias]):
                        action[canonical] = action.pop(alias)
            if (
                kind == "assert_attribute"
                and action.get("name") == "match"
                and action.get("value") in {"exact", "contains", "regex", "nonempty"}
                and str(action.get("selector") or "") in known_attribute_by_selector
            ):
                requested_match = str(action["value"])
                action["name"] = known_attribute_by_selector[
                    str(action.get("selector") or "")
                ]
                action["value"] = ""
                action["match"] = requested_match
            if kind == "key_press" and "key" not in action and "value" in action:
                action["key"] = action.pop("value")
            if kind == "scroll":
                scroll_y = int(action.get("y") or 600)
                if scroll_y > 0 and not viewport_prepared:
                    normalized.append(
                        {"action": "set_viewport", "width": 1280, "height": 400}
                    )
                    viewport_prepared = True
                normalized.append({
                    "action": "scroll",
                    "y": scroll_y,
                    **({"settle_ms": action["settle_ms"]} if "settle_ms" in action else {}),
                })
                continue
            if kind == "assert_attribute" and action.get("name") == "value":
                normalized.append({
                    "action": "assert_value",
                    "selector": action.get("selector"),
                    "value": action.get("value", ""),
                    **({"settle_ms": action["settle_ms"]} if "settle_ms" in action else {}),
                })
                continue
            if (
                kind == "assert_attribute"
                and action.get("name") == "class"
                and re.fullmatch(r"[A-Za-z_][\w-]*", str(action.get("value") or ""))
            ):
                normalized.append({
                    "action": "assert_visible",
                    "selector": (
                        f"{action.get('selector')}[class~='{action.get('value')}']"
                    ),
                    **({"settle_ms": action["settle_ms"]} if "settle_ms" in action else {}),
                })
                continue
            if kind == "assert_property" and action.get("name") == "scrollTop":
                normalized.append({
                    "action": "assert_scroll",
                    "y": int(action.get("value") or 0),
                    "tolerance": 2,
                })
                continue
            if kind == "assert_property" and action.get("name") == "isInView":
                normalized.append({
                    "action": "assert_in_view",
                    "selector": action.get("selector"),
                    **({"settle_ms": action["settle_ms"]} if "settle_ms" in action else {}),
                })
                continue
            if (
                kind == "assert_property"
                and action.get("name") == "innerHTML"
                and str(action.get("value") or "").casefold() == "nonempty"
            ):
                normalized.append({
                    "action": "assert_visible",
                    "selector": action.get("selector"),
                    **({"settle_ms": action["settle_ms"]} if "settle_ms" in action else {}),
                })
                continue
            if kind == "assert_property":
                action.pop("match", None)
            if kind == "assert_hash" and action.get("value") == "":
                action = {
                    "action": "assert_hash",
                    "value": "",
                    "match": "nonempty",
                    **({"settle_ms": action["settle_ms"]} if "settle_ms" in action else {}),
                }
            if kind == "assert_url" and action.get("value") == "":
                action = {
                    "action": "assert_hash",
                    "value": "",
                    "match": "nonempty",
                    **({"settle_ms": action["settle_ms"]} if "settle_ms" in action else {}),
                }
            normalized.append(action)
        check["actions"] = normalized
    return output


def _normalize_instruction_context_actions(
    payload: dict[str, Any], *, user_prompt: str
) -> dict[str, Any]:
    """Materialize explicit viewport and ordinal semantics from the instruction."""
    output = json.loads(json.dumps(payload))
    mobile_requested = bool(
        re.search(r"\bmobile\b|移动端|手机端", user_prompt, re.IGNORECASE)
    )
    first_link_requested = bool(
        re.search(r"\bfirst\s+link\b|第一个链接", user_prompt, re.IGNORECASE)
    )
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        if mobile_requested and not any(
            item.get("action") == "set_viewport" for item in actions
        ):
            actions.insert(0, {
                "action": "set_viewport",
                "width": 390,
                "height": 844,
            })
        if first_link_requested:
            for action in actions:
                selector = str(action.get("selector") or "")
                if (
                    action.get("action") == "assert_focus"
                    and re.search(r"\.main-nav\s+a(?:\[|$)", selector)
                    and not re.search(r":(?:first|nth)-", selector)
                ):
                    action["selector"] = re.sub(
                        r"\.main-nav\s+a(?:\[[^\]]+\])?",
                        ".main-nav li:first-child a",
                        selector,
                        count=1,
                    )
        check["actions"] = actions
    return output


def _normalize_scroll_restore_checks(
    payload: dict[str, Any], *, user_prompt: str
) -> dict[str, Any]:
    """Compare restored scroll with the real click-time state, not a brittle literal.

    Playwright may scroll a partially visible card before dispatching its click.
    Capturing at the click event gives the exact gallery state the application
    receives, preventing a correct implementation from entering repeated Repair
    rounds because the planner's earlier numeric scroll command was adjusted by
    normal browser actionability behavior.
    """
    output = json.loads(json.dumps(payload))
    if not re.search(
        r"(?:restore|return)[^.\n]{0,200}scroll|scroll[^.\n]{0,200}(?:restore|return)|"
        r"滚动(?:位置|状态)|恢复[^。\n]{0,40}滚动",
        user_prompt,
        re.IGNORECASE,
    ):
        return output
    for check_index, check in enumerate(output.get("checks") or [], start=1):
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        scroll_index = next(
            (index for index, item in enumerate(actions) if item.get("action") == "scroll"),
            None,
        )
        assertion_index = next(
            (
                index
                for index, item in enumerate(actions)
                if index > (scroll_index if scroll_index is not None else len(actions))
                and item.get("action") == "assert_scroll"
            ),
            None,
        )
        if scroll_index is None or assertion_index is None:
            continue
        entry_click_index = next(
            (
                index
                for index in range(scroll_index + 1, assertion_index)
                if actions[index].get("action") == "click"
            ),
            None,
        )
        if entry_click_index is None:
            continue
        snapshot = f"scroll-before-detail-{check_index}"
        actions[entry_click_index]["capture_scroll_as"] = snapshot
        tolerance = actions[assertion_index].get("tolerance", 2)
        actions[assertion_index] = {
            "action": "assert_scroll",
            "snapshot": snapshot,
            "tolerance": tolerance,
        }
        check["actions"] = actions
    return output


def _ensure_addressability_assertion(
    payload: dict[str, Any], *, user_prompt: str
) -> dict[str, Any]:
    """Require observable URL state before accepting direct-link persistence."""
    output = json.loads(json.dumps(payload))
    if not re.search(
        r"\b(?:url|hash|direct\s+link|addressable)\b|直链|地址栏|链接直达",
        user_prompt,
        re.IGNORECASE,
    ):
        return output
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        reload_index = next(
            (index for index, item in enumerate(actions) if item.get("action") == "reload"),
            None,
        )
        if reload_index is None or any(
            item.get("action") in {"assert_hash", "assert_url"}
            for item in actions[:reload_index]
        ):
            continue
        actions.insert(reload_index, {
            "action": "assert_hash",
            "value": "",
            "match": "nonempty",
        })
        check["actions"] = actions
    return output


def _normalize_observed_select_options(
    payload: dict[str, Any], *, source_ui_contract: dict[str, Any], user_prompt: str
) -> dict[str, Any]:
    """Replace invented test fixtures with a real option from the accepted source."""
    output = json.loads(json.dumps(payload))
    values_by_selector: dict[str, list[str]] = {}
    for page in source_ui_contract.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for control in page.get("controls") or []:
            if not isinstance(control, dict) or control.get("tag") != "select":
                continue
            values = [
                str(item.get("value"))
                for item in control.get("options") or []
                if isinstance(item, dict) and str(item.get("value") or "").strip()
            ]
            selectors = [control.get("selector"), *(control.get("selector_aliases") or [])]
            for selector in selectors:
                if isinstance(selector, str) and selector.strip():
                    values_by_selector[selector] = values
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        for action in check.get("actions") or []:
            if not isinstance(action, dict) or action.get("action") not in {
                "select_option", "assert_value"
            }:
                continue
            options = values_by_selector.get(str(action.get("selector") or "")) or []
            current = str(action.get("value") or "")
            if not options or current in options or current.lower() in user_prompt.lower():
                continue
            representative = next(
                (
                    value for value in options
                    if value.lower() not in {"all", "any", "default", "none"}
                ),
                options[0],
            )
            action["value"] = representative
    return output


def _drop_redundant_option_visibility_assertions(
    payload: dict[str, Any]
) -> dict[str, Any]:
    """Options have selectable text/state but no independent rendered box."""
    output = json.loads(json.dumps(payload))
    for check in output.get("checks") or []:
        if not isinstance(check, dict):
            continue
        actions = [item for item in check.get("actions") or [] if isinstance(item, dict)]
        visible_selects = {
            str(item.get("selector") or "").strip()
            for item in actions
            if item.get("action") == "assert_visible"
            and not re.search(r"\s+option\s*$", str(item.get("selector") or ""))
        }
        check["actions"] = [
            item
            for item in actions
            if not (
                item.get("action") == "assert_visible"
                and (
                    match := re.fullmatch(
                        r"(.+?)\s+option\s*", str(item.get("selector") or "")
                    )
                )
                and match.group(1).strip() in visible_selects
            )
        ]
    return output


def _apply_harness_visual_evidence_policy(
    payload: dict[str, Any], *, user_prompt: str, workdir: Path
) -> dict[str, Any]:
    """Skip paid visual review only when typed evidence fully covers the Edit.

    The source-blind Planner may conservatively return ``conditional`` for a
    behavior/state-only request.  The Harness can decide this case locally from
    the instruction, input modality, check categories, and action types.  Any
    image input or explicit appearance signal keeps visual review enabled.
    """
    output = json.loads(json.dumps(payload))
    checks = [item for item in output.get("checks") or [] if isinstance(item, dict)]
    categories = {
        str(check.get("category") or "").strip().casefold() for check in checks
    }
    actions = {
        str(action.get("action") or "").strip().casefold()
        for check in checks
        for action in check.get("actions") or []
        if isinstance(action, dict)
    }
    has_images = bool(task_input_image_paths(workdir))
    visual_delta_prompt = re.sub(
        r"(?:^|[.!?]\s+)(?:keep|preserve|retain)\b[^.!?]*",
        " ",
        user_prompt,
        flags=re.IGNORECASE,
    )
    has_explicit_visual_requirement = bool(
        categories & _VISUAL_CHECK_CATEGORIES
        or _VISUAL_INSTRUCTION_RE.search(visual_delta_prompt)
    )
    has_typed_visual_action = bool(actions & _VISUAL_ACTIONS)
    if has_images:
        output["visual_evidence"] = "required"
        output["visual_evidence_reason"] = (
            "Image-grounded Edit requires independent visual evidence."
        )
    elif has_explicit_visual_requirement:
        output["visual_evidence"] = "required"
        output["visual_evidence_reason"] = (
            "The Edit explicitly changes visual appearance or responsive layout; "
            "independent rendered review is required after typed browser checks."
        )
    elif not has_typed_visual_action:
        output["visual_evidence"] = "not_required"
        output["visual_evidence_reason"] = (
            "Behavior/state-only atomic Edit is fully covered by typed DOM, "
            "ARIA, state, and real-browser evidence."
        )
    elif output.get("visual_evidence") == "not_required":
        output["visual_evidence"] = "conditional"
        output["visual_evidence_reason"] = (
            "The instruction or checks contain an appearance signal; the Harness "
            "keeps independent visual review available after non-visual gates pass."
        )
    return output


def _normalized_words(value: str) -> list[str]:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return re.findall(r"[a-z0-9]+", value.casefold())


def _planned_testid_is_grounded(value: str, grounding: str) -> bool:
    """Allow a new target test id only when its semantic stem is requested.

    This does not allow arbitrary source selectors: the identifier must be a
    data-testid whose meaningful multi-word stem occurs verbatim in the Edit.
    Structural suffixes such as ``item`` may describe the asserted target.
    """
    words = _normalized_words(value)
    while words and words[0] in {"edit", "target", "control"}:
        words.pop(0)
    while words and words[-1] in _PLANNED_TARGET_SUFFIXES:
        words.pop()
    if not words:
        return False
    def singular(word: str) -> str:
        return word[:-1] if len(word) > 3 and word.endswith("s") else word

    semantic_aliases = {
        "count": "number",
        "created": "create",
        "latest": "recent",
        "last": "recent",
        "named": "name",
        "prev": "previous",
        "pos": "position",
    }

    def canonical(word: str) -> str:
        return semantic_aliases.get(singular(word), singular(word))

    normalized_grounding = " ".join(_normalized_words(grounding))
    grounding_words = {canonical(word) for word in normalized_grounding.split()}
    # Structural table/section roles may occur inside a hierarchical test id,
    # e.g. requested-feature-header-date. Only its business words need grounding.
    words = [canonical(word) for word in words
             if word not in {"header", "heading", "cell", "column", "row"}]
    if not words:
        return False
    return " ".join(words) in normalized_grounding or (
        len(words) >= 2 and all(word in grounding_words for word in words)
    )


def _validate_atomic_plan_grounding(
    *, plan: dict[str, Any], user_prompt: str, workdir: Path
) -> None:
    """Reject model-authored test interfaces that are absent from the request.

    The atomic Planner intentionally cannot inspect source. Fine-grained selectors
    therefore belong either to explicit user/accepted-obligation language or to a
    Harness-owned hidden oracle. Letting the Planner invent them makes the Generator
    add product code solely to satisfy its own test.
    """
    obligations = accepted_obligation_summary(workdir / ".harness")
    source_ui_contract = _read_source_ui_contract(workdir)
    grounding = (
        user_prompt
        + "\n" + json.dumps(chain_obligations(read_edit_task_contract(workdir) or {}), ensure_ascii=False)
        + "\n"
        + json.dumps(obligations, ensure_ascii=False)
        + "\n"
        + json.dumps(source_ui_contract, ensure_ascii=False)
    ).casefold()
    contract = read_edit_task_contract(workdir) or {}
    allowed_routes = {
        str(route).casefold()
        for route in contract.get("requested_target_routes") or []
        if str(route).strip()
    }
    errors: list[str] = []
    for anchor in plan.get("source_anchors") or []:
        value = str(anchor).strip()
        if (
            value
            and value.casefold() not in grounding
            and value.lstrip("#.").casefold() not in grounding
        ):
            errors.append(f"source anchor {value!r} is not grounded")
    for check in plan.get("checks") or []:
        if not isinstance(check, dict):
            continue
        check_id = str(check.get("id") or "unknown")
        for action in check.get("actions") or []:
            if not isinstance(action, dict):
                continue
            selector = str(action.get("selector") or "").strip()
            for attribute, _quote, value in _SELECTOR_ATTRIBUTE_RE.findall(selector):
                folded = value.casefold()
                if attribute.casefold() in {"href", "src"}:
                    route_value = folded if folded.startswith("/") else f"/{folded}"
                    if folded in allowed_routes or route_value in allowed_routes or folded in grounding:
                        continue
                if (
                    attribute.casefold() == "data-testid"
                    and _planned_testid_is_grounded(value, grounding)
                ):
                    continue
                if folded not in grounding:
                    errors.append(
                        f"{check_id}: selector attribute {attribute}={value!r} is not grounded"
                    )
            for token in re.findall(r"(?<![\w-])[#.](?!\d)([A-Za-z_][\w-]*)", selector):
                if token.casefold() not in grounding:
                    errors.append(
                        f"{check_id}: selector identity {token!r} is not grounded"
                    )
            if action.get("action") in {"set_storage_value", "assert_storage_value"}:
                key = str(action.get("key") or "").strip()
                if key and key.casefold() not in grounding:
                    errors.append(
                        f"{check_id}: storage key {key!r} is not grounded"
                    )
    if errors:
        raise ValueError(
            "ungrounded atomic Edit checks: " + "; ".join(dict.fromkeys(errors))
        )


class AtomicEditPlannerToolPolicy:
    """Allow the SDK fallback to touch only its single semantic artifact."""

    _WRITE_TOOLS = {"write", "write_file", "edit", "apply_patch", "multiedit"}
    _READ_TOOLS = {"read", "read_file"}

    def check(self, tool: str, tool_input: dict[str, Any]) -> str | None:
        normalized = tool.lower()
        raw_path = tool_input.get("path") or tool_input.get("file_path")
        path = str(raw_path or "").replace("\\", "/")
        allowed = f".harness/{ATOMIC_EDIT_PLAN_NAME}"
        if normalized in self._WRITE_TOOLS and path != allowed:
            return f"Atomic Edit Planner may write only {allowed}."
        if normalized in self._READ_TOOLS and path != allowed:
            return f"Atomic Edit Planner may read only {allowed}."
        if normalized not in self._WRITE_TOOLS | self._READ_TOOLS:
            return "Atomic Edit Planner has no repository exploration tools."
        return None

    def observe_result(
        self, _tool: str, _tool_input: dict[str, Any], *, ok: bool, output: Any
    ) -> None:
        del ok, output


def _atomic_edit_prompt(
    *, user_prompt: str, workdir: Path, contract: dict[str, Any]
) -> str:
    obligations = accepted_obligation_summary(workdir / ".harness")
    source_ui_contract = _read_source_ui_contract(workdir)
    input_context = task_input_prompt_context(workdir)
    prompt = (
        "Create the atomic Edit plan for this instruction:\n\n"
        f"{user_prompt.strip()}\n\n"
        "Requested target routes: "
        + json.dumps(contract.get("requested_target_routes") or [], ensure_ascii=False)
        + "\nExisting accepted obligations are preservation metadata only:\n"
        + json.dumps(obligations, ensure_ascii=False)
    )
    if source_ui_contract:
        prompt += (
            "\nHarness-observed source UI navigation/control hints (not source code):\n"
            + json.dumps(source_ui_contract, ensure_ascii=False)
            + "\nUse an exact observed navigation entry before target assertions when the "
            "requested surface is not the default visible view. These setup controls are "
            "preserved source state, not new Edit deliverables."
        )
    if input_context:
        prompt += "\n\n" + input_context
    if chain_obligations(contract):
        prompt += (
            "\nChain host-state obligations (not additional tasks):\n"
            + json.dumps(chain_obligations(contract), ensure_ascii=False)
            + "\nVerify requires against the accepted source. Preserve named prior capabilities; "
            "use produces/acceptance for the compact completion check. Do not assume declared "
            "state exists merely because a previous step planned it."
        )
    return prompt


def _validate_and_materialize(
    *, file_comm: FileComm, user_prompt: str, config: HarnessConfig
) -> dict[str, Any]:
    from src.agents.planner import _initialize_accepted_sprints, _validate_planning_bundle

    plan_path = file_comm.dir / ATOMIC_EDIT_PLAN_NAME
    if not plan_path.is_file():
        raise ValueError(f"Atomic Edit Planner must write .harness/{ATOMIC_EDIT_PLAN_NAME}.")
    raw_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    source_ui_contract = _read_source_ui_contract(file_comm.dir.parent)
    raw_plan = _rewrite_observed_positional_selectors(
        raw_plan, source_ui_contract
    )
    raw_plan = _rewrite_observed_item_selectors(raw_plan, source_ui_contract)
    raw_plan = _rewrite_observed_setup_control_selectors(
        raw_plan, source_ui_contract
    )
    raw_plan = _ensure_source_surface_entry(raw_plan, source_ui_contract)
    raw_plan = _drop_harness_owned_preservation_presence_checks(raw_plan)
    obligations = accepted_obligation_summary(file_comm.dir)
    literal_grounding = (user_prompt + "\n" + json.dumps(obligations, ensure_ascii=False)
                         + "\n" + json.dumps(chain_obligations(
                             read_edit_task_contract(file_comm.dir.parent) or {}
                         ), ensure_ascii=False))
    source_grounding = (
        literal_grounding
        + "\n"
        + json.dumps(
            source_ui_contract, ensure_ascii=False
        )
    )
    raw_plan = _drop_unowned_source_anchors(
        raw_plan, grounding=source_grounding
    )
    raw_plan = _downgrade_unowned_text_assertions(
        raw_plan, grounding=literal_grounding
    )
    raw_plan = _normalize_unspecified_value_assertions(raw_plan)
    raw_plan = _normalize_instruction_context_actions(
        raw_plan, user_prompt=user_prompt
    )
    raw_plan = _normalize_bounded_planner_actions(raw_plan)
    raw_plan = _ensure_explicit_reorder_history_fields(
        raw_plan, user_prompt=user_prompt
    )
    raw_plan = _drop_incidental_summary_dependency_waits(
        raw_plan, user_prompt=user_prompt
    )
    raw_plan = _normalize_named_view_flows(
        raw_plan, user_prompt=user_prompt
    )
    raw_plan = _drop_duplicate_pre_effect_assertions(raw_plan)
    raw_plan = _ensure_direct_hash_setup(raw_plan, user_prompt=user_prompt)
    raw_plan = _normalize_hash_filtered_summary_flow(
        raw_plan,
        user_prompt=user_prompt,
        source_ui_contract=source_ui_contract,
    )
    raw_plan = _normalize_scroll_restore_checks(
        raw_plan, user_prompt=user_prompt
    )
    raw_plan = _ensure_addressability_assertion(
        raw_plan, user_prompt=user_prompt
    )
    raw_plan = _normalize_observed_select_options(
        raw_plan,
        source_ui_contract=source_ui_contract,
        user_prompt=user_prompt,
    )
    raw_plan = _stabilize_persistence_checks(raw_plan)
    raw_plan = _drop_redundant_option_visibility_assertions(raw_plan)
    raw_plan = _drop_adjacent_duplicate_assertions(raw_plan)
    raw_plan = _drop_unowned_storage_assertions(
        raw_plan,
        grounding=source_grounding,
    )
    raw_plan = _relax_unowned_hash_encodings(
        raw_plan,
        grounding=source_grounding,
    )
    raw_plan = _preserve_observed_hash_route_for_default_filter(
        raw_plan,
        user_prompt=user_prompt,
        source_ui_contract=source_ui_contract,
    )
    raw_plan = _apply_harness_visual_evidence_policy(
        raw_plan,
        user_prompt=user_prompt,
        workdir=file_comm.dir.parent,
    )
    write_atomic_edit_plan(
        file_comm.dir,
        raw_plan,
        instruction_delta=user_prompt,
    )
    plan = read_atomic_edit_plan(file_comm.dir)
    if plan is None:  # pragma: no cover - write above validates the schema
        raise ValueError("Atomic Edit Planner produced an unreadable normalized plan.")
    _validate_atomic_plan_grounding(
        plan=plan, user_prompt=user_prompt, workdir=file_comm.dir.parent
    )
    materialize_atomic_edit_compatibility_bundle(
        file_comm=file_comm,
        instruction_delta=user_prompt,
        plan=plan,
    )
    _validate_planning_bundle(file_comm, config)
    _initialize_accepted_sprints(file_comm)
    return plan


def _make_atomic_stop_hook(
    *, file_comm: FileComm, user_prompt: str, config: HarnessConfig
):
    async def _hook(_input: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        try:
            _validate_and_materialize(
                file_comm=file_comm, user_prompt=user_prompt, config=config
            )
        except Exception as exc:
            reason = f"Atomic Edit plan validation failed: {exc}"
            return {"decision": "block", "reason": reason, "stopReason": reason}
        return {"decision": "complete"}

    return _hook


def _is_openai_runtime(config: HarnessConfig) -> bool:
    runtime = config.agent_runtime.strip().lower()
    model = config.planner_model.strip().lower()
    return runtime == "openai" or (
        runtime == "auto"
        and model.startswith(("deepseek", "qwen", "gpt-", "o1", "o3", "o4"))
    )


async def run_atomic_edit_planner(
    config: HarnessConfig, user_prompt: str, file_comm: FileComm, workdir: Path,
) -> AgentRunStats:
    """Correct one diagnosed pre-Build action-contract error with the same planner."""
    from src.agents.planner import PlannerValidationError
    trace_path = file_comm.dir / "traces/planner.jsonl"
    offset = len(trace_path.read_text().splitlines()) if trace_path.exists() else 0
    try:
        return await _run_atomic_edit_planner(config, user_prompt, file_comm, workdir)
    except PlannerValidationError as exc:
        if not _is_openai_runtime(config) or (file_comm.read_state() or {}).get("edit_freeze"):
            raise
        path = file_comm.dir / ATOMIC_EDIT_PLAN_NAME
        rejected = json.loads(path.read_text())
        feedback = {"diagnostic": str(exc), "rejected_plan": rejected}
        rejected_path = file_comm.dir / f"rejected_atomic_plan_{time.time_ns()}.json"
        rejected_path.write_text(json.dumps(feedback, ensure_ascii=False, indent=2) + "\n")
        result = await _run_atomic_edit_planner(
            config, user_prompt, file_comm, workdir, validation_feedback=feedback,
        )
        # Both paid attempts remain attributable in the existing trace and cost ledger.
        usage_events = [json.loads(line) for line in trace_path.read_text().splitlines()[offset:]]
        usage_events = [event for event in usage_events if event.get("event") == "usage"]
        return replace(result,
            cost_usd=sum(float(event.get("estimated_cost_usd") or 0) for event in usage_events),
            token_usage={key: sum(int((event.get("attempt_usage") or {}).get(key) or 0)
                                  for event in usage_events) for key in ("input_tokens", "output_tokens")},
        )


async def _run_atomic_edit_planner(
    config: HarnessConfig,
    user_prompt: str,
    file_comm: FileComm,
    workdir: Path,
    *, validation_feedback: dict[str, Any] | None = None,
) -> AgentRunStats:
    """Plan one Edit with one JSON response on native OpenAI runtimes."""
    contract = read_edit_task_contract(workdir)
    if contract is None:
        raise ValueError("Atomic Edit Planner requires edit_task_contract.json")
    prompt = _atomic_edit_prompt(
        user_prompt=user_prompt, workdir=workdir, contract=contract
    )
    if validation_feedback:
        prompt += ("\nCorrect the rejected pre-Build plan using the exact structural diagnostics below. "
                   "Preserve the user requirement and meaningful tests; do not weaken assertions to pass validation. "
                   "Use the source facts to supply valid action parameters. Return the complete corrected plan.\n"
                   + json.dumps(validation_feedback, ensure_ascii=False))
    trace_path = file_comm.dir / "traces" / "planner.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    plan_path = file_comm.dir / ATOMIC_EDIT_PLAN_NAME

    # A novelty/instruction-expansion stage may already have produced this
    # exact atomic plan. Revalidate it against the current instruction and
    # materialize compatibility views locally instead of paying a second LLM.
    if plan_path.is_file() and validation_feedback is None:
        plan = _validate_and_materialize(
            file_comm=file_comm, user_prompt=user_prompt, config=config
        )
        with trace_path.open("a", encoding="utf-8") as trace:
            trace.write(json.dumps({
                "event": "run_start",
                "model": None,
                "phase": "upstream_atomic_plan_reuse",
                "prompt": prompt,
            }, ensure_ascii=False) + "\n")
            trace.write(json.dumps({
                "event": "atomic_edit_plan",
                "artifact": f".harness/{ATOMIC_EDIT_PLAN_NAME}",
                "source": "upstream_structured_plan",
                "check_count": len(plan.get("checks") or []),
            }, ensure_ascii=False) + "\n")
            trace.write(json.dumps({
                "event": "usage",
                "request_usage": {},
                "attempt_usage": {"input_tokens": 0, "output_tokens": 0},
                "cumulative_usage": {"input_tokens": 0, "output_tokens": 0},
                "estimated_cost_usd": 0.0,
            }, ensure_ascii=False) + "\n")
        return AgentRunStats(
            cost_usd=0.0,
            duration_ms=int((time.monotonic() - started) * 1000),
            duration_api_ms=0,
            token_usage={"input_tokens": 0, "output_tokens": 0},
            usage={"recovery": "upstream_atomic_plan"},
            model_usage={},
        )

    if _is_openai_runtime(config):
        content: str | list[dict[str, Any]] = prompt
        images = task_input_image_paths(workdir)
        if images:
            content = openai_user_content(prompt, images)
        client = OpenAIHTTPClient(config, config.agent_request_timeout_seconds)
        with trace_path.open("a", encoding="utf-8") as trace:
            trace.write(json.dumps({
                "event": "run_start",
                "model": config.planner_model,
                "phase": "atomic_edit_planner",
                "prompt": prompt,
                "image_paths": [str(path) for path in images],
            }, ensure_ascii=False) + "\n")
            trace.flush()
            response = await client.complete(
                model=config.planner_model,
                messages=[
                    {"role": "system", "content": ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                temperature=0,
                max_tokens=4096,
            )
            message = response.get("choices", [{}])[0].get("message", {})
            text = str(message.get("content") or "")
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
            output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
            trace.write(json.dumps({
                "event": "assistant_response",
                "content": text,
            }, ensure_ascii=False) + "\n")
            trace.write(json.dumps({
                "event": "usage",
                "request_usage": usage,
                "attempt_usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                "cumulative_usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                "estimated_cost_usd": estimate_cost_usd(config.planner_model, usage),
            }, ensure_ascii=False) + "\n")
            trace.flush()
            payload = extract_json_object(text)
            write_atomic_edit_plan(
                file_comm.dir,
                payload,
                instruction_delta=user_prompt,
            )
            _validate_and_materialize(
                file_comm=file_comm, user_prompt=user_prompt, config=config
            )
            trace.write(json.dumps({
                "event": "atomic_edit_plan",
                "artifact": f".harness/{ATOMIC_EDIT_PLAN_NAME}",
            }, ensure_ascii=False) + "\n")
            trace.flush()
        return AgentRunStats(
            cost_usd=estimate_cost_usd(config.planner_model, usage),
            duration_ms=int((time.monotonic() - started) * 1000),
            duration_api_ms=None,
            token_usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
            usage=usage,
            model_usage={},
        )

    if not plan_path.exists():
        plan_path.write_text("{}\n", encoding="utf-8")
    sdk_prompt = (
        prompt
        + f"\n\nWrite only `.harness/{ATOMIC_EDIT_PLAN_NAME}` with the JSON object."
    )
    result, _cost, _text, _denials = await run_sdk_agent(
        prompt=sdk_prompt,
        config=config,
        workdir=workdir,
        model=config.planner_model,
        system_prompt=ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT,
        max_turns=min(config.planner_max_turns, 4),
        allow_bash=False,
        stop_hooks=[_make_atomic_stop_hook(
            file_comm=file_comm, user_prompt=user_prompt, config=config
        )],
        trace_path=trace_path,
        mutation_policy=AtomicEditPlannerToolPolicy(),
        image_paths=task_input_image_paths(workdir),
    )
    _validate_and_materialize(
        file_comm=file_comm, user_prompt=user_prompt, config=config
    )
    return build_agent_run_stats(result, model=config.planner_model)


def refresh_atomic_edit_plan(
    *, file_comm: FileComm, user_prompt: str, config: HarnessConfig
) -> dict[str, Any] | None:
    """Reapply current deterministic plan normalization on an Edit resume."""
    if not (file_comm.dir / ATOMIC_EDIT_PLAN_NAME).is_file():
        return None
    return _validate_and_materialize(
        file_comm=file_comm,
        user_prompt=user_prompt,
        config=config,
    )


__all__ = [
    "AtomicEditPlannerToolPolicy",
    "_validate_atomic_plan_grounding",
    "refresh_atomic_edit_plan",
    "run_atomic_edit_planner",
]
