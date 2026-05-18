from __future__ import annotations
import asyncio
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from src.agents.visual_review import apply_dedicated_visual_review, render_feedback_from_grades
from src.agents.sdk_runner import AgentRunStats
from src.agents.design_stage import run_design_stage
from src.agents.evaluator import run_evaluator
from src.agents.generator import run_generator
from src.agents.planner import run_planner
from src.config import HarnessConfig
from src.orchestration.cost_tracker import CostTracker
from src.orchestration.file_comm import FileComm
from src.orchestration.git_journal import commit_round
from src.orchestration.runtime import start_app_stack
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _save_checkpoint(
    file_comm: FileComm,
    phase: str,
    round_num: int,
    prompt: str,
    cost_tracker: CostTracker,
    *,
    phase_metrics: dict[str, dict[str, Any]] | None = None,
    current_sprint: int | None = None,
    generator_mode: str | None = None,
    last_verdict: str | None = None,
    accepted_sprints_payload: dict[str, Any] | None = None,
    design_metadata: dict[str, Any] | None = None,
) -> None:
    """Persist the harness checkpoint.

    ``accepted_sprints_payload`` is the full ``accepted_sprints.json``
    dict that *should* be on disk after this checkpoint. The harness now
    writes the checkpoint BEFORE rewriting ``accepted_sprints.json`` so
    that, if a crash happens between the two writes, resume can
    reconcile the file from the checkpoint. For phases that
    do not change ``accepted_sprints.json`` (plan / build), pass ``None``
    and the existing file is read back into the checkpoint.
    """
    if accepted_sprints_payload is None:
        accepted_sprints_payload = file_comm.read_accepted_sprints() or {}

    state = {
        "last_completed_phase": phase,
        "round_num": round_num,
        "prompt": prompt,
        "costs": cost_tracker.breakdown.copy(),
        "phase_metrics": phase_metrics or {},
        "current_sprint": current_sprint,
        "generator_mode": generator_mode,
        "accepted_sprints": accepted_sprints_payload.get("accepted", []),
        "accepted_sprints_payload": accepted_sprints_payload,
        "last_verdict": last_verdict,
        "timestamp": datetime.now().isoformat(),
    }
    if design_metadata:
        state.update(design_metadata)
    file_comm.write_state(state)
    logger.debug(f"Checkpoint saved: {phase} (round {round_num})")


def _restore_costs(cost_tracker: CostTracker, costs: dict[str, float]) -> None:
    for name, cost in costs.items():
        cost_tracker.add(name, cost)


def _coerce_agent_run_stats(stats: AgentRunStats | float | int) -> AgentRunStats:
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


def _clear_current_task_cancellation() -> int:
    task = asyncio.current_task()
    if task is None:
        return 0

    cleared = 0
    while task.cancelling():
        task.uncancel()
        cleared += 1
    return cleared


async def _close_app_stack_safely(app_stack) -> None:
    """Close the dev server stack without letting leaked cancellations win.

    Some SDK-backed evaluator shutdown paths can leave the harness task in a
    cancelling state *after* a successful result has already been returned.
    If that leaked cancellation lands on the next await, the harness loses the
    completed evaluation checkpoint while merely trying to stop the frontend.
    Run the close operation in a shielded child task, then keep clearing any
    leaked parent-task cancellation until the cleanup task has really finished.
    """
    close_task = asyncio.create_task(app_stack.close(), name="app_stack_close")
    suppressed = 0
    try:
        while True:
            try:
                await asyncio.shield(close_task)
                break
            except asyncio.CancelledError:
                cleared = _clear_current_task_cancellation()
                suppressed += max(cleared, 1)
                if close_task.done():
                    break

        if close_task.cancelled():
            logger.warning(
                "[bold yellow]App stack cleanup task was cancelled after evaluation; "
                "continuing with the completed round state.[/]"
            )
            return

        exc = close_task.exception()
        if exc is not None:
            raise exc

        if suppressed > 0:
            logger.warning(
                "[bold yellow]Suppressed leaked cancellation during app stack cleanup "
                "after evaluation.[/]"
            )
    finally:
        if not close_task.done():
            close_task.cancel()


