from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from src.agents.sdk_runner import AgentRunStats, build_agent_run_stats, run_sdk_agent
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.ui_action_contracts import (
    ActionContractError,
    TYPED_ASSERTION_ACTIONS,
    validate_ui_action,
    validate_ui_action_sequence,
)
from src.orchestration.target_profile import target_profile_guidance
from src.orchestration.edit_task_contract import read_edit_task_contract
from src.orchestration.accepted_tapes import accepted_obligation_summary
from src.orchestration.task_inputs import (
    task_input_image_paths,
    task_input_prompt_context,
)
from src.prompts.planner import planner_system_prompt
from src.utils.logger import get_logger

logger = get_logger(__name__)

_PLANNER_TRACE_ARTIFACTS = {
    "spec.md",
    "design_tokens.json",
    "feature_list.json",
    "sprint_plan.json",
    "ui_verification_plan.json",
}
# progress.md is operational provenance: the stop hook is explicitly allowed
# to write it after every semantic planning artifact validates. Recovery still
# validates its current contents, but does not falsely require model authorship.

_REQUIRED_SPEC_HEADERS = (
    "## Product Overview",
    "## Target Users",
    "## Feature Descriptions",
    "## Technical Architecture",
    "## Visual Design Direction",
)


class PlannerValidationError(ValueError):
    """planner 产物缺失、结构异常或交叉引用失配时抛出。"""


