"""harness 的三个阶段实现。

每个阶段函数负责本阶段的检查点写入、成本累计与 phase_metrics 记录。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
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
from src.orchestration.cost_tracker import CostTracker
from src.orchestration.file_comm import FileComm
from src.orchestration.git_journal import commit_round
from src.orchestration.runtime import start_app_stack
from src.orchestration.sprint_state import SprintState
from src.utils.logger import get_logger
from src.utils.sdk_session import safe_sdk_session

logger = get_logger(__name__)


class Verdict(StrEnum):
    completed = "completed"
    accepted_review = "accepted_review"
    failed_review = "failed_review"


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


def _save_checkpoint(
    ctx: HarnessContext,
    phase: str,
    round_num: int,
    *,
    current_sprint: int | None = None,
    generator_mode: str | None = None,
    last_verdict: str | None = None,
    accepted_sprints_payload: dict[str, Any] | None = None,
    design_metadata: dict[str, Any] | None = None,
) -> None:
    """写入 harness 检查点。"""
    if accepted_sprints_payload is None:
        accepted_sprints_payload = ctx.file_comm.read_accepted_sprints() or {}
    state = {
        "last_completed_phase": phase,
        "round_num": round_num,
        "prompt": ctx.user_prompt,
        "costs": ctx.cost_tracker.breakdown.copy(),
        "phase_metrics": ctx.phase_metrics,
        "current_sprint": current_sprint,
        "generator_mode": generator_mode,
        "accepted_sprints": accepted_sprints_payload.get("accepted", []),
        "accepted_sprints_payload": accepted_sprints_payload,
        "last_verdict": last_verdict,
        "timestamp": datetime.now().isoformat(),
    }
    if design_metadata:
        state.update(design_metadata)
    ctx.file_comm.write_state(state)
    logger.debug(f"Checkpoint saved: {phase} (round {round_num})")


# ---- Planner 阶段 ----


async def run_planner_phase(ctx: HarnessContext) -> None:
    """执行一次 planner，并在完成后写入检查点。"""
    logger.info("[bold cyan]═" * 40)
    logger.info("[bold cyan]PHASE 1: PLAN")
    started = time.perf_counter()
    async with safe_sdk_session(phase_name="planner"):
        raw_stats = await run_planner(ctx.config, ctx.user_prompt, ctx.file_comm, ctx.workdir)
        _record_phase_stats(ctx, "planner", _coerce_stats(raw_stats), started_at=started)
        _save_checkpoint(ctx, "plan", 0, last_verdict="planned")


async def run_design_phase(ctx: HarnessContext) -> dict[str, Any]:
    """执行 design 阶段，并在完成后写入检查点。"""
    logger.info("[bold magenta]PHASE 2: DESIGN")
    result = await run_design_stage(ctx.config, ctx.file_comm, ctx.workdir)
    _save_checkpoint(
        ctx,
        "design",
        0,
        last_verdict=result.metadata.get("design_status"),
        design_metadata=result.metadata,
    )
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


def _record_build_log(
    ctx: HarnessContext,
    commit_result,
    round_num: int,
    sprint_num: int,
    mode: str,
) -> None:
    """将本轮构建提交结果追加到 build_log.md。"""
    if commit_result.success:
        short = (commit_result.commit_hash or "")[:7]
        empty = " (no changes)" if commit_result.was_empty else ""
        logger.info(
            f"[bold green]Generator commit[/] {short} round={round_num} "
            f"sprint={sprint_num} mode={mode}{empty}"
        )
        line = f"round {round_num:02d}/sprint_{sprint_num} ({mode}): git commit {short}{empty}"
    else:
        logger.warning(
            f"[bold yellow]Generator commit failed[/] round={round_num} "
            f"sprint={sprint_num}: {commit_result.error}"
        )
        line = (
            f"round {round_num:02d}/sprint_{sprint_num} ({mode}): "
            f"git commit FAILED — {commit_result.error}"
        )
    existing = ctx.file_comm.read_build_log() or ""
    ctx.file_comm.write_build_log((existing.rstrip() + "\n" + line + "\n").lstrip())


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
    async with safe_sdk_session(phase_name=f"generator round {round_num}"):
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

        # `prior_grade` 只在 repair 模式下有意义。
        prior_grade = (
            ctx.file_comm.read_grades(round_num - 1)
            if round_num > 1 and mode == "repair"
            else None
        )
        accepted = (ctx.file_comm.read_accepted_sprints() or {}).get("accepted", [])
        commit_result = await commit_round(
            ctx.workdir / "frontend",
            round_n=round_num,
            sprint_num=sprint_num,
            mode=mode,
            prior_grade=prior_grade,
            accepted=accepted,
        )
        _record_build_log(ctx, commit_result, round_num, sprint_num, mode)

        _save_checkpoint(
            ctx, f"build_r{round_num}", round_num,
            current_sprint=sprint_num,
            generator_mode=mode,
            last_verdict="awaiting_review",
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


async def run_evaluate_phase(ctx: HarnessContext, round_num: int) -> Verdict:
    """执行 evaluator 与视觉复核，推进 sprint 状态并写入检查点。"""
    logger.info("[bold yellow]EVALUATE phase")
    sprint_num = ctx.sprint_state.current_target
    sprint_ctx = ctx.sprint_state.sprint_context(sprint_num)
    started = time.perf_counter()

    async with safe_sdk_session(phase_name=f"evaluator round {round_num}"):
        app_stack = await start_app_stack(ctx.workdir, ctx.file_comm.dir, ctx.config, round_num)
        try:
            passed, grades, ev_stats = await run_evaluator(
                ctx.config, ctx.file_comm, ctx.workdir,
                round_num=round_num, app_url=app_stack.frontend_url,
            )
        finally:
            await app_stack.close()

    visual_manifest = ctx.file_comm.read_visual_manifest(round_num)
    if not visual_manifest:
        matches = sorted(ctx.file_comm.dir.glob(f"visual_round_{round_num}_*.png"))
        if matches:
            visual_manifest = {
                "round": round_num,
                "app_url": "",
                "screenshots": [f".harness/{p.name}" for p in matches],
                "notes": "",
            }

    _record_phase_stats(
        ctx,
        f"evaluator_r{round_num}",
        _coerce_stats(ev_stats),
        started_at=started,
    )

    vs_started = time.perf_counter()
    vs_stats = None
    async with safe_sdk_session(phase_name=f"visual review round {round_num}"):
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

    # 先在内存中计算 accepted_sprints 的下一版内容，确保检查点先落盘。
    # 如果两次写入之间进程中断，恢复执行时可用检查点中的 payload 重建。
    next_payload = ctx.sprint_state.compute_advance(
        sprint_num=sprint_num, round_num=round_num, recommendation=recommendation,
    )

    last_verdict = (
        "completed" if recommendation == "complete"
        else "accepted_review" if recommendation == "generate_next_sprint"
        else "failed_review"
    )
    _save_checkpoint(
        ctx, f"evaluate_r{round_num}", round_num,
        current_sprint=sprint_num,
        generator_mode="repair" if recommendation == "repair" else "generate",
        last_verdict=last_verdict,
        accepted_sprints_payload=next_payload,
    )

    # 再更新 accepted_sprints.json，本地文件与检查点由此保持同一推进顺序。
    ctx.sprint_state.advance(
        sprint_num=sprint_num, round_num=round_num, recommendation=recommendation,
    )

    return _build_verdict(recommendation)