async def _drain_post_success_cancellation(*, phase: str) -> None:
    """Clear delayed cancellations leaked after a successful SDK phase.

    Some async-generator finalizers schedule parent-task cancellation one
    event-loop turn *after* the successful result and stack cleanup have
    already completed. If the harness proceeds directly, that stale
    cancellation can strike the next unrelated await and abort checkpoint
    persistence after a phase that already finished successfully.
    """
    suppressed = 0
    while True:
        try:
            await asyncio.sleep(0)
            break
        except asyncio.CancelledError:
            cleared = _clear_current_task_cancellation()
            suppressed += max(cleared, 1)

    if suppressed > 0:
        logger.warning(
            f"[bold yellow]Suppressed leaked cancellation after {phase} success "
            "while finalizing round state.[/]"
        )


async def _await_post_success_step(awaitable, *, phase: str):
    """Wait for a post-success async step without letting leaked cancellation win.

    After a successful SDK-backed phase, delayed parent-task cancellation can
    still arrive during later awaits that are only housekeeping for that
    already-successful round. Run those awaits in a child task, shield them,
    and keep clearing leaked parent cancellation until the child task finishes.
    """
    task = asyncio.create_task(awaitable, name=f"post_success:{phase}")
    suppressed = 0
    try:
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cleared = _clear_current_task_cancellation()
                suppressed += max(cleared, 1)
                if task.done():
                    break

        if task.cancelled():
            raise RuntimeError(f"{phase} task was cancelled")

        exc = task.exception()
        if exc is not None:
            raise exc

        if suppressed > 0:
            logger.warning(
                f"[bold yellow]Suppressed leaked cancellation while awaiting {phase} "
                "after evaluation success.[/]"
            )

        return task.result()
    finally:
        if not task.done():
            task.cancel()


