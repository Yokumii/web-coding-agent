"""harness 的三个阶段实现。

每个阶段函数负责本阶段的检查点写入、成本累计与 phase_metrics 记录。
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.agents.evaluator import _determine_passed, run_evaluator
from src.agents.design_stage import run_design_stage
from src.agents.generator import run_generator
from src.agents.planner import run_planner
from src.agents.sdk_runner import AgentRunStats
from src.agents.visual_review import (
    apply_dedicated_visual_review,
    render_feedback_from_grades,
)
from src.config import HarnessConfig
from src.orchestration.checkpoints import CheckpointTransaction
from src.orchestration.cost_tracker import CostTracker
from src.orchestration.file_comm import FileComm
from src.orchestration.edit_dom_guard import capture_baseline, evaluate_guard, is_forward_edit
from src.orchestration.runtime import start_app_stack
from src.orchestration.sprint_state import SprintState
from src.prompts.grading import evaluation_is_inconclusive
from src.utils.logger import get_logger
from src.utils.sdk_session import safe_sdk_session

logger = get_logger(__name__)


@asynccontextmanager
async def _agent_phase_session(ctx: "HarnessContext", *, phase_name: str):
    """Use the SDK cancellation guard only for the Claude SDK runtime.

    The native OpenAI runner has no SDK stream to clean up. Wrapping it in the
    guard can swallow a real cancellation and falsely let a phase finish
    without writing its checkpoint.
    """
    if ctx.config.agent_runtime.strip().lower() == "openai":
        yield
        return
    async with safe_sdk_session(phase_name=phase_name):
        yield


class Verdict(StrEnum):
    completed = "completed"
    accepted_review = "accepted_review"
    failed_review = "failed_review"


class EvaluationInfrastructureError(RuntimeError):
    """Evaluation provider/tooling failed; project code must not enter repair."""


@dataclass
class HarnessContext:
    workdir: Path
    config: HarnessConfig
    file_comm: FileComm
    cost_tracker: CostTracker
    sprint_state: SprintState
    phase_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    user_prompt: str = ""


# ---- 局部辅助函数 ----


def _coerce_stats(stats: AgentRunStats | float | int) -> AgentRunStats:
    """兼容旧测试桩的数值返回，统一转成 `AgentRunStats`。"""
    if isinstance(stats, AgentRunStats):
        return stats
    return AgentRunStats(
        cost_usd=float(stats),
        duration_ms=None,
        duration_api_ms=None,
        token_usage={},
        usage={},
        model_usage={},
    )


def _record_phase_stats(
    ctx: HarnessContext,
    phase_key: str,
    stats: AgentRunStats,
    *,
    started_at: float,
) -> AgentRunStats:
    """补写墙钟耗时，并同步更新成本与 phase_metrics。"""
    completed = stats.with_wall_duration(int((time.perf_counter() - started_at) * 1000))
    ctx.cost_tracker.add(phase_key, completed.cost_usd)
    ctx.phase_metrics[phase_key] = completed.to_dict()
    return completed


def _checkpoint_transaction(ctx: HarnessContext) -> CheckpointTransaction:
    return CheckpointTransaction(
        file_comm=ctx.file_comm,
        prompt=ctx.user_prompt,
        costs=ctx.cost_tracker.breakdown,
        phase_metrics=ctx.phase_metrics,
    )


# ---- Planner 阶段 ----


async def run_planner_phase(ctx: HarnessContext) -> None:
    """执行一次 planner，并在完成后写入检查点。"""
    logger.info("[bold cyan]═" * 40)
    logger.info("[bold cyan]PHASE 1: PLAN")
    started = time.perf_counter()
    async with _agent_phase_session(ctx, phase_name="planner"):
        raw_stats = await run_planner(ctx.config, ctx.user_prompt, ctx.file_comm, ctx.workdir)
        _record_phase_stats(ctx, "planner", _coerce_stats(raw_stats), started_at=started)
        _checkpoint_transaction(ctx).record_plan_completed()


async def run_design_phase(ctx: HarnessContext) -> dict[str, Any]:
    """执行 design 阶段，并在完成后写入检查点。"""
    logger.info("[bold magenta]PHASE 2: DESIGN")
    result = await run_design_stage(ctx.config, ctx.file_comm, ctx.workdir)
    _checkpoint_transaction(ctx).record_design_completed(result.metadata)
    return result.metadata


# ---- Build 阶段 ----


def _resume_requests_repair(
    resume_state: dict[str, Any] | None,
    *,
    round_num: int,
    sprint_num: int,
) -> bool:
    """判断恢复执行时是否应沿用 repair 模式。"""
    if not resume_state:
        return False
    if resume_state.get("last_completed_phase") != f"evaluate_r{round_num - 1}":
        return False
    if resume_state.get("last_verdict") != "failed_review":
        return False
    checkpoint_sprint = int(resume_state.get("current_sprint") or sprint_num)
    if checkpoint_sprint != sprint_num:
        return False
    checkpoint_mode = resume_state.get("generator_mode")
    return checkpoint_mode in {"repair", "generate"}


def _select_generator_mode(
    ctx: HarnessContext,
    round_num: int,
    sprint_num: int,
    resume_state: dict[str, Any] | None,
) -> str:
    """根据上一轮评分与恢复状态决定 generator 模式。"""
    if round_num == 1:
        return "generate"
    previous = ctx.file_comm.read_grades(round_num - 1) or {}
    if previous.get("mode_recommendation") == "repair" and previous.get("sprint") == sprint_num:
        return "repair"
    if _resume_requests_repair(
        resume_state,
        round_num=round_num,
        sprint_num=sprint_num,
    ):
        return "repair"
    return "generate"


async def run_build_phase(
    ctx: HarnessContext,
    round_num: int,
    *,
    resume_state: dict[str, Any] | None = None,
) -> None:
    """执行当前目标 sprint 的 generator，并写入检查点。"""
    logger.info("[bold green]BUILD phase")
    sprint_num = ctx.sprint_state.current_target
    mode = _select_generator_mode(ctx, round_num, sprint_num, resume_state)
    baseline_path = ctx.file_comm.dir / "edit_dom_baseline.json"
    if is_forward_edit(ctx.workdir) and not baseline_path.exists():
        # Capture the already accepted seed before the editor has a chance to
        # touch it.  A capture problem is infrastructure, not a repair signal.
        app_stack = await start_app_stack(ctx.workdir, ctx.file_comm.dir, ctx.config, round_num)
        try:
            await capture_baseline(
                workdir=ctx.workdir, file_comm=ctx.file_comm, config=ctx.config,
                app_url=app_stack.frontend_url,
            )
        finally:
            await app_stack.close()
    async with _agent_phase_session(ctx, phase_name=f"generator round {round_num}"):
        ctx.sprint_state.mark_sprint_in_progress(sprint_num)
        started = time.perf_counter()
        raw_stats = await run_generator(
            ctx.config, ctx.file_comm, ctx.workdir,
            round_num=round_num, sprint_num=sprint_num, mode=mode,
        )
        _record_phase_stats(
            ctx,
            f"generator_r{round_num}",
            _coerce_stats(raw_stats),
            started_at=started,
        )

        _checkpoint_transaction(ctx).record_build_completed(
            round_num=round_num,
            current_sprint=sprint_num,
            generator_mode=mode,
        )


# ---- Evaluate 阶段 ----


def _resolve_recommendation(
    ctx: HarnessContext,
    sprint_num: int,
    passed: bool,
    grades: dict[str, Any],
) -> str:
    """根据归一化后的通过状态生成唯一 recommendation。"""
    rec = grades.get("mode_recommendation")
    if not passed:
        if isinstance(rec, str) and rec and rec != "repair":
            logger.warning(
                f"[bold yellow]Evaluator[/] reported recommendation={rec!r} "
                f"for failed sprint {sprint_num}; normalizing to 'repair'."
            )
        return "repair"

    expected = "complete" if sprint_num >= ctx.sprint_state.total_sprints else "generate_next_sprint"
    if isinstance(rec, str) and rec and rec != expected:
        logger.warning(
            f"[bold yellow]Evaluator[/] reported recommendation={rec!r} "
            f"for passed sprint {sprint_num}; normalizing to {expected!r}."
        )
    return expected


def _normalize_grades_and_recommendation(
    ctx: HarnessContext,
    *,
    sprint_num: int,
    grades: dict[str, Any],
) -> tuple[dict[str, Any], bool, str]:
    """统一评分文件中的 passed / recommendation 字段，避免日志与推进状态分叉。"""
    passed = _determine_passed(grades)
    recommendation = _resolve_recommendation(
        ctx,
        sprint_num=sprint_num,
        passed=passed,
        grades=grades,
    )

    grades["overall_passed"] = passed
    if "sprint_passed" in grades:
        grades["sprint_passed"] = passed
    grades["mode_recommendation"] = recommendation
    return grades, passed, recommendation


def _build_verdict(recommendation: str) -> Verdict:
    """将 recommendation 映射为 harness 主循环使用的阶段判定。"""
    if recommendation == "complete":
        return Verdict.completed
    if recommendation == "generate_next_sprint":
        return Verdict.accepted_review
    return Verdict.failed_review


def _edit_guard_requires_repair(
    guard_result: dict[str, Any] | None,
    grades: dict[str, Any],
    *,
    evaluator_mode: str,
) -> bool:
    """Combine the mechanical contract with the independent scope audit."""
    if guard_result is None:
        return False
    if not guard_result.get("passed"):
        return True
    return evaluator_mode == "full" and grades.get("edit_scope_audit") != "pass"


async def run_evaluate_phase(ctx: HarnessContext, round_num: int) -> Verdict:
    """执行 evaluator 与视觉复核，推进 sprint 状态并写入检查点。"""
    logger.info("[bold yellow]EVALUATE phase")
    sprint_num = ctx.sprint_state.current_target
    sprint_ctx = ctx.sprint_state.sprint_context(sprint_num)
    started = time.perf_counter()
    ev_stats = None
    startup_error: Exception | None = None
    grades: dict[str, Any] = {}
    passed = False

    async with _agent_phase_session(ctx, phase_name=f"evaluator round {round_num}"):
        try:
            app_stack = await start_app_stack(ctx.workdir, ctx.file_comm.dir, ctx.config, round_num)
        except Exception as exc:
            startup_error = exc
            reason = f"Application startup failed: {type(exc).__name__}: {exc}"
            logger.warning(f"[bold red]{reason}[/]")
            grades = {
                "round": round_num,
                "sprint": sprint_num,
                "mode_recommendation": "repair",
                "phase_results": {
                    "render_gate": "fail",
                    "ui_functionality": "fail",
                    "appearance": "fail",
                    "source_inspection": "skipped",
                },
                "sprint_passed": False,
                "regression_passed": False,
                "overall_passed": False,
                "criteria": {
                    name: {"score": 0.0, "passed": False, "notes": reason}
                    for name in ("design_quality", "functionality", "originality", "craft")
                },
                "bugs_found": [reason],
                "repair_instructions": [
                    "Make `npm run dev -- --host HOST --port PORT --strictPort` honor the supplied host and port, then verify the application starts successfully."
                ],
            }
            passed = False
        else:
            try:
                guard_result = await evaluate_guard(
                    workdir=ctx.workdir, file_comm=ctx.file_comm, config=ctx.config,
                    app_url=app_stack.frontend_url, round_num=round_num,
                )
                passed, grades, ev_stats = await run_evaluator(
                    ctx.config, ctx.file_comm, ctx.workdir,
                    round_num=round_num, app_url=app_stack.frontend_url, edit_guard=guard_result,
                )
                if guard_result is not None:
                    grades["edit_guard"] = guard_result
                    if _edit_guard_requires_repair(
                        guard_result, grades, evaluator_mode=ctx.config.evaluator_mode
                    ):
                        passed = False
                        grades["regression_passed"] = False
                        grades["overall_passed"] = False
                        grades.setdefault("regressions_found", []).append(
                            "Edit guard failed: "
                            + str(guard_result.get("violations") or guard_result.get("reason")
                                  or "the independent scope audit did not pass")
                        )
                        grades.setdefault("repair_instructions", []).append(
                            "Restore every out-of-scope DOM/ARIA surface, or narrow the edit to the declared roots."
                        )
            finally:
                await app_stack.close()

    if not passed and evaluation_is_inconclusive(grades):
        reason = (
            "Evaluator did not reproduce a concrete defect; all negative findings "
            "are explicitly unverified. Retry evaluation instead of repairing code."
        )
        grades["evaluation_infrastructure_failure"] = {
            "phase": "evaluator_coverage",
            "reason": reason,
        }
        ctx.file_comm.write_grades(round_num, grades)
        ctx.file_comm.write_feedback(round_num, render_feedback_from_grades(grades))
        raise EvaluationInfrastructureError(reason)

    visual_manifest = ctx.file_comm.read_visual_manifest(round_num)

    if ev_stats is not None:
        _record_phase_stats(
            ctx,
            f"evaluator_r{round_num}",
            _coerce_stats(ev_stats),
            started_at=started,
        )

    vs_started = time.perf_counter()
    vs_stats = None
    if ctx.config.evaluator_mode == "full" and startup_error is None:
        async with _agent_phase_session(ctx, phase_name=f"visual review round {round_num}"):
            grades, vs_stats = await apply_dedicated_visual_review(
                config=ctx.config,
                file_comm=ctx.file_comm,
                workdir=ctx.workdir,
                round_num=round_num,
                sprint_num=sprint_num,
                sprint_context=sprint_ctx,
                grades=grades,
                manifest=visual_manifest,
            )
    if vs_stats is not None:
        _record_phase_stats(
            ctx,
            f"visual_score_r{round_num}",
            _coerce_stats(vs_stats),
            started_at=vs_started,
        )

    if grades:
        ctx.file_comm.write_grades(round_num, grades)
        ctx.file_comm.write_feedback(round_num, render_feedback_from_grades(grades))

    infra_failure = grades.get("evaluation_infrastructure_failure")
    if infra_failure:
        reason = str(infra_failure.get("reason", "unknown evaluation failure"))
        raise EvaluationInfrastructureError(
            "Evaluation infrastructure failed; refusing to create a code repair round: "
            + reason
        )

    grades, passed, recommendation = _normalize_grades_and_recommendation(
        ctx,
        sprint_num=sprint_num,
        grades=grades,
    )
    if isinstance(grades.get("criteria"), dict) and "round" in grades:
        ctx.file_comm.write_grades(round_num, grades)
        ctx.file_comm.write_feedback(round_num, render_feedback_from_grades(grades))

    final_status = "[bold green]PASSED[/]" if passed else "[bold red]FAILED[/]"
    logger.info(f"[bold yellow]Evaluator[/] round {round_num} final verdict {final_status}.")

    ctx.sprint_state.mark_sprint_outcome(sprint_num, recommendation=recommendation, grades=grades)

    _checkpoint_transaction(ctx).record_evaluate_completed(
        sprint_state=ctx.sprint_state,
        round_num=round_num,
        sprint_num=sprint_num,
        recommendation=recommendation,
    )

    return _build_verdict(recommendation)
