from __future__ import annotations
import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from src.agents.visual_capture import run_visual_capture
from src.agents.visual_review import apply_dedicated_visual_review, render_feedback_from_grades
from src.agents.sdk_runner import AgentRunStats
from src.agents.evaluator import run_evaluator
from src.agents.generator import run_generator
from src.agents.planner import run_planner
from src.config import HarnessConfig
from src.orchestration.cost_tracker import CostTracker
from src.orchestration.file_comm import FileComm
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
) -> None:
    accepted_sprints = file_comm.read_accepted_sprints() or {}
    file_comm.write_state({
        "last_completed_phase": phase,
        "round_num": round_num,
        "prompt": prompt,
        "costs": cost_tracker.breakdown.copy(),
        "phase_metrics": phase_metrics or {},
        "current_sprint": current_sprint,
        "generator_mode": generator_mode,
        "accepted_sprints": accepted_sprints.get("accepted", []),
        "last_verdict": last_verdict,
        "timestamp": datetime.now().isoformat(),
    })
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


def _update_accepted_sprints_after_evaluation(
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

    next_target = accepted_sprints.get("current_target", 1)
    if recommendation == "generate_next_sprint":
        next_target = sprint_num + 1
    elif recommendation == "complete":
        next_target = sprint_num + 1
    else:
        next_target = sprint_num

    updated = {
        "accepted": accepted,
        "current_target": next_target,
        "last_evaluated_round": round_num,
    }
    file_comm.write_accepted_sprints(updated)
    return updated


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
    accepted = existing_state.get("accepted_sprints")
    if accepted is None or file_comm.read_accepted_sprints() is not None:
        return

    current_sprint = int(existing_state.get("current_sprint") or 1)
    file_comm.write_accepted_sprints(
        {
            "accepted": accepted,
            "current_target": current_sprint,
            "last_evaluated_round": existing_state.get("round_num", 0),
        }
    )


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


async def run_harness(
    user_prompt: str,
    workdir: Path,
    config: HarnessConfig,
    plan_only: bool = False,
    resume: bool = False,
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
        logger.info(f"[bold]Harness started[/] — prompt: {user_prompt[:80]}...")

    logger.info(f"Workdir: {workdir}")

    # Phase 1: Plan
    if _resume_phase_kind(resume_from) not in ("plan", "build", "evaluate"):
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
                evaluator_result, visual_capture_result = await asyncio.gather(
                    run_evaluator(
                        config, file_comm, workdir,
                        round_num=round_num,
                        app_url=app_stack.frontend_url,
                    ),
                    run_visual_capture(
                        config,
                        file_comm,
                        workdir,
                        round_num=round_num,
                        app_url=app_stack.frontend_url,
                    ),
                )
            finally:
                await app_stack.close()
            passed, grades, evaluator_stats = evaluator_result
            visual_manifest, visual_capture_stats = visual_capture_result
            evaluator_stats = _coerce_agent_run_stats(evaluator_stats).with_wall_duration(
                int((time.perf_counter() - evaluate_started_at) * 1000)
            )
            visual_capture_stats = _coerce_agent_run_stats(visual_capture_stats)
            cost_tracker.add(f"evaluator_r{round_num}", evaluator_stats.cost_usd)
            cost_tracker.add(f"visual_capture_r{round_num}", visual_capture_stats.cost_usd)
            phase_metrics[f"evaluator_r{round_num}"] = evaluator_stats.to_dict()
            phase_metrics[f"visual_capture_r{round_num}"] = visual_capture_stats.to_dict()
            visual_score_started_at = time.perf_counter()
            grades, visual_score_stats = await apply_dedicated_visual_review(
                config=config,
                file_comm=file_comm,
                workdir=workdir,
                round_num=round_num,
                sprint_num=sprint_num,
                sprint_context=sprint_context,
                grades=grades,
                manifest=visual_manifest,
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
            accepted_sprints = _update_accepted_sprints_after_evaluation(
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
            )

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
