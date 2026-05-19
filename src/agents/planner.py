from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.agents.sdk_runner import AgentRunStats, build_agent_run_stats, run_sdk_agent
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.prompts.planner import PLANNER_SYSTEM_PROMPT
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

    spec = file_comm.read_spec()
    if not spec:
        raise PlannerValidationError(
            "Planner completed without writing .harness/spec.md."
        )
    if not all(header in spec for header in _REQUIRED_SPEC_HEADERS):
        raise PlannerValidationError(
            "Planner wrote invalid spec.md: required sections are missing."
        )

    try:
        design_tokens = file_comm.read_design_tokens()
        feature_list = file_comm.read_feature_list()
        sprint_plan = file_comm.read_sprint_plan()
        verification_plan = file_comm.read_ui_verification_plan()
    except ValidationError as exc:
        raise PlannerValidationError(
            f"Planner artifact failed schema validation:\n{exc}"
        ) from exc

    for artifact, filename in (
        (design_tokens, "design_tokens.json"),
        (feature_list, "feature_list.json"),
        (sprint_plan, "sprint_plan.json"),
        (verification_plan, "ui_verification_plan.json"),
    ):
        if artifact is None:
            raise PlannerValidationError(
                f"Planner completed without writing .harness/{filename}."
            )

    progress = file_comm.read_progress()
    if not progress.strip():
        raise PlannerValidationError(
            "Planner completed without writing .harness/progress.md."
        )

    assert feature_list is not None  # 缩窄类型，便于静态检查。
    assert sprint_plan is not None
    assert verification_plan is not None

    _check_cross_references(
        feature_list=feature_list,
        sprint_plan=sprint_plan,
        verification_plan=verification_plan,
    )
    _check_sprint_size_caps(sprint_plan, config)


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
    for sprint in sprint_plan["sprints"]:
        deliverables = sprint["deliverables"]
        if len(deliverables) > config.max_deliverables_per_sprint:
            raise PlannerValidationError(
                f"Planner wrote invalid sprint_plan.json: sprint {sprint['number']} has "
                f"{len(deliverables)} deliverables; max allowed is "
                f"{config.max_deliverables_per_sprint}. Split into smaller sprints."
            )
        exit_criteria = sprint["exit_criteria"]
        if len(exit_criteria) > config.max_exit_criteria_per_sprint:
            raise PlannerValidationError(
                f"Planner wrote invalid sprint_plan.json: sprint {sprint['number']} has "
                f"{len(exit_criteria)} exit_criteria; max allowed is "
                f"{config.max_exit_criteria_per_sprint}. Split into smaller sprints."
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

    prompt = (
        f"Create a complete planning bundle for this product idea:\n\n"
        f"{user_prompt}\n\n"
        f"Write all required planning artifacts into .harness using only file editing tools such as "
        f"Write, Edit, and MultiEdit. Bash is unavailable for this task. "
        f"The Harness has already prepared the workdir and .harness directory for this task, "
        f"so begin by writing the files themselves instead of creating directories. "
        f"Use paths relative to the workdir only; do not use absolute paths. "
        f"The workdir is: {workdir}"
    )

    result, cost, _assistant_text, permission_denials = await run_sdk_agent(
        prompt=prompt,
        config=config,
        workdir=workdir,
        model=config.planner_model,
        system_prompt=PLANNER_SYSTEM_PROMPT,
        max_turns=30,
        allow_bash=False,
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