def _make_planner_stop_hook(file_comm: FileComm, config: HarnessConfig):
    """在 planner 结束前执行最终校验，失败时阻断 stop 并要求原会话修正。"""

    async def _hook(_input: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        try:
            _validate_planning_bundle(file_comm, config)
        except PlannerValidationError as exc:
            # progress.md is operational provenance, not a semantic planning
            # decision. Once every schema-bearing artifact is valid, the
            # harness can record that milestone itself instead of spending an
            # extra model turn that often causes the planner to rewrite the
            # whole bundle.
            if str(exc) == "Planner completed without writing .harness/progress.md.":
                file_comm.write_progress(
                    "# Progress Log\n\nPlanning bundle validated by the harness; ready for implementation.\n"
                )
                try:
                    _validate_planning_bundle(file_comm, config)
                except PlannerValidationError:
                    pass
                else:
                    return {"decision": "complete"}
            return {
                "decision": "block",
                "reason": (
                    "Planning artifact validation failed. Update the existing files under "
                    f".harness, then try to stop again.\n\n{exc}"
                ),
                "stopReason": "Planner artifacts failed validation; continue editing .harness.",
            }
        return {"decision": "complete"}

    return _hook


def _validate_planning_bundle(
    file_comm: FileComm, config: HarnessConfig | None = None
) -> None:
    """校验 planner 落盘结果是否完整，且多文件之间彼此一致。

    各单文件的字段结构已经由 `FileComm.read_*` 背后的 pydantic 模型保证，
    这里补足模型难以表达的规则：

    * 必需产物是否真的写出；
    * `spec.md` 是否包含约定章节；
    * `progress.md` 是否为空；
    * `feature_list`、`sprint_plan`、`ui_verification_plan` 的交叉引用是否成立；
    * 每个 sprint 的 deliverables 与 exit_criteria 是否超过配置上限。
    """
    if config is None:
        config = HarnessConfig()

    if file_comm.is_planning_scaffold("spec.md"):
        raise PlannerValidationError(
            "Planner completed without writing .harness/spec.md."
        )
    spec = file_comm.read_spec()
    if not spec:
        raise PlannerValidationError(
            "Planner completed without writing .harness/spec.md."
        )
    if not all(header in spec for header in _REQUIRED_SPEC_HEADERS):
        raise PlannerValidationError(
            "Planner wrote invalid spec.md: required sections are missing."
        )

    artifact_readers = (
        ("design_tokens.json", file_comm.read_design_tokens),
        ("feature_list.json", file_comm.read_feature_list),
        ("sprint_plan.json", file_comm.read_sprint_plan),
        ("ui_verification_plan.json", file_comm.read_ui_verification_plan),
    )
    artifacts: dict[str, Any] = {}
    for filename, reader in artifact_readers:
        if file_comm.is_planning_scaffold(filename):
            raise PlannerValidationError(
                f"Planner completed without writing .harness/{filename}."
            )
        try:
            artifact = reader()
        except ValidationError as exc:
            raise PlannerValidationError(
                f"Planner artifact failed schema validation in {filename}:\n{exc}"
            ) from exc
        if artifact is None:
            raise PlannerValidationError(
                f"Planner completed without writing .harness/{filename}."
            )
        artifacts[filename] = artifact

    progress = file_comm.read_progress()
    if file_comm.is_planning_scaffold("progress.md") or not progress.strip():
        raise PlannerValidationError(
            "Planner completed without writing .harness/progress.md."
        )

    feature_list = artifacts["feature_list.json"]
    sprint_plan = artifacts["sprint_plan.json"]
    verification_plan = artifacts["ui_verification_plan.json"]

    _check_cross_references(
        feature_list=feature_list,
        sprint_plan=sprint_plan,
        verification_plan=verification_plan,
    )
    _check_action_contracts(verification_plan)
    _check_sprint_size_caps(sprint_plan, config)
    _check_edit_transaction(file_comm, sprint_plan, verification_plan)


def _check_edit_transaction(
    file_comm: FileComm,
    sprint_plan: dict[str, Any],
    verification_plan: dict[str, Any],
) -> None:
    """Keep one user Edit atomic while retaining multi-route/multi-file scope."""
    contract = read_edit_task_contract(file_comm.dir.parent)
    if contract is None:
        return
    sprints = sprint_plan.get("sprints") or []
    if int(sprint_plan.get("total_sprints") or 0) != 1 or len(sprints) != 1:
        raise PlannerValidationError(
            "Explicit Edit planning requires exactly one Sprint; split an oversized "
            "instruction before it enters the Harness, not into internal Sprints."
        )
    sprint = sprints[0]
    if not sprint.get("requirement_changes"):
        raise PlannerValidationError(
            "Explicit Edit sprint requires requirement_changes with add/refine/replace/withdraw semantics."
        )
    if not sprint.get("impact_tags"):
        raise PlannerValidationError("Explicit Edit sprint requires non-empty impact_tags.")
    if not str(sprint.get("visual_evidence_reason") or "").strip():
        raise PlannerValidationError(
            "Explicit Edit sprint requires visual_evidence_reason, including when vision is not required."
        )
    verification_sprints = verification_plan.get("sprints") or []
    if len(verification_sprints) != 1:
        raise PlannerValidationError(
            "Explicit Edit requires exactly one UI verification Sprint."
        )
    for check in verification_sprints[0].get("checks") or []:
        if not str(check.get("requirement_id") or "").strip():
            raise PlannerValidationError(
                f"Explicit Edit check {check.get('id', 'unknown')} requires requirement_id."
            )
        if not check.get("impact_tags"):
            raise PlannerValidationError(
                f"Explicit Edit check {check.get('id', 'unknown')} requires impact_tags."
            )


# Keep validation feedback batch-oriented. A billable planner should receive
# every malformed check in one correction turn instead of discovering one
# schema error per retry.
def _check_action_contracts(verification_plan: dict[str, Any]) -> None:
    errors: list[str] = []
    for sprint in verification_plan.get("sprints") or []:
        # Each sprint's target checks execute in a fresh browser context. State
        # from an accepted earlier sprint is replayed independently as a
        # regression tape and is not setup for the current sprint's checks.
        stateful_routes: set[str] = set()
        known_selector_min: dict[tuple[str, str], int] = {}
        known_selector_exact: dict[tuple[str, str], int] = {}
        for check in sprint.get("checks") or []:
            check_id = str(check.get("id", "unknown"))
            route = check.get("route", "/")
            parsed_route = urlsplit(route) if isinstance(route, str) else None
            route_segments = (
                parsed_route.path.replace("\\", "/").split("/")
                if parsed_route is not None else []
            )
            if (
                parsed_route is None
                or not route
                or not route.startswith("/")
                or route.startswith("//")
                or "\\" in route
                or parsed_route.scheme
                or parsed_route.netloc
                or parsed_route.query
                or parsed_route.fragment
                or any(segment in {".", ".."} for segment in route_segments)
            ):
                errors.append(
                    f"{check_id}: route must be a safe same-origin path; got {route!r}"
                )
            actions = check.get("actions") or []
            fixtures = check.get("fixtures") or []
            if not isinstance(fixtures, list) or any(
                not isinstance(value, str) or not value.strip() or len(value) > 200
                for value in fixtures
            ):
                errors.append(
                    f"{check_id}: fixtures must be a list of non-empty strings up to 200 characters"
                )
                fixtures = []
            if not actions:  # Historical action-less plans remain readable.
                continue
            action_kinds = [
                str(action.get("action", ""))
                for action in actions
                if isinstance(action, dict)
            ]
            if (
                check.get("category") == "empty_state"
                and action_kinds[:1] == ["reload"]
                and route in stateful_routes
            ):
                errors.append(
                    f"{check_id}: an initial empty-state reload must run before state-producing "
                    f"functionality/persistence checks on route {route!r}"
                )
            final_action = (
                str(actions[-1].get("action"))
                if isinstance(actions[-1], dict) else ""
            )
            if final_action not in TYPED_ASSERTION_ACTIONS:
                errors.append(f"{check_id}: must end with a typed assertion")
            assertion_count = sum(
                isinstance(action, dict)
                and action.get("action") in TYPED_ASSERTION_ACTIONS
                for action in actions
            )
            if not 1 <= assertion_count <= 4:
                errors.append(
                    f"{check_id}: must contain 1 to 4 related typed assertions; found {assertion_count}"
                )
            for index, action in enumerate(actions, 1):
                if (
                    isinstance(action, dict)
                    and action.get("action") == "scroll"
                    and (
                        isinstance(action.get("y"), bool)
                        or not isinstance(action.get("y"), int)
                    )
                ):
                    errors.append(
                        f"{check_id} action {index}: scroll action requires integer y"
                    )
                if isinstance(action, dict) and action.get("action") == "evaluate":
                    errors.append(
                        f"{check_id} action {index}: legacy evaluate is forbidden; use a bounded typed assertion"
                    )
                    continue
                if (
                    isinstance(action, dict)
                    and action.get("action") == "key_press"
                    and action.get("key") == "Tab"
                    and str(action.get("selector", "")).strip().lower()
                    in {"body", "html", "main"}
                ):
                    errors.append(
                        f"{check_id} action {index}: Tab start must name a focusable "
                        "control, not a global container"
                    )
                if (
                    isinstance(action, dict)
                    and action.get("action") == "assert_storage_value"
                    and isinstance(action.get("value"), str)
                    and "match" not in action
                ):
                    errors.append(
                        f"{check_id} action {index}: string storage assertions require "
                        "explicit match exact/contains; JSON-backed storage normally uses contains"
                    )
                if (
                    isinstance(action, dict)
                    and action.get("action") == "assert_attribute"
                    and str(action.get("name", "")).lower() in {"class", "style"}
                ):
                    errors.append(
                        f"{check_id} action {index}: exact class/style assertions are forbidden; "
                        "use a stable state selector/attribute or assert_focus"
                    )
                try:
                    validate_ui_action(action)
                except ActionContractError as exc:
                    errors.append(f"{check_id} action {index}: {exc}")
            try:
                validate_ui_action_sequence(actions)
            except ActionContractError as exc:
                errors.append(f"{check_id} sequence: {exc}")

            active_route = str(route)
            state_producing = False
            for index, action in enumerate(actions, 1):
                if not isinstance(action, dict):
                    continue
                kind = str(action.get("action", ""))
                if kind in {
                    "click",
                    "drag_and_drop",
                    "fill",
                    "key_press",
                    "select_option",
                    "set_storage_value",
                    "set_input_files",
                }:
                    state_producing = True
                if kind == "assert_count" and isinstance(action.get("selector"), str):
                    key = (active_route, str(action["selector"]))
                    count = int(action.get("count", 0))
                    if (
                        not state_producing
                        and not fixtures
                        and known_selector_exact.get(key) != count
                        and known_selector_min.get(key) != count
                    ):
                        errors.append(
                            f"{check_id} action {index}: exact count has no state-producing "
                            "setup or declared fixtures; use assert_visible or declare the "
                            "planned fixture literals"
                        )
                    known_selector_exact[key] = count
                if kind == "assert_url" and isinstance(action.get("value"), str):
                    active_route = str(action["value"])

            for index, action in enumerate(actions):
                if not isinstance(action, dict) or action.get("action") != "fill":
                    continue
                value = action.get("value")
                if not isinstance(value, str) or not value:
                    continue
                later_actions = [
                    item for item in actions[index + 1 :] if isinstance(item, dict)
                ]
                creates_value = any(
                    item.get("action")
                    in {"click", "drag_and_drop", "key_press", "select_option", "set_input_files"}
                    for item in later_actions
                )
                asserts_literal = any(
                    item.get("action") == "assert_text" and item.get("value") == value
                    for item in later_actions
                )
                if asserts_literal and not creates_value and value not in fixtures:
                    errors.append(
                        f"{check_id}: filter assertion literal {value!r} is not declared "
                        "in fixtures; bind pre-existing test data explicitly"
                    )

            # A persistence check that starts with reload may rely only on
            # state established by earlier checks in this same sprint. A prior
            # `assert_visible` proves one matching node, not two; accepted-tape
            # state from another sprint is deliberately not assumed here.
            if (
                check.get("category") == "persistence"
                and action_kinds[:1] == ["reload"]
            ):
                active_route = str(route)
                for action in actions:
                    if not isinstance(action, dict):
                        continue
                    kind = str(action.get("action", ""))
                    selector = action.get("selector")
                    if kind in TYPED_ASSERTION_ACTIONS and isinstance(selector, str):
                        required = (
                            int(action.get("count", 1))
                            if kind == "assert_count"
                            else 1
                        )
                        established = known_selector_min.get(
                            (active_route, selector), 0
                        )
                        if required > established:
                            errors.append(
                                f"{check_id}: persistence assertion for {selector!r} "
                                f"expects {required}, but current-sprint setup establishes "
                                f"only {established}; add explicit setup/assertion or lower "
                                "the contract to the proven state"
                            )
                    if kind == "assert_url":
                        destination = action.get("value")
                        if isinstance(destination, str):
                            active_route = destination

            active_route = str(route)
            for action in actions:
                if not isinstance(action, dict):
                    continue
                kind = str(action.get("action", ""))
                selector = action.get("selector")
                if kind in TYPED_ASSERTION_ACTIONS and isinstance(selector, str):
                    bound = int(action.get("count", 1)) if kind == "assert_count" else 1
                    key = (active_route, selector)
                    known_selector_min[key] = max(known_selector_min.get(key, 0), bound)
                if kind == "assert_url":
                    destination = action.get("value")
                    if isinstance(destination, str):
                        active_route = destination
            if (
                check.get("category") in {"functionality", "persistence"}
                and any(
                    action in {"click", "fill", "select_option", "set_input_files"}
                    for action in action_kinds
                )
            ):
                stateful_routes.add(route)
    if errors:
        raise PlannerValidationError(
            "Planning UI action contract validation failed:\n- "
            + "\n- ".join(dict.fromkeys(errors))
        )


def _check_cross_references(
    *,
    feature_list: dict[str, Any],
    sprint_plan: dict[str, Any],
    verification_plan: dict[str, Any],
) -> None:
    """检查单文件 schema 无法覆盖的跨文件引用关系。"""
    feature_ids = {str(feature["id"]) for feature in feature_list["features"]}
    total_sprints = int(sprint_plan["total_sprints"])
    valid_sprint_numbers = set(range(1, total_sprints + 1))

    # 1. feature.sprint 必须落在声明过的 sprint 区间内。
    for feature in feature_list["features"]:
        sprint_num = feature.get("sprint")
        if sprint_num not in valid_sprint_numbers:
            raise PlannerValidationError(
                f"Planner cross-ref failed: feature {feature.get('id')!r} is "
                f"assigned to sprint {sprint_num!r} which is outside "
                f"1..{total_sprints}."
            )

    # 2. sprint_plan 必须完整且唯一地声明 1..total_sprints。
    declared_sprint_numbers = sorted(
        sprint["number"] for sprint in sprint_plan["sprints"]
    )
    if declared_sprint_numbers != sorted(valid_sprint_numbers):
        raise PlannerValidationError(
            "Planner cross-ref failed: sprint_plan.sprints must declare each "
            f"sprint number 1..{total_sprints} exactly once; got "
            f"{declared_sprint_numbers}."
        )

    # 3. sprint.feature_ids 必须都能在 feature_list 中找到。
    for sprint in sprint_plan["sprints"]:
        for feature_id in sprint.get("feature_ids") or []:
            if str(feature_id) not in feature_ids:
                raise PlannerValidationError(
                    f"Planner cross-ref failed: sprint {sprint.get('number')} "
                    f"references unknown feature_id {feature_id!r}."
                )

    # 4. ui_verification_plan 中的 feature_id 必须真实存在。
    for sprint in verification_plan["sprints"]:
        for check in sprint.get("checks") or []:
            ref_id = check.get("feature_id")
            if str(ref_id) not in feature_ids:
                raise PlannerValidationError(
                    f"Planner cross-ref failed: ui_verification_plan check "
                    f"{check.get('id')!r} references unknown feature_id "
                    f"{ref_id!r}."
                )


def _check_sprint_size_caps(
    sprint_plan: dict[str, Any], config: HarnessConfig
) -> None:
    """按配置限制每个 sprint 的 deliverable 与 exit_criterion 数量。"""
    deliverable_cap = config.max_deliverables_per_sprint
    exit_criteria_cap = config.max_exit_criteria_per_sprint
    if config.planner_scope_mode == "expansive-data":
        deliverable_cap = min(deliverable_cap, 3)
        exit_criteria_cap = min(exit_criteria_cap, 3)
        total_sprints = int(sprint_plan["total_sprints"])
        if not 6 <= total_sprints <= 9:
            raise PlannerValidationError(
                "Planner wrote invalid expansive-data sprint_plan.json: "
                f"expected 6..9 sprints, got {total_sprints}."
            )
    for sprint in sprint_plan["sprints"]:
        deliverables = sprint["deliverables"]
        if len(deliverables) > deliverable_cap:
            raise PlannerValidationError(
                f"Planner wrote invalid sprint_plan.json: sprint {sprint['number']} has "
                f"{len(deliverables)} deliverables; max allowed is "
                f"{deliverable_cap}. Split into smaller sprints."
            )
        exit_criteria = sprint["exit_criteria"]
        if len(exit_criteria) > exit_criteria_cap:
            raise PlannerValidationError(
                f"Planner wrote invalid sprint_plan.json: sprint {sprint['number']} has "
                f"{len(exit_criteria)} exit_criteria; max allowed is "
                f"{exit_criteria_cap}. Split into smaller sprints."
            )
        if config.planner_scope_mode == "expansive-data":
            if len(deliverables) < 2 or len(exit_criteria) < 2:
                raise PlannerValidationError(
                    f"Planner wrote invalid expansive-data sprint {sprint['number']}: "
                    "each sprint needs 2..3 deliverables and 2..3 exit_criteria."
                )


def _initialize_accepted_sprints(file_comm: FileComm) -> None:
    if file_comm.read_accepted_sprints() is not None:
        return
    sprint_plan = file_comm.read_sprint_plan()
    if sprint_plan is None:
        # 正常链路下不会触发；前置校验已保证 sprint_plan 存在。
        raise PlannerValidationError(
            "Cannot initialize accepted_sprints.json: sprint_plan.json missing."
        )
    total_sprints = sprint_plan["total_sprints"]
    current_target = 1 if total_sprints > 0 else 0
    file_comm.write_accepted_sprints(
        {
            "accepted": [],
            "current_target": current_target,
            "last_evaluated_round": 0,
        }
    )


def _planner_trace_written_artifacts(trace_path: Path) -> set[str]:
    pending: list[tuple[str, str]] = []
    written: set[str] = set()
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "atomic_edit_plan":
                artifact = event.get("artifact")
                if isinstance(artifact, str):
                    written.add(Path(artifact).name)
            elif event.get("event") == "assistant":
                for call in ((event.get("message") or {}).get("tool_calls") or []):
                    function = call.get("function") if isinstance(call, dict) else None
                    if not isinstance(function, dict) or function.get("name") not in {
                        "write_file", "apply_patch",
                    }:
                        continue
                    raw_args = function.get("arguments", "{}")
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    raw_path = args.get("path") if isinstance(args, dict) else None
                    if isinstance(raw_path, str) and raw_path.startswith(".harness/"):
                        pending.append((str(function.get("name")), Path(raw_path).name))
            elif event.get("event") == "tool" and event.get("ok") is True:
                tool_name = str(event.get("name", ""))
                match = next(
                    (index for index, (name, _path) in enumerate(pending) if name == tool_name),
                    None,
                )
                if match is not None:
                    _name, path = pending.pop(match)
                    written.add(path)
    except (OSError, ValueError, TypeError, AttributeError):
        return set()
    return written


def _planner_trace_usage(trace_path: Path) -> dict[str, Any]:
    latest = {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}
    try:
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") not in {"usage", "run_error"}:
                continue
            usage = event.get("cumulative_usage")
            if isinstance(usage, dict):
                latest["input_tokens"] = int(usage.get("input_tokens") or 0)
                latest["output_tokens"] = int(usage.get("output_tokens") or 0)
            cost = event.get("estimated_cost_usd")
            if isinstance(cost, (int, float)) and cost >= 0:
                latest["estimated_cost_usd"] = float(cost)
    except (OSError, ValueError, TypeError):
        return {"input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}
    return latest


def recover_trace_proven_planner_checkpoint(
    file_comm: FileComm, config: HarnessConfig
) -> AgentRunStats | None:
    """Recover a valid, model-written plan after the process died before checkpointing."""
    trace_path = file_comm.dir / "traces" / "planner.jsonl"
    if not trace_path.is_file():
        return None
    edit_contract = read_edit_task_contract(file_comm.dir.parent)
    written = _planner_trace_written_artifacts(trace_path)
    try:
        if edit_contract is not None:
            from src.orchestration.atomic_edit_plan import (
                ATOMIC_EDIT_PLAN_NAME,
                materialize_atomic_edit_compatibility_bundle,
                read_atomic_edit_plan,
            )

            plan = read_atomic_edit_plan(file_comm.dir)
            if plan is None or ATOMIC_EDIT_PLAN_NAME not in written:
                return None
            materialize_atomic_edit_compatibility_bundle(
                file_comm=file_comm,
                instruction_delta="Recovered atomic Edit",
                plan=plan,
            )
        elif not _PLANNER_TRACE_ARTIFACTS.issubset(written):
            return None
        _validate_planning_bundle(file_comm, config)
    except (PlannerValidationError, ValidationError, ValueError, OSError):
        return None
    _initialize_accepted_sprints(file_comm)
    usage = _planner_trace_usage(trace_path)
    return AgentRunStats(
        cost_usd=float(usage["estimated_cost_usd"]),
        duration_ms=0,
        duration_api_ms=0,
        token_usage={
            "input_tokens": int(usage["input_tokens"]),
            "output_tokens": int(usage["output_tokens"]),
        },
        usage={"recovery": "trace_proven_planner_checkpoint", **usage},
        model_usage={},
    )


def _build_planner_prompt(
    config: HarnessConfig, user_prompt: str, workdir: Path,
    target_profile: dict | None = None,
) -> str:
    final_mode = ""
    if config.final_project_mode:
        final_mode = (
            "FINAL PROJECT MODE: Plan the complete requested product using a natural number of Sprints "
            "appropriate to its complexity. Keep each Sprint coherent and independently verifiable, "
            "but ensure the full roadmap ends in a polished, runnable final website with no requested "
            "features omitted. Do not optimize the roadmap for extracting edit or repair training samples. "
        )
    existing_frontend_guidance = ""
    frontend_dir = workdir / "frontend"
    if frontend_dir.is_dir():
        existing_frontend_guidance = (
            "An existing runnable frontend is already present in `frontend/`. Treat its current "
            "HTML/CSS/JS or framework stack as authoritative: plan only an in-place extension and "
            "do not propose a stack migration, scaffold replacement, or React/Vite conversion. "
        )
    return (
        f"Create a complete planning bundle for this product idea:\n\n"
        f"{user_prompt}\n\n"
        f"{target_profile_guidance(target_profile)}\n"
        f"{final_mode}"
        f"{existing_frontend_guidance}"
        f"Update the existing planning artifact files under .harness using only file editing tools such as "
        f"Write, Edit, and MultiEdit. Bash is unavailable for this task. "
        f"The Harness has already prepared the workdir, the .harness directory, and the required artifact files. "
        f"Replace the scaffold content in those files; do not create directories, rename files, or add alternate filenames. "
        f"Use paths relative to the workdir only; do not use absolute paths. "
        f"The workdir is: {workdir}"
    )


async def run_planner(
    config: HarnessConfig,
    user_prompt: str,
    file_comm: FileComm,
    workdir: Path,
) -> AgentRunStats:
    """运行 planner，并返回统一的执行统计信息。"""
    logger.info(f"[bold blue]Planner[/] starting for prompt: {user_prompt[:80]}...")
    workdir.mkdir(parents=True, exist_ok=True)
    file_comm.dir.mkdir(parents=True, exist_ok=True)
    file_comm.initialize_planning_artifacts()

    # A timed-out planning call often leaves a nearly-complete bundle.  Give a
    # real follow-up model the precise schema failure instead of making it
    # rediscover and rewrite every artifact (which is both costly and prone to
    # introducing new inconsistencies).
    repair_context = ""
    try:
        _validate_planning_bundle(file_comm, config)
    except PlannerValidationError as exc:
        repair_context = (
            "\n\nA previous planner attempt left an invalid bundle. Preserve valid "
            "artifacts and fix this exact validation failure before finishing:\n"
            f"{exc}\n"
        )

    prompt = _build_planner_prompt(
        config, user_prompt, workdir, file_comm.read_target_profile()
    ) + repair_context
    input_context = task_input_prompt_context(workdir)
    edit_contract = read_edit_task_contract(workdir)
    if edit_contract is not None:
        obligations = accepted_obligation_summary(file_comm.dir)
        prompt += (
            "\n\n## Harness-owned Edit contract\n"
            "This is an Edit of the accepted existing project, not a new product generation. "
            "Plan only the requested change. Put every UI check on the exact requested target "
            "route(s); all other discovered routes are protected.\n"
            f"Requested target routes: {edit_contract.get('requested_target_routes') or 'derive narrowly from the request'}\n"
            "Use exactly one Sprint for this one Edit transaction, including a coherent multi-route "
            "or multi-file change. If the request contains independent product changes, do not invent "
            "internal Sprints; report them as an unresolved conflict so upstream can split the request.\n"
            "In that Sprint add requirement_changes (add/refine/replace/withdraw with requirement_id, "
            "prior_requirement_ids, and rationale), non-empty impact_tags, unresolved_conflicts, "
            "visual_evidence (required/conditional/not_required), and visual_evidence_reason. "
            "Every UI check must repeat its requirement_id and relevant impact_tags.\n"
            "Historical accepted obligations, when available, are metadata for semantic conflict and "
            "impact decisions; do not copy them into the user instruction:\n"
            f"{json.dumps(obligations, ensure_ascii=False)}\n"
        )
    if input_context:
        prompt += "\n\n" + input_context

    result, cost, _assistant_text, permission_denials = await run_sdk_agent(
        prompt=prompt,
        config=config,
        workdir=workdir,
        model=config.planner_model,
        system_prompt=planner_system_prompt(config.planner_scope_mode),
        max_turns=config.planner_max_turns,
        allow_bash=False,
        stop_hooks=[_make_planner_stop_hook(file_comm, config)],
        trace_path=file_comm.dir / "traces" / "planner.jsonl",
        image_paths=task_input_image_paths(workdir),
    )

    _validate_planning_bundle(file_comm, config)
    _initialize_accepted_sprints(file_comm)

    if permission_denials:
        logger.warning(
            f"[bold blue]Planner[/] completed with permission denials: {permission_denials}"
        )

    logger.info(f"[bold blue]Planner[/] done. Cost: ${cost:.4f}")
    return build_agent_run_stats(result, model=config.planner_model)