def _copy_phase_metrics(existing: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(existing, dict):
        return {}

    copied: dict[str, dict[str, Any]] = {}
    for phase_name, payload in existing.items():
        if isinstance(payload, dict):
            copied[phase_name] = dict(payload)
    return copied


def _resume_phase_kind(phase: str | None) -> str | None:
    if not phase:
        return None
    if phase == "plan":
        return "plan"
    if phase == "design":
        return "design"
    if phase.startswith("build_r"):
        return "build"
    if phase.startswith("evaluate_r"):
        return "evaluate"
    return phase


def _get_total_sprints(file_comm: FileComm) -> int:
    sprint_plan = file_comm.read_sprint_plan() or {}
    if isinstance(sprint_plan.get("total_sprints"), int):
        return sprint_plan["total_sprints"]
    return len(sprint_plan.get("sprints", []))


def _get_current_target_sprint(file_comm: FileComm) -> int:
    accepted_sprints = file_comm.read_accepted_sprints() or {}
    return int(accepted_sprints.get("current_target", 1))


def _select_generator_mode(
    file_comm: FileComm,
    round_num: int,
    sprint_num: int,
    resume_state: dict[str, Any] | None = None,
) -> str:
    if round_num == 1:
        return "generate"

    previous_grades = file_comm.read_grades(round_num - 1) or {}
    previous_recommendation = previous_grades.get("mode_recommendation")
    previous_sprint = previous_grades.get("sprint")

    if previous_recommendation == "repair" and previous_sprint == sprint_num:
        return "repair"

    if resume_state and resume_state.get("last_completed_phase") == f"evaluate_r{round_num - 1}":
        if resume_state.get("last_verdict") == "failed_review":
            checkpoint_sprint = int(resume_state.get("current_sprint") or sprint_num)
            if checkpoint_sprint == sprint_num:
                checkpoint_mode = resume_state.get("generator_mode")
                if checkpoint_mode in {"repair", "generate"}:
                    return "repair"

    return "generate"


def _compute_accepted_sprints_after_evaluation(
    file_comm: FileComm,
    *,
    round_num: int,
    sprint_num: int,
    recommendation: str,
) -> dict:
    accepted_sprints = file_comm.read_accepted_sprints() or {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 0,
    }
    accepted = list(accepted_sprints.get("accepted", []))

    if recommendation in {"generate_next_sprint", "complete"} and sprint_num not in accepted:
        accepted.append(sprint_num)
        accepted.sort()

    if recommendation in {"generate_next_sprint", "complete"}:
        next_target = sprint_num + 1
    else:
        next_target = sprint_num

    return {
        "accepted": accepted,
        "current_target": next_target,
        "last_evaluated_round": round_num,
    }


def _resolve_evaluation_recommendation(
    *,
    file_comm: FileComm,
    round_num: int,
    sprint_num: int,
    passed: bool,
    grades: dict,
) -> str:
    recommendation = grades.get("mode_recommendation")
    if isinstance(recommendation, str) and recommendation:
        return recommendation

    if not passed:
        return "repair"

    total_sprints = _get_total_sprints(file_comm)
    if sprint_num >= total_sprints:
        return "complete"
    return "generate_next_sprint"


def _feature_id_set_for_sprint(file_comm: FileComm, sprint_num: int) -> set[str]:
    sprint_plan = file_comm.read_sprint_plan() or {}
    for sprint in sprint_plan.get("sprints", []):
        if sprint.get("number") == sprint_num:
            feature_ids = sprint.get("feature_ids", [])
            return {str(feature_id) for feature_id in feature_ids}
    return set()


def _update_feature_statuses_for_sprint(file_comm: FileComm, sprint_num: int, status: str) -> None:
    feature_list = file_comm.read_feature_list()
    if not feature_list:
        return

    target_ids = _feature_id_set_for_sprint(file_comm, sprint_num)
    if not target_ids:
        return

    changed = False
    for feature in feature_list.get("features", []):
        if feature.get("id") in target_ids and feature.get("status") != status:
            feature["status"] = status
            changed = True

    if changed:
        file_comm.write_feature_list(feature_list)


def _collect_failing_feature_ids(file_comm: FileComm, sprint_num: int, grades: dict[str, Any]) -> set[str]:
    target_ids = _feature_id_set_for_sprint(file_comm, sprint_num)
    if not target_ids:
        return set()

    failing_ids: set[str] = set()

    for check in grades.get("ui_checks", []):
        if not isinstance(check, dict):
            continue
        feature_id = str(check.get("feature_id", "")).strip()
        status = str(check.get("status", "")).strip().lower()
        if feature_id and feature_id in target_ids and status in {"fail", "partial"}:
            failing_ids.add(feature_id)

    for result in grades.get("target_exit_criteria_results", []):
        if not isinstance(result, dict):
            continue
        feature_id = str(result.get("feature_id", "")).strip()
        passed = result.get("passed")
        if feature_id and feature_id in target_ids and passed is False:
            failing_ids.add(feature_id)

    if failing_ids:
        return failing_ids

    if grades.get("overall_passed") is False:
        return set(target_ids)

    return set()


def _apply_post_evaluation_feature_statuses(
    file_comm: FileComm,
    sprint_num: int,
    recommendation: str,
    grades: dict[str, Any],
) -> None:
    target_ids = _feature_id_set_for_sprint(file_comm, sprint_num)
    if not target_ids:
        return

    feature_list = file_comm.read_feature_list()
    if not feature_list:
        return

    failing_ids = _collect_failing_feature_ids(file_comm, sprint_num, grades)
    changed = False

    for feature in feature_list.get("features", []):
        feature_id = feature.get("id")
        if feature_id not in target_ids:
            continue

        next_status = feature.get("status")
        if recommendation in {"generate_next_sprint", "complete"}:
            next_status = "accepted"
        elif feature_id in failing_ids:
            next_status = "repair_required"
        else:
            next_status = "implemented"

        if feature.get("status") != next_status:
            feature["status"] = next_status
            changed = True

    if changed:
        file_comm.write_feature_list(feature_list)


def _restore_resume_state(file_comm: FileComm, existing_state: dict[str, Any]) -> None:
    """Reconcile ``accepted_sprints.json`` with the resumed checkpoint.

    Two cases:

    1. Checkpoints written by the new code carry the full
       ``accepted_sprints_payload``. If the file disagrees with that
       payload (e.g. crashed mid-write somewhere), the file is rewritten
       from the checkpoint — the checkpoint is the source of truth.

    2. Legacy / pre-fix checkpoints only have the list of accepted
       sprint numbers and a ``current_sprint`` field. If the file is
       missing entirely, restore from those. Additionally, if the file
       claims to be ahead of the checkpoint's last completed phase
       (e.g. ``last_completed_phase=build_rN`` but the file already
       advanced past round N), the file is treated as stale and rewound
       to match the checkpoint.
    """
    payload = existing_state.get("accepted_sprints_payload")
    file_value = file_comm.read_accepted_sprints()

    if isinstance(payload, dict) and "accepted" in payload:
        if file_value != payload:
            file_comm.write_accepted_sprints(payload)
        return

    last_phase = str(existing_state.get("last_completed_phase") or "")
    state_round = int(existing_state.get("round_num") or 0)
    state_accepted = existing_state.get("accepted_sprints")
    state_current_sprint = existing_state.get("current_sprint")

    if (
        last_phase.startswith("build_r")
        and isinstance(file_value, dict)
        and state_current_sprint is not None
    ):
        # The file claims this round's evaluate already completed, but the
        # checkpoint says we only got as far as build. The file got ahead;
        # rewind it.
        file_advanced_past_state = (
            file_value.get("last_evaluated_round", 0) >= state_round
            or file_value.get("current_target", 0) > int(state_current_sprint)
        )
        if file_advanced_past_state:
            file_comm.write_accepted_sprints({
                "accepted": list(state_accepted or []),
                "current_target": int(state_current_sprint),
                "last_evaluated_round": max(state_round - 1, 0),
            })
            return

    if state_accepted is None or file_value is not None:
        return

    current_sprint = int(state_current_sprint or 1)
    file_comm.write_accepted_sprints({
        "accepted": state_accepted,
        "current_target": current_sprint,
        "last_evaluated_round": state_round,
    })


def _get_sprint_context(file_comm: FileComm, sprint_num: int) -> dict[str, Any]:
    sprint_plan = file_comm.read_sprint_plan() or {}
    for sprint in sprint_plan.get("sprints", []):
        if sprint.get("number") == sprint_num:
            return sprint
    return {}


def _resolve_start_round(
    resume_from: str | None,
    existing_state: dict[str, Any] | None,
    file_comm: FileComm,
) -> int:
    if not resume_from or not existing_state:
        return 1

    if resume_from.startswith("build_r"):
        return int(existing_state["round_num"])

    if resume_from.startswith("evaluate_r"):
        last_verdict = existing_state.get("last_verdict")
        if last_verdict == "completed":
            return int(existing_state["round_num"])
        return int(existing_state["round_num"]) + 1

    return 1


def _default_design_metadata(requested_mode: str) -> dict[str, Any]:
    if requested_mode == "image-first":
        return {
            "requested_design_mode": "image-first",
            "design_mode": "pending",
            "design_status": "pending",
            "approved_concept_path": None,
            "background_ui_path": None,
        }
    return {
        "requested_design_mode": "text-only",
        "design_mode": "text_only",
        "design_status": "not_requested",
        "approved_concept_path": None,
        "background_ui_path": None,
    }


def _restore_design_metadata(
    existing_state: dict[str, Any] | None,
    requested_mode: str,
) -> dict[str, Any]:
    metadata = _default_design_metadata(requested_mode)
    if not existing_state:
        return metadata

    for key in (
        "requested_design_mode",
        "design_mode",
        "design_status",
        "approved_concept_path",
        "background_ui_path",
    ):
        if key in existing_state:
            metadata[key] = existing_state[key]
    return metadata


async def run_harness(
    user_prompt: str,
    workdir: Path,
    config: HarnessConfig,
    plan_only: bool = False,
    resume: bool = False,
    keep_frontend: bool = False,
) -> None:
    """Run the full Planner → Generator → Evaluator harness."""
    start = time.time()
    harness_dir = workdir / ".harness"
    file_comm = FileComm(harness_dir)
    cost_tracker = CostTracker(config.max_budget_usd)

    workdir.mkdir(parents=True, exist_ok=True)

    # Check for existing checkpoint
    existing_state = file_comm.read_state()
    resume_from = None
    phase_metrics = _copy_phase_metrics((existing_state or {}).get("phase_metrics"))
    requested_design_mode = (
        str((existing_state or {}).get("requested_design_mode") or config.design_mode)
        if resume
        else config.design_mode
    )
    design_metadata = _restore_design_metadata(existing_state, requested_design_mode)
    if resume and existing_state:
        resume_from = existing_state["last_completed_phase"]
        user_prompt = existing_state.get("prompt", user_prompt)
        _restore_costs(cost_tracker, existing_state.get("costs", {}))
        _restore_resume_state(file_comm, existing_state)
        logger.info(
            f"[bold]Harness resuming[/] from phase '{resume_from}' "
            f"(round {existing_state['round_num']})"
        )
    elif resume:
        logger.warning("[bold]Resume requested but no checkpoint state was found.[/]")
    else:
        file_comm.reset_run_artifacts()
        if not keep_frontend:
            _reset_frontend_dir(workdir)
        logger.info(f"[bold]Harness started[/] — prompt: {user_prompt[:80]}...")

    logger.info(f"Workdir: {workdir}")

    # Phase 1: Plan
    if _resume_phase_kind(resume_from) not in ("plan", "design", "build", "evaluate"):
        logger.info("[bold cyan]═" * 40)
        logger.info("[bold cyan]PHASE 1: PLAN")
        planner_started_at = time.perf_counter()
        planner_stats = _coerce_agent_run_stats(
            await run_planner(config, user_prompt, file_comm, workdir)
        ).with_wall_duration(int((time.perf_counter() - planner_started_at) * 1000))
        cost_tracker.add("planner", planner_stats.cost_usd)
        phase_metrics["planner"] = planner_stats.to_dict()
        _save_checkpoint(
            file_comm,
            "plan",
            0,
            user_prompt,
            cost_tracker,
            phase_metrics=phase_metrics,
            last_verdict="planned",
            design_metadata=design_metadata,
        )

        if plan_only:
            logger.info("[bold]--plan-only mode:[/] stopping after planner")
            spec = file_comm.read_spec()
            logger.info(f"Spec written to {harness_dir / 'spec.md'}")
            _print_summary(cost_tracker, time.time() - start, 0, True)
            return

        if cost_tracker.is_over_budget():
            logger.warning("[bold red]Budget exceeded after planning. Stopping.[/]")
            return
    else:
        logger.info("[bold cyan]PHASE 1: PLAN[/] — [dim]skipped (checkpoint)[/]")

    if requested_design_mode == "image-first":
        if _resume_phase_kind(resume_from) not in ("design", "build", "evaluate"):
            logger.info("[bold cyan]PHASE 1.5: DESIGN")
            design_result = await run_design_stage(config, file_comm, workdir)
            design_metadata = design_result.metadata
            _save_checkpoint(
                file_comm,
                "design",
                0,
                user_prompt,
                cost_tracker,
                phase_metrics=phase_metrics,
                last_verdict=design_metadata["design_status"],
                design_metadata=design_metadata,
            )
        else:
            logger.info("[bold cyan]PHASE 1.5: DESIGN[/] - skipped (checkpoint)")

    start_round = _resolve_start_round(resume_from, existing_state, file_comm)

    current_target = _get_current_target_sprint(file_comm)
    total_sprints = _get_total_sprints(file_comm)
    if current_target > total_sprints > 0:
        logger.info("[bold green]All sprints already accepted.[/]")
        _print_summary(cost_tracker, time.time() - start, max(start_round - 1, 0), True)
        return

    if start_round > config.max_rounds:
        logger.info(f"[bold]All rounds completed (max_rounds={config.max_rounds}).[/]")
        _print_summary(cost_tracker, time.time() - start, config.max_rounds, False)
        return

    for round_num in range(start_round, config.max_rounds + 1):
        logger.info(f"[bold cyan]═" * 40)
        logger.info(f"[bold cyan]ROUND {round_num}/{config.max_rounds}")

        # Phase 2: Build
        if resume_from and resume_from == f"build_r{round_num}":
            logger.info(f"[bold green]BUILD phase[/] — [dim]skipped (checkpoint)[/]")
        else:
            logger.info("[bold green]BUILD phase")
            sprint_num = _get_current_target_sprint(file_comm)
            mode = _select_generator_mode(file_comm, round_num, sprint_num, existing_state)
            _update_feature_statuses_for_sprint(file_comm, sprint_num, "in_progress")
            build_started_at = time.perf_counter()
            generator_stats = _coerce_agent_run_stats(
                await run_generator(
                    config, file_comm, workdir,
                    round_num=round_num,
                    sprint_num=sprint_num,
                    mode=mode,
                )
            ).with_wall_duration(int((time.perf_counter() - build_started_at) * 1000))
            _update_feature_statuses_for_sprint(file_comm, sprint_num, "implemented")
            cost_tracker.add(f"generator_r{round_num}", generator_stats.cost_usd)
            phase_metrics[f"generator_r{round_num}"] = generator_stats.to_dict()
            # Snapshot the generator's frontend output as a linear commit on
            # main inside <workdir>/frontend/. Runs even when there are no
            # file changes (--allow-empty) so every round shows up in
            # `git log`. Failures are recorded in build_log.md and never
            # abort the run (commit_round is contractually never-raises).
            # `prior_grade` is only meaningful in repair mode (same-sprint
            # retry). For `generate` rounds — round 1, or a fresh sprint
            # advanced to after a previous sprint was accepted — the
            # previous round's grade belongs to a *different* sprint, so
            # surfacing it as "prior grade" would mislead a reader of
            # `git log`.
            prior_grade = (
                file_comm.read_grades(round_num - 1)
                if round_num > 1 and mode == "repair"
                else None
            )
            accepted_for_msg = (file_comm.read_accepted_sprints() or {}).get("accepted", [])
            commit_result = await commit_round(
                workdir / "frontend",
                round_n=round_num,
                sprint_num=sprint_num,
                mode=mode,
                prior_grade=prior_grade,
                accepted=accepted_for_msg,
            )
            if commit_result.success:
                short = (commit_result.commit_hash or "")[:7]
                empty_marker = " (no changes)" if commit_result.was_empty else ""
                logger.info(
                    f"[bold green]Generator commit[/] {short} round={round_num} "
                    f"sprint={sprint_num} mode={mode}{empty_marker}"
                )
                build_log_line = (
                    f"round {round_num:02d}/sprint_{sprint_num} ({mode}): "
                    f"git commit {short}{empty_marker}"
                )
            else:
                logger.warning(
                    f"[bold yellow]Generator commit failed[/] round={round_num} "
                    f"sprint={sprint_num}: {commit_result.error}"
                )
                build_log_line = (
                    f"round {round_num:02d}/sprint_{sprint_num} ({mode}): "
                    f"git commit FAILED — {commit_result.error}"
                )
            existing_build_log = file_comm.read_build_log() or ""
            file_comm.write_build_log(
                (existing_build_log.rstrip() + "\n" + build_log_line + "\n").lstrip()
            )
            _save_checkpoint(
                file_comm,
                f"build_r{round_num}",
                round_num,
                user_prompt,
                cost_tracker,
                phase_metrics=phase_metrics,
                current_sprint=sprint_num,
                generator_mode=mode,
                last_verdict="awaiting_review",
                design_metadata=design_metadata,
            )

            if cost_tracker.is_over_budget():
                logger.warning("[bold red]Budget exceeded after build. Stopping.[/]")
                break

        # Phase 3: Evaluate
        if resume_from and resume_from == f"evaluate_r{round_num}":
            logger.info(f"[bold yellow]EVALUATE phase[/] — [dim]skipped (checkpoint)[/]")
            last_verdict = (existing_state or {}).get("last_verdict")
            if last_verdict == "accepted_review":
                logger.info(
                    f"[bold green]✓ Round {round_num} evaluation was already accepted.[/] "
                    "Continuing with the next sprint."
                )
            elif last_verdict == "completed":
                logger.info(
                    f"[bold green]✓ Round {round_num} already completed the final sprint.[/]"
                )
                _print_summary(cost_tracker, time.time() - start, round_num, True)
                return
            else:
                logger.info(
                    f"[bold red]✗ Round {round_num} previously failed review.[/] "
                    "Continuing with repair on the same sprint."
                )
            resume_from = None  # Clear so subsequent rounds run normally
        else:
            logger.info("[bold yellow]EVALUATE phase")
            sprint_num = _get_current_target_sprint(file_comm)
            sprint_context = _get_sprint_context(file_comm, sprint_num)
            evaluate_started_at = time.perf_counter()
            app_stack = await start_app_stack(workdir, harness_dir, config, round_num)
            try:
                evaluator_result = await run_evaluator(
                    config, file_comm, workdir,
                    round_num=round_num,
                    app_url=app_stack.frontend_url,
                )
            finally:
                await _close_app_stack_safely(app_stack)
            await _drain_post_success_cancellation(phase=f"evaluator round {round_num}")
            passed, grades, evaluator_stats = evaluator_result
            # The evaluator captured the screenshots itself during its Phase C
            # and wrote `.harness/visual_manifest_round_N.json` before returning.
            # Read it directly; fall back to globbing the PNGs if the evaluator
            # skipped the manifest write but the screenshots are on disk.
            visual_manifest = file_comm.read_visual_manifest(round_num)
            if not visual_manifest:
                matches = sorted(file_comm.dir.glob(f"visual_round_{round_num}_*.png"))
                if matches:
                    visual_manifest = {
                        "round": round_num,
                        "app_url": "",
                        "screenshots": [f".harness/{path.name}" for path in matches],
                        "notes": "",
                    }
            evaluator_stats = _coerce_agent_run_stats(evaluator_stats).with_wall_duration(
                int((time.perf_counter() - evaluate_started_at) * 1000)
            )
            cost_tracker.add(f"evaluator_r{round_num}", evaluator_stats.cost_usd)
            phase_metrics[f"evaluator_r{round_num}"] = evaluator_stats.to_dict()
            visual_score_started_at = time.perf_counter()
            grades, visual_score_stats = await _await_post_success_step(
                apply_dedicated_visual_review(
                    config=config,
                    file_comm=file_comm,
                    workdir=workdir,
                    round_num=round_num,
                    sprint_num=sprint_num,
                    sprint_context=sprint_context,
                    grades=grades,
                    manifest=visual_manifest,
                ),
                phase=f"visual review round {round_num}",
            )
            await _drain_post_success_cancellation(
                phase=f"visual review round {round_num}"
            )
            if visual_score_stats is not None:
                visual_score_stats = _coerce_agent_run_stats(visual_score_stats).with_wall_duration(
                    int((time.perf_counter() - visual_score_started_at) * 1000)
                )
                cost_tracker.add(f"visual_score_r{round_num}", visual_score_stats.cost_usd)
                phase_metrics[f"visual_score_r{round_num}"] = visual_score_stats.to_dict()
            if grades:
                file_comm.write_grades(round_num, grades)
                file_comm.write_feedback(round_num, render_feedback_from_grades(grades))
            recommendation = _resolve_evaluation_recommendation(
                file_comm=file_comm,
                round_num=round_num,
                sprint_num=sprint_num,
                passed=passed,
                grades=grades,
            )
            _apply_post_evaluation_feature_statuses(file_comm, sprint_num, recommendation, grades)
            last_verdict = "completed" if recommendation == "complete" else (
                "accepted_review" if recommendation == "generate_next_sprint" else "failed_review"
            )
            # Compute the new accepted_sprints in memory, checkpoint
            # state with that payload, THEN write the file. If we crash
            # in the gap, resume reconciles the file from state.
            next_accepted_sprints = _compute_accepted_sprints_after_evaluation(
                file_comm,
                round_num=round_num,
                sprint_num=sprint_num,
                recommendation=recommendation,
            )
            _save_checkpoint(
                file_comm,
                f"evaluate_r{round_num}",
                round_num,
                user_prompt,
                cost_tracker,
                phase_metrics=phase_metrics,
                current_sprint=sprint_num,
                generator_mode="repair" if recommendation == "repair" else "generate",
                last_verdict=last_verdict,
                accepted_sprints_payload=next_accepted_sprints,
                design_metadata=design_metadata,
            )
            file_comm.write_accepted_sprints(next_accepted_sprints)
            accepted_sprints = next_accepted_sprints

            if cost_tracker.is_over_budget():
                # Evaluate phase now includes evaluator + manifest collection +
                # visual_score, any of which can spike cost. Without this gate
                # a single round could run far past max_budget_usd before the
                # next round's build-phase check noticed.
                logger.warning(
                    "[bold red]Budget exceeded after evaluate. Stopping.[/]"
                )
                break

            if recommendation == "complete":
                logger.info(f"[bold green]✓ Final sprint accepted in round {round_num}![/]")
                _print_summary(cost_tracker, time.time() - start, round_num, True)
                return

            if recommendation == "generate_next_sprint":
                next_sprint = accepted_sprints["current_target"]
                logger.info(
                    f"[bold green]✓ Sprint {sprint_num} accepted.[/] "
                    f"Advancing to sprint {next_sprint}."
                )
                continue

            logger.info(
                f"[bold red]✗ Sprint {sprint_num} requires repair.[/] "
                "The next round will continue on the same sprint."
            )

        # After handling resume skip, clear resume_from so next round runs normally
        if resume_from and resume_from == f"build_r{round_num}":
            resume_from = None

    _print_summary(cost_tracker, time.time() - start, config.max_rounds, False)


def _print_summary(cost: CostTracker, elapsed: float, rounds: int, success: bool) -> None:
    status = "[bold green]SUCCESS[/]" if success else "[bold red]INCOMPLETE[/]"
    logger.info(f"\n[bold]{'═' * 40}[/]")
    logger.info(f"[bold]Harness {status}[/]")
    logger.info(f"Duration: {elapsed / 60:.1f} min")
    logger.info(f"Rounds: {rounds}")
    logger.info(cost.summary())


def _reset_frontend_dir(workdir: Path) -> None:
    """Wipe ``workdir/frontend/`` before a fresh run.

    Without this, a generator started for a brand new prompt would
    inherit the previous prompt's frontend and treat it as a baseline
    to "repair". ``--keep-frontend`` opts out.
    """
    frontend_dir = workdir / "frontend"
    if not frontend_dir.exists():
        return
    if not frontend_dir.is_dir():
        return
    shutil.rmtree(frontend_dir)
    logger.info("[bold]Frontend cleared[/] — pass --keep-frontend to preserve it.")
