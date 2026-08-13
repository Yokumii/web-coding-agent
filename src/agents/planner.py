from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from src.agents.sdk_runner import AgentRunStats, build_agent_run_stats, run_sdk_agent
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.target_profile import target_profile_guidance
from src.prompts.planner import planner_system_prompt
from src.utils.logger import get_logger

logger = get_logger(__name__)

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


def _check_action_contracts(verification_plan: dict[str, Any]) -> None:
    """Reject planner-authored tests that would fabricate a product failure."""
    for sprint in verification_plan.get("sprints") or []:
        for check in sprint.get("checks") or []:
            check_id = str(check.get("id", "unknown"))
            route = check.get("route", "/")
            if not isinstance(route, str) or not route:
                raise PlannerValidationError(
                    f"Planner action contract {check_id} route must be a non-empty same-origin path."
                )
            parsed_route = urlsplit(route)
            route_segments = parsed_route.path.replace("\\", "/").split("/")
            if (
                not route.startswith("/")
                or route.startswith("//")
                or "\\" in route
                or parsed_route.scheme
                or parsed_route.netloc
                or parsed_route.query
                or parsed_route.fragment
                or any(segment in {".", ".."} for segment in route_segments)
            ):
                raise PlannerValidationError(
                    f"Planner action contract {check_id} route must be a safe same-origin path; got {route!r}."
                )
            actions = check.get("actions") or []
            if not actions:  # Backwards compatibility for legacy plans.
                continue
            if str(actions[-1].get("action")) != "evaluate":
                raise PlannerValidationError(
                    f"Planner action contract {check_id} must end with evaluate "
                    "so the observable result is asserted."
                )
            evaluate_count = sum(
                1 for action in actions if str(action.get("action")) == "evaluate"
            )
            if evaluate_count != 1:
                raise PlannerValidationError(
                    f"Planner action contract {check_id} must contain exactly one final evaluate; "
                    "combine related assertions into one boolean expression."
                )
            for action in actions:
                kind = str(action.get("action", ""))
                if "settle_ms" in action:
                    settle_ms = action.get("settle_ms")
                    if (
                        isinstance(settle_ms, bool)
                        or not isinstance(settle_ms, int)
                        or not 0 <= settle_ms <= 5_000
                    ):
                        raise PlannerValidationError(
                            f"Planner action contract {check_id} settle_ms must be an integer from 0 to 5000."
                        )
                if kind == "scroll":
                    y = action.get("y")
                    if isinstance(y, bool) or not isinstance(y, int):
                        raise PlannerValidationError(
                            f"Planner action contract {check_id} scroll action requires integer y."
                        )
                if kind == "evaluate":
                    expression = str(action.get("expression", "")).strip()
                    if not expression:
                        raise PlannerValidationError(
                            f"Planner action contract {check_id} evaluate requires an expression."
                        )
                    if "return " in expression or expression.startswith("return"):
                        raise PlannerValidationError(
                            f"Planner action contract {check_id} contains a top-level return; "
                            "write a directly evaluable boolean expression instead."
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

    result, cost, _assistant_text, permission_denials = await run_sdk_agent(
        prompt=prompt,
        config=config,
        workdir=workdir,
        model=config.planner_model,
        system_prompt=planner_system_prompt(config.planner_scope_mode),
        max_turns=16,
        allow_bash=False,
        stop_hooks=[_make_planner_stop_hook(file_comm, config)],
        trace_path=file_comm.dir / "traces" / "planner.jsonl",
    )

    _validate_planning_bundle(file_comm, config)
    _initialize_accepted_sprints(file_comm)

    if permission_denials:
        logger.warning(
            f"[bold blue]Planner[/] completed with permission denials: {permission_denials}"
        )

    logger.info(f"[bold blue]Planner[/] done. Cost: ${cost:.4f}")
    return build_agent_run_stats(result, model=config.planner_model)
