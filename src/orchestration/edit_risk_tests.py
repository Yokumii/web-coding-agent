"""Derive target-blind hidden tests from an accepted source and normal Edit.

The module does not create defects.  It translates semantic signals already
produced by the Edit Planner, together with the accepted source UI inventory,
into a small set of WebCompass-shaped risk assertions.  Those assertions are
frozen before Build and can only yield Repair data when a normal candidate
actually fails and is later recovered in the same Sprint.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from src.orchestration.atomic_edit_plan import read_atomic_edit_plan
from src.orchestration.edit_task_contract import read_edit_task_contract
from src.orchestration.hidden_oracle_checks import (
    read_hidden_oracle_checks,
    write_hidden_oracle_checks,
)
from src.orchestration.ui_action_contracts import TYPED_ASSERTION_ACTIONS
from src.orchestration.webcompass_protocol import REPAIR_TYPES

SOURCE_RISK_POLICY_VERSION = "source-risk-baseline-v11-state-reuse"


ARTIFACT_NAME = "edit_risk_tests.json"
SCHEMA_VERSION = "edit-risk-tests-v1"
DEFAULT_MAX_RISK_CHECKS = 0

_SETUP_ACTIONS = {
    "click",
    "drag_and_drop",
    "emulate_media",
    "fill",
    "hover",
    "key_press",
    "reload",
    "scroll",
    "select_option",
    "set_hash",
    "set_input_files",
    "set_storage_value",
    "set_viewport",
    "wait_for",
}
_INTERACTION_ACTIONS = {
    "click",
    "drag_and_drop",
    "fill",
    "hover",
    "key_press",
    "select_option",
    "set_input_files",
}

# These are risk triggers, not defect labels.  The semantic interpretation is
# primarily supplied by planner-authored categories, impact tags and typed
# actions; instruction terms add evidence when the planner uses generic tags.
_TRIGGERS: dict[str, tuple[str, ...]] = {
    "Occlusion": (
        "modal", "dialog", "drawer", "overlay", "popover", "dropdown", "menu",
        "tooltip", "toast", "notification", "fixed", "sticky", "浮层", "弹窗",
        "抽屉", "下拉", "通知",
    ),
    "Crowding": (
        "layout", "responsive", "mobile", "grid", "table", "card", "list",
        "dashboard", "toolbar", "spacing", "布局", "响应式", "表格", "卡片", "间距",
    ),
    "Text Overlap": (
        "text", "label", "title", "description", "table", "card", "tooltip",
        "notification", "responsive", "文本", "标题", "标签", "表格", "响应式",
    ),
    "Alignment": (
        "align", "layout", "grid", "table", "form", "dashboard", "card", "row",
        "column", "对齐", "布局", "网格", "表格", "表单", "卡片",
    ),
    "Color Contrast": (
        "color", "colour", "theme", "dark", "light", "background", "foreground",
        "contrast", "颜色", "主题", "深色", "浅色", "背景", "对比度",
    ),
    "Overflow": (
        "responsive", "mobile", "table", "grid", "carousel", "sidebar", "text",
        "list", "dashboard", "scroll", "width", "响应式", "移动端", "表格", "滚动",
        "宽度", "溢出",
    ),
    "Sizing Proportion": (
        "image", "avatar", "logo", "video", "canvas", "icon", "chart", "ratio",
        "width", "height", "size", "图片", "头像", "视频", "图标", "图表", "尺寸",
        "宽度", "高度", "比例",
    ),
    "Loss of Interactivity": (
        "interaction", "interactive", "button", "link", "filter", "sort", "drag",
        "upload", "form", "wizard", "cart", "auth", "click", "交互", "按钮", "链接",
        "筛选", "排序", "拖拽", "上传", "表单", "购物车", "登录",
    ),
    "Semantic Error": (
        "accessibility", "semantic", "aria", "navigation", "nav", "form", "table",
        "list", "button", "link", "dialog", "可访问", "语义", "导航", "表单", "表格",
        "列表", "按钮", "链接",
    ),
    "Nesting Error": (
        "markup", "structure", "wrap", "nest", "navigation", "form", "table", "list",
        "dialog", "结构", "嵌套", "导航", "表单", "表格", "列表",
    ),
    "Missing Attributes": (
        "accessibility", "aria", "image", "avatar", "logo", "form", "input", "upload",
        "auth", "link", "validation", "可访问", "图片", "头像", "表单", "输入", "上传",
        "链接", "校验",
    ),
}

_SOURCE_TAG_RISKS = {
    "form": {"Loss of Interactivity", "Semantic Error", "Nesting Error", "Missing Attributes"},
    "table": {"Crowding", "Text Overlap", "Alignment", "Overflow", "Semantic Error", "Nesting Error"},
    "img": {"Sizing Proportion", "Missing Attributes"},
    "video": {"Sizing Proportion", "Missing Attributes"},
    "canvas": {"Sizing Proportion"},
    "nav": {"Semantic Error", "Nesting Error", "Missing Attributes"},
    "button": {"Loss of Interactivity", "Semantic Error", "Missing Attributes"},
    "input": {"Loss of Interactivity", "Semantic Error", "Missing Attributes"},
    "select": {"Loss of Interactivity", "Semantic Error", "Missing Attributes"},
}


def artifact_path(harness_dir: Path) -> Path:
    return Path(harness_dir) / ARTIFACT_NAME


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_seed_manifest(workdir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((Path(workdir) / "seed_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _source_signals(workdir: Path, seed: dict[str, Any]) -> dict[str, Any]:
    """Return bounded facts from the immutable accepted source, never a candidate."""
    contract = seed.get("source_ui_contract")
    if not isinstance(contract, dict):
        contract = {}
    tags: set[str] = set()
    selectors: set[str] = set()
    routes: set[str] = set()
    for page in contract.get("pages") or []:
        if not isinstance(page, dict):
            continue
        routes.add(str(page.get("route") or "/"))
        for group in ("surfaces", "controls", "outputs", "addressable_items"):
            for item in page.get(group) or []:
                if not isinstance(item, dict):
                    continue
                tag = str(item.get("tag") or "").casefold()
                selector = str(item.get("selector") or "").strip()
                if tag:
                    tags.add(tag)
                if selector:
                    selectors.add(selector)
    for item in contract.get("observed_controls") or []:
        if isinstance(item, dict):
            tag = str(item.get("tag") or "").casefold()
            selector = str(item.get("selector") or "").strip()
            if tag:
                tags.add(tag)
            if selector:
                selectors.add(selector)

    # Explicit Edit workdirs made without prepare_forward_edit_seed may not yet
    # have the compact contract.  A bounded tag inventory keeps risk selection
    # source-aware without exposing full source to the implementation model.
    frontend = Path(workdir) / "frontend"
    scanned_files = 0
    scanned_bytes = 0
    for path in sorted(frontend.rglob("*.html"))[:20]:
        if ".git" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")[:200_000]
        except OSError:
            continue
        scanned_files += 1
        scanned_bytes += len(source.encode("utf-8"))
        tags.update(match.casefold() for match in re.findall(r"<\s*([A-Za-z][\w:-]*)\b", source))
        relative = path.relative_to(frontend).as_posix()
        routes.add("/" if relative == "index.html" else "/" + relative)
    return {
        "contract_schema_version": contract.get("schema_version"),
        "routes": sorted(routes),
        "semantic_tags": sorted(tags & set(_SOURCE_TAG_RISKS)),
        "known_selector_count": len(selectors),
        "fallback_html_files_scanned": scanned_files,
        "fallback_html_bytes_scanned": scanned_bytes,
    }


def _check_route(check: dict[str, Any]) -> str:
    return str(check.get("route") or "/")


def _target_index(check: dict[str, Any], *, interactive: bool = False) -> int | None:
    actions = check.get("actions") or []
    for index in range(len(actions) - 1, -1, -1):
        action = actions[index]
        if not isinstance(action, dict):
            continue
        kind = action.get("action")
        eligible = kind in (_INTERACTION_ACTIONS if interactive else TYPED_ASSERTION_ACTIONS)
        if kind == "assert_hidden" or (kind == "assert_count" and action.get("count") == 0):
            eligible = False
        if eligible and (action.get("selector") or action.get("source_selector")):
            return index
    return None


def _setup_prefix(check: dict[str, Any], *, interactive: bool = False) -> list[dict[str, Any]]:
    index = _target_index(check, interactive=interactive)
    if index is None and interactive:
        index = _target_index(check)
    actions = check.get("actions") or []
    # Preserve intermediate observations and captures: later actions may need
    # their state. Stop at the target observation (or before the audited click).
    return json.loads(json.dumps(actions[:index] if index is not None else []))


def _target_selector(check: dict[str, Any]) -> str:
    index = _target_index(check)
    if index is not None:
        return str(check["actions"][index]["selector"])
    return "body"


def _interactive_selector(check: dict[str, Any]) -> str:
    index = _target_index(check, interactive=True)
    if index is not None:
        action = check["actions"][index]
        return str(action.get("selector") or action["source_selector"])
    return _target_selector(check)


def _selector_implies_interactive(selector: str) -> bool:
    lowered = selector.casefold()
    return bool(re.search(
        r"(?:^|[\s>+~,])(?:a(?:\[href\]|[:.#\[])|button|input|select|textarea|summary)(?:$|[\s>+~:.#\[])"
        r"|\[(?:role\s*=\s*['\"]?(?:button|link|checkbox|radio|switch|tab)|onclick|href)\b",
        lowered,
    ))


def _restore_split_flow_prefixes(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    restored = []
    previous_flow = None
    prefix = []
    for check in checks:
        check_id = str(check.get("id") or "")
        base = re.sub(r"(?:__part\d+)+$", "", check_id)
        flow = (base, _check_route(check)) if base != check_id else None
        if flow is None or flow != previous_flow:
            prefix = []
        prefix = [*prefix, *(check.get("actions") or [])]
        restored.append({**check, "actions": list(prefix)})
        previous_flow = flow
    return restored


def _term_hits(text: str, terms: Iterable[str]) -> list[str]:
    lowered = text.casefold()
    hits: list[str] = []
    for term in terms:
        token = term.casefold()
        if re.search(r"[\u4e00-\u9fff]", token):
            matched = token in lowered
        else:
            matched = re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered) is not None
        if matched:
            hits.append(term)
    return hits[:5]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _ranked_candidates(
    *, instruction_delta: str, plan: dict[str, Any], source: dict[str, Any], routes: list[str],
    all_types: bool = False,
) -> list[dict[str, Any]]:
    checks = _restore_split_flow_prefixes(
        [item for item in plan.get("checks") or [] if isinstance(item, dict)]
    )
    plan_text = " ".join(
        [instruction_delta, str(plan.get("title") or ""), str(plan.get("goal") or "")]
        + [str(item) for item in plan.get("deliverables") or []]
        + [str(item) for item in plan.get("exit_criteria") or []]
        + [str(item) for item in plan.get("impact_tags") or []]
        + [str(item.get("category") or "") for item in checks]
        + [str(item.get("task") or "") for item in checks]
        + [str(item.get("expected_result") or "") for item in checks]
    )
    source_tags = set(source.get("semantic_tags") or [])
    candidates: list[dict[str, Any]] = []
    scenes = [
        (route, check) for route in routes
        for check in ([item for item in checks if _check_route(item) == route] or [{}])
    ]
    for route, representative in scenes:
        action_kinds = {str(action.get("action") or "")
                        for action in representative.get("actions") or []
                        if isinstance(action, dict)}
        setup = _setup_prefix(representative)
        target_selector = _target_selector(representative)
        for repair_type in sorted(REPAIR_TYPES):
            # Taxonomy-first: missing words in the Edit must not suppress an
            # entire defect class. Signals prioritize checks, not enable them.
            score = 2
            reasons: list[str] = ["WebCompass defect audit of the changed surface"]
            hits = _term_hits(plan_text, _TRIGGERS[repair_type])
            if hits:
                score += min(3, len(hits))
                reasons.append("planner semantic signals: " + ", ".join(hits))
            matching_tags = sorted(
                tag for tag in source_tags if repair_type in _SOURCE_TAG_RISKS.get(tag, set())
            )
            if matching_tags and hits:
                score += 1
                reasons.append("accepted source contains: " + ", ".join(matching_tags[:4]))
            if repair_type == "Loss of Interactivity" and action_kinds & _INTERACTION_ACTIONS:
                score += 4
                reasons.append(
                    "planned user actions: "
                    + ", ".join(sorted(action_kinds & _INTERACTION_ACTIONS))
                )
            if repair_type in {"Crowding", "Text Overlap", "Alignment", "Overflow", "Sizing Proportion"} and "set_viewport" in action_kinds:
                score += 3
                reasons.append("planner requires a bounded viewport transition")
            if repair_type == "Color Contrast" and "emulate_media" in action_kinds:
                score += 3
                reasons.append("planner requires a color-scheme transition")
            if repair_type == "Overflow" and target_selector != "body":
                score += 1
                reasons.append("target-local container can regress after this Edit")
            if repair_type in {"Semantic Error", "Missing Attributes"} and target_selector != "body":
                score += 1
                reasons.append("new or changed target DOM is explicitly addressable")
            if repair_type == "Loss of Interactivity" and not all_types and not (
                action_kinds & _INTERACTION_ACTIONS
                or _selector_implies_interactive(target_selector)
            ):
                # Global instruction words such as "link" do not make every
                # asserted heading, label, or container interactive.
                continue
            selector = (
                _interactive_selector(representative)
                if repair_type == "Loss of Interactivity"
                else target_selector
            )
            candidates.append(
                {
                    "repair_type": repair_type,
                    "route": route,
                    "selector": selector,
                    "score": score,
                    "reasons": reasons,
                    "setup_actions": _setup_prefix(representative, interactive=True)
                    if repair_type == "Loss of Interactivity" else setup,
                }
            )
    return sorted(
        candidates,
        key=lambda item: (-int(item["score"]), routes.index(str(item["route"])), str(item["repair_type"])),
    )


def _select_with_route_coverage(
    candidates: list[dict[str, Any]], routes: list[str], limit: int,
    *, priority_routes: list[str] | None = None,
    collapse_equivalent_across_routes: bool = False,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    def identity(item: dict[str, Any]) -> str:
        return json.dumps([item["route"], item["repair_type"], item["selector"],
                           item["setup_actions"]], sort_keys=True)

    used: set[str] = set()
    covered_scenes: set[str] = set()
    covered_risk_targets: set[tuple[str, str]] = set()
    covered_types: set[str] = set()

    def scene_identity(item: dict[str, Any]) -> str:
        return json.dumps(
            [item["route"], item["selector"], item["setup_actions"]], sort_keys=True
        )

    route_order = list(dict.fromkeys([*(priority_routes or []), *routes]))
    for route in route_order:
        candidate = next((item for item in candidates if item["route"] == route), None)
        signature = (
            str(candidate["repair_type"]), str(candidate["selector"])
        ) if candidate is not None else None
        if (candidate is not None
                and (not collapse_equivalent_across_routes
                     or signature not in covered_risk_targets)
                and len(selected) < limit):
            selected.append(candidate)
            used.add(identity(candidate))
            covered_scenes.add(scene_identity(candidate))
            covered_risk_targets.add(signature)
            covered_types.add(str(candidate["repair_type"]))
    passes = (
        ("type", "scene") if collapse_equivalent_across_routes
        else ("scene", "type")
    )
    for pass_kind in passes:
        for candidate in candidates:
            repair_type = str(candidate["repair_type"])
            scene = scene_identity(candidate)
            signature = (repair_type, str(candidate["selector"]))
            uncovered = (
                repair_type not in covered_types
                if pass_kind == "type"
                else scene not in covered_scenes
            )
            if (uncovered
                    and (not collapse_equivalent_across_routes
                         or signature not in covered_risk_targets)
                    and len(selected) < limit):
                selected.append(candidate)
                used.add(identity(candidate))
                covered_scenes.add(scene)
                covered_risk_targets.add(signature)
                covered_types.add(repair_type)
    for candidate in candidates:
        key = identity(candidate)
        signature = (str(candidate["repair_type"]), str(candidate["selector"]))
        if (key not in used
                and (not collapse_equivalent_across_routes
                     or signature not in covered_risk_targets)
                and len(selected) < limit):
            selected.append(candidate)
            used.add(key)
            covered_risk_targets.add(signature)
    return selected


def materialize_edit_risk_tests(
    *,
    workdir: Path,
    instruction_delta: str,
    max_checks: int | None = None,
    applicable_states: bool = False,
) -> dict[str, Any]:
    """Create and stage relevant hidden risk tests before candidate generation."""
    if max_checks is not None and not 0 <= int(max_checks) <= 11:
        raise ValueError("max_checks must be between 0 and 11")
    workdir = Path(workdir)
    harness_dir = workdir / ".harness"
    plan = read_atomic_edit_plan(harness_dir)
    contract = read_edit_task_contract(workdir)
    if plan is None or contract is None:
        raise ValueError("Edit risk tests require an atomic plan and Edit task contract")
    seed = _read_seed_manifest(workdir)
    source = _source_signals(workdir, seed)
    routes = list(contract.get("requested_target_routes") or [])
    if not routes:
        routes = list(dict.fromkeys(_check_route(item) for item in plan.get("checks") or []))
    if not routes:
        routes = ["/"]
    if applicable_states:
        # Audit the regions actually observed in the existing user flow. Keep
        # state-changing setup, but do not let a failed outcome assertion hide
        # other independently observable defects in that reached state.
        scenes = []
        for check in _restore_split_flow_prefixes(plan.get("checks") or []):
            setup = []
            for action in check.get("actions") or []:
                if action.get("action") in TYPED_ASSERTION_ACTIONS:
                    if (action.get("selector") and action.get("action") != "assert_hidden"
                            and not (action.get("action") == "assert_count" and action.get("count") == 0)):
                        scenes.append({**check, "actions": [*setup, action]})
                else:
                    setup.append(action)
        plan = {**plan, "checks": scenes or plan.get("checks") or []}
    candidates = _ranked_candidates(
        instruction_delta=instruction_delta,
        plan=plan,
        source=source,
        routes=routes,
        all_types=applicable_states,
    )
    # Visible browser checks already verify the requested behavior. Hidden
    # risk checks sample one high-signal risk per planned state by default;
    # they must not multiply all Repair classes across every split assertion.
    limit = int(max_checks) if max_checks is not None else DEFAULT_MAX_RISK_CHECKS
    new_routes = [route for route in routes if route not in set(source.get("routes") or [])]
    selected = _select_with_route_coverage(
        candidates, routes, limit, priority_routes=new_routes,
        collapse_equivalent_across_routes=max_checks is None,
    )
    if applicable_states:
        # Cover applicable taxonomy classes in each observed state; an explicit
        # historical sampling cap must not silently remove whole classes.
        unique = {}
        for candidate in candidates:
            key = json.dumps([candidate["route"], candidate["repair_type"],
                              candidate["selector"], candidate["setup_actions"]], sort_keys=True)
            unique.setdefault(key, candidate)
        selected = list(unique.values())
        limit = len(selected)
    if not selected and limit > 0:
        # A changed, explicitly addressed surface always has one conservative
        # target-local layout risk.  This provides one extra falsifiable check
        # without pretending all 11 defect types are relevant.
        first_check = next(
            (item for item in plan.get("checks") or [] if isinstance(item, dict)), {}
        )
        selected = [{
            "repair_type": "Overflow",
            "route": routes[0],
            "selector": _target_selector(first_check),
            "score": 1,
            "reasons": ["fallback audit for the explicitly changed target surface"],
            "setup_actions": _setup_prefix(first_check),
        }]

    generated: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    groups: dict[str, list[dict[str, Any]]] = {}
    for risk in selected:
        key = json.dumps([risk["route"], risk["setup_actions"]], sort_keys=True)
        groups.setdefault(key, []).append(risk)
    grouped = [(group_index, part, risk)
               for group_index, group in enumerate(groups.values(), start=1)
               for part, risk in enumerate(group, start=1)]
    for group_index, part, risk in grouped:
        check_id = f"RISK-SCENE-{group_index:02d}__part{part}"
        selected_keys.add((str(risk["route"]), str(risk["repair_type"])))
        generated.append({
            "id": check_id,
            "route": risk["route"],
            "origin": "source_edit_risk_analysis",
            "repair_type": risk["repair_type"],
            "risk_reason": "; ".join(risk["reasons"]),
            "actions": [
                *risk["setup_actions"],
                {
                    "action": "assert_webcompass_risk",
                    "selector": risk["selector"],
                    "defect_type": risk["repair_type"],
                },
            ],
        })

    existing = read_hidden_oracle_checks(harness_dir)
    retained = [
        item for item in existing
        if str(item.get("origin") or "") != "source_edit_risk_analysis"
    ]
    visible_ids = {
        str(check.get("id") or "")
        for check in plan.get("checks") or []
        if isinstance(check, dict)
    }
    reserved_ids = visible_ids | {str(item.get("id") or "") for item in retained}
    for group_index in range(1, len(groups) + 1):
        base = f"RISK-SCENE-{group_index:02d}"
        members = [item for item in generated if item["id"].startswith(base + "__part")]
        unique_base = base
        while any(unique_base + "__part" + item["id"].rsplit("__part", 1)[1] in reserved_ids for item in members):
            unique_base += "-AUTO"
        for item in members:
            item["id"] = unique_base + "__part" + item["id"].rsplit("__part", 1)[1]
    staged_checks = [*retained, *generated]
    if staged_checks:
        write_hidden_oracle_checks(
            harness_dir,
            staged_checks,
            target_routes=routes,
        )
    else:
        (harness_dir / "hidden_oracle_checks.json").unlink(missing_ok=True)

    all_decisions: list[dict[str, Any]] = []
    candidate_map = {
        (str(item["route"]), str(item["repair_type"])): item for item in candidates
    }
    for route in routes:
        for repair_type in sorted(REPAIR_TYPES):
            item = candidate_map.get((route, repair_type))
            all_decisions.append({
                "route": route,
                "repair_type": repair_type,
                "selected": (route, repair_type) in selected_keys,
                "score": int(item["score"]) if item else 0,
                "reasons": list(item["reasons"]) if item else [],
            })
    payload = {
        "schema_version": SCHEMA_VERSION,
        "owner": "harness",
        "prompt_visibility": "hidden",
        "status": "authored_before_build",
        "instruction_sha256": _sha256_text(instruction_delta),
        "baseline_commit": str(contract.get("baseline_commit") or ""),
        "source_signals": source,
        "target_routes": routes,
        "max_generated_checks": limit,
        "test_policy": "webcompass_applicable_states" if applicable_states else "webcompass_defects_first",
        "risk_decisions": all_decisions,
        "generated_check_ids": [str(item["id"]) for item in generated],
    }
    path = artifact_path(harness_dir)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def read_edit_risk_tests(harness_dir: Path) -> dict[str, Any] | None:
    path = artifact_path(harness_dir)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("owner") != "harness"
        or payload.get("prompt_visibility") != "hidden"
        or payload.get("status") != "authored_before_build"
    ):
        raise ValueError(f"invalid Edit risk test artifact: {path}")
    return payload


async def collect_source_risk_baseline(workdir: Path, checks: list[dict[str, Any]], *, headless: bool) -> dict[str, Any]:
    """Replay risk measurements on the frozen static source, never on a candidate.

    This records source defects, not source acceptance. New workflow actions
    cannot run on the source; only initial state and viewport/media setup apply.
    Unsupported app stacks keep the existing absolute risk checks.
    """
    import functools
    import io
    import subprocess
    import tarfile
    import tempfile
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from src.orchestration.browser_evidence import collect_browser_evidence

    contract = read_edit_task_contract(workdir)
    if not contract:
        return {"checks": [], "status": "not_edit"}
    commit = contract["baseline_commit"]
    source_checks = [{**check, "actions": [action for action in check.get("actions", [])
                      if action.get("action") in {"set_viewport", "emulate_media", "assert_webcompass_risk"}]}
                     for check in checks if check.get("origin") == "source_edit_risk_analysis"]
    identity = {"schema": SOURCE_RISK_POLICY_VERSION, "baseline_commit": commit, "checks": source_checks}
    cache = workdir / ".harness" / "source_risk_baseline.json"
    if cache.exists():
        saved = json.loads(cache.read_text())
        if saved.get("identity") == identity:
            return saved
    archive = subprocess.run(["git", "archive", "--format=tar", commit], cwd=workdir / "frontend",
                             check=True, capture_output=True, timeout=15).stdout
    with tempfile.TemporaryDirectory(prefix="harness-source-risk-") as directory:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(directory, filter="data")
        root = Path(directory)
        if not (root / "index.html").is_file() or (root / "package.json").exists():
            return {"checks": [], "status": "unsupported_static_baseline"}
        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=directory))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            evidence = await collect_browser_evidence(
                app_url=f"http://127.0.0.1:{server.server_port}", checks=source_checks,
                output_path=workdir / ".harness" / "source_risk_baseline_raw.json", headless=headless)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
    result = {**evidence, "identity": identity, "status": "observed_source_initial_state"}
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def distinguish_preexisting_risks(evidence: dict[str, Any], baseline: dict[str, Any]) -> None:
    """Retain exact source findings separately; new or changed findings still fail."""
    from collections import Counter
    previous = {item["check_id"]: item for item in baseline.get("checks", [])}
    for item in evidence.get("checks", []):
        source = previous.get(item["check_id"], {})
        source_findings = [issue for step in source.get("steps", []) if step.get("action") == "assert_webcompass_risk"
                           for issue in step.get("output", {}).get("actual", {}).get("issues", [])
                           if issue.get("element_path") and issue.get("kind") != "target-surface-missing"]
        remaining = Counter(json.dumps(issue, sort_keys=True) for issue in source_findings)
        for step in item.get("steps", []):
            if step.get("action") != "assert_webcompass_risk" or "output" not in step:
                continue
            actual = step["output"]["actual"]
            novel, preexisting = [], []
            for issue in actual.get("issues", []):
                signature = json.dumps(issue, sort_keys=True)
                if remaining[signature] > 0:
                    remaining[signature] -= 1
                    preexisting.append(issue)
                else:
                    novel.append(issue)
            if preexisting:
                actual.update(issues=novel, preexisting_issues=preexisting, passed=not novel)
                step["ok"] = not novel
        if item.get("status") == "action_failed" and all(step.get("ok") for step in item.get("steps", [])):
            item["status"] = "ok"
    evidence["source_risk_baseline"] = {"status": baseline.get("status"),
                                       "policy_version": SOURCE_RISK_POLICY_VERSION,
                                       "identity": baseline.get("identity")}


__all__ = [
    "ARTIFACT_NAME",
    "DEFAULT_MAX_RISK_CHECKS",
    "materialize_edit_risk_tests",
    "read_edit_risk_tests",
]
