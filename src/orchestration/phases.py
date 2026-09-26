"""Harness phase implementations, including target-blind Edit validation.

每个阶段函数负责本阶段的检查点写入、成本累计与 phase_metrics 记录。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.agents.evaluator import (
    _determine_passed,
    build_deterministic_failure_grades,
    build_lightweight_browser_grades,
    run_evaluator,
)
from src.agents.lightweight_edit_judge import (
    build_lightweight_grades,
    capture_runtime_snapshot,
    judge_edit,
    _safe_diff,
)
from src.agents.design_stage import run_design_stage
from src.agents.generator import run_generator
from src.agents.edit_planner import run_atomic_edit_planner
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
from src.orchestration.edit_dom_guard import (
    capture_baseline,
    capture_sprint_source_baseline,
    evaluate_guard,
    is_forward_edit,
    repair_baseline_name,
    snapshot_semantic_dom,
    sprint_baseline_name,
)
from src.orchestration.browser_evidence import (
    _same_origin_route_url,
    collect_browser_evidence,
)
from src.orchestration.accepted_tapes import (
    AcceptedTapeError,
    accepted_replay_checks,
    append_accepted_tape,
    select_accepted_replay_checks,
)
from src.orchestration.edit_card import (
    materialize_edit_card,
    read_edit_card,
    visual_evidence_required,
)
from src.orchestration.edit_task_contract import read_edit_task_contract
from src.orchestration.hidden_oracle_checks import read_hidden_oracle_checks
from src.orchestration.edit_context import ensure_edit_context
from src.orchestration.integration_contract import build_integration_contract
from src.orchestration.atomic_edit_plan import (
    normalize_atomic_edit_plan_payload,
    read_atomic_edit_plan,
)
from src.orchestration.repair_packet import write_repair_packet
from src.orchestration.skill_feedback import write_skill_feedback
from src.orchestration.task_inputs import load_task_input_manifest
from src.orchestration.preimplementation_validation import (
    freeze_preimplementation_validation,
    frozen_checks_for_sprint,
    frozen_hidden_oracle_checks,
    verify_preimplementation_validation,
)
from src.orchestration.edit_risk_tests import (
    materialize_edit_risk_tests, collect_source_risk_baseline, distinguish_preexisting_risks,
)
from src.orchestration.ui_action_contracts import TYPED_ASSERTION_ACTIONS
from src.orchestration.minimality_runtime import (
    certify_round_minimality,
    ensure_minimality_policy,
    record_round_build_destination,
    record_round_build_source,
)
from src.orchestration.minimal_path_guidance import (
    discover_page_routes,
    ensure_minimal_path_plan,
    post_edit_scope_sanity,
)
from src.orchestration.runtime import start_app_stack
from src.orchestration.sprint_state import SprintState
from src.prompts.grading import criterion_threshold, evaluation_is_inconclusive
from src.utils.logger import get_logger
from src.utils.sdk_session import safe_sdk_session


def _current_atomic_repair_context_plan(
    workdir: Path, harness_dir: Path, plan: dict[str, Any], round_num: int
) -> dict[str, Any]:
    """Limit Repair source exposure to files changed by the current atomic Edit."""
    if round_num <= 1:
        return plan
    build_map_path = harness_dir / "round_build_map.json"
    frontend = workdir / "frontend"
    if not build_map_path.is_file() or not (frontend / ".git").is_dir():
        return plan
    try:
        build_map = json.loads(build_map_path.read_text(encoding="utf-8"))
        entries = [
            value for key, value in sorted(build_map.items(), key=lambda item: int(item[0]))
            if int(key) < round_num and isinstance(value, dict)
        ]
        baseline = str(entries[0].get("source_commit") or "") if entries else ""
        names = subprocess.run(
            ["git", "diff", "--name-only", baseline, "HEAD", "--"],
            cwd=frontend, check=True, text=True, capture_output=True,
        ).stdout.splitlines()
        changed = {f"frontend/{name}" for name in names if name.strip()}
    except (OSError, ValueError, subprocess.CalledProcessError):
        return plan
    if not changed:
        return plan
    filtered = json.loads(json.dumps(plan))
    cone = filtered.get("source_change_cone") or {}
    for key in ("initial_paths", "local_paths", "dependency_paths"):
        cone[key] = [path for path in cone.get(key) or [] if str(path) in changed]
    cone["hotspots"] = [
        item for item in cone.get("hotspots") or []
        if isinstance(item, dict) and str(item.get("path")) in changed
    ]
    return filtered if (cone.get("initial_paths") or cone.get("local_paths")) else plan

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


def _task_has_image_input(workdir: Path) -> bool:
    manifest = load_task_input_manifest(workdir)
    return any(item.get("kind") == "image" for item in manifest.get("inputs", []))


def _counterfactual_minimality_is_sound(
    grades: dict[str, Any], *, is_edit: bool
) -> bool:
    """A cheap deletion oracle is sound only when typed evidence is complete.

    If the full evaluator was needed, the typed browser contract was already
    judged insufficient for semantic admission.  Reusing that incomplete
    contract to delete allegedly redundant code can remove unasserted required
    behavior (for example the filtering itself while preserving only its hash).
    """
    if not is_edit:
        return True
    route = grades.get("evidence_route")
    return (
        isinstance(route, dict)
        and route.get("decision") == "deterministic_typed_pass"
    )


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
    edit_freeze: dict[str, Any] | None = None
    async with _agent_phase_session(ctx, phase_name="planner"):
        edit_contract = read_edit_task_contract(ctx.workdir)
        if edit_contract is not None:
            raw_stats = await run_atomic_edit_planner(
                ctx.config, ctx.user_prompt, ctx.file_comm, ctx.workdir
            )
        else:
            raw_stats = await run_planner(
                ctx.config, ctx.user_prompt, ctx.file_comm, ctx.workdir
            )
        if edit_contract is not None:
            materialize_edit_card(
                harness_dir=ctx.file_comm.dir,
                instruction_delta=ctx.user_prompt,
                edit_contract=edit_contract,
                sprint_plan=ctx.file_comm.read_sprint_plan() or {},
                verification_plan=ctx.file_comm.read_ui_verification_plan() or {},
            )
            logger.info(
                "[bold blue]VALIDATE: deriving source/Edit risk tests before Build"
            )
            materialize_edit_risk_tests(
                workdir=ctx.workdir,
                instruction_delta=ctx.user_prompt,
                applicable_states=ctx.config.edit_webcompass_defect_checks,
            )
            logger.info(
                "[bold blue]VALIDATE: freezing target-blind Edit tests before Build"
            )
            edit_freeze = freeze_preimplementation_validation(
                workdir=ctx.workdir,
                file_comm=ctx.file_comm,
                instruction_delta=ctx.user_prompt,
            )
        _record_phase_stats(ctx, "planner", _coerce_stats(raw_stats), started_at=started)
        _checkpoint_transaction(ctx).record_plan_completed(edit_freeze=edit_freeze)


async def run_design_phase(ctx: HarnessContext) -> dict[str, Any]:
    """执行 design 阶段，并在完成后写入检查点。"""
    logger.info("[bold magenta]PHASE 2: DESIGN")
    result = await run_design_stage(ctx.config, ctx.file_comm, ctx.workdir)
    _checkpoint_transaction(ctx).record_design_completed(result.metadata)
    return result.metadata


# ---- Build 阶段 ----


def _is_incremental_edit_sprint(ctx: HarnessContext, sprint_num: int) -> bool:
    """Whether this build extends an already accepted product checkpoint.

    Explicit Edit runs are scoped from their seed starting at sprint one. A
    Generate run becomes an incremental Edit producer only after every earlier
    sprint has been accepted; a failed first build therefore remains a Repair
    source rather than being mislabeled as an Edit.
    """
    if is_forward_edit(ctx.workdir):
        return True
    return sprint_num > 1 and set(range(1, sprint_num)).issubset(
        set(ctx.sprint_state.accepted)
    )


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
    if previous.get("overall_passed") is False and previous.get("sprint") == sprint_num:
        return "repair"
    if _resume_requests_repair(
        resume_state,
        round_num=round_num,
        sprint_num=sprint_num,
    ):
        return "repair"
    return "generate"


def _visual_style_recheck_allowed(
    grade: dict[str, Any], certificate: dict[str, Any], checks: list[dict[str, Any]]
) -> bool:
    """Allow a zero-mutation recheck only for uncovered target-local style atoms."""
    if (
        grade.get("overall_passed") is not False
        or grade.get("bugs_found")
        or not isinstance(grade.get("ui_checks"), list)
        or not grade["ui_checks"]
        or any(
            not isinstance(item, dict)
            or str(item.get("status", "")).strip().lower() != "pass"
            for item in grade["ui_checks"]
        )
        or (grade.get("edit_guard") or {}).get("passed") is not True
        or certificate.get("status") != "non_minimal"
    ):
        return False
    redundant = set(certificate.get("redundant_change_ids") or [])
    patches = {
        str(item.get("change_id")): str(item.get("path", ""))
        for item in certificate.get("atomic_patches", [])
        if isinstance(item, dict) and item.get("change_id")
    }
    if not redundant or not redundant <= set(patches) or any(
        Path(patches[change_id]).suffix.lower() not in {".css", ".scss"}
        for change_id in redundant
    ):
        return False
    return any(
        isinstance(check, dict)
        and str(check.get("category", "")).strip().lower()
        in {"appearance", "responsive", "style", "visual"}
        for check in checks or []
    )


def _dedicated_visual_recheck_allowed(grade: dict[str, Any]) -> bool:
    """Retry vision without source mutation when every non-visual gate passed."""
    phase = grade.get("phase_results") or {}
    return bool(
        grade.get("overall_passed") is False
        and phase.get("render_gate") == "pass"
        and phase.get("ui_functionality") == "pass"
        and phase.get("source_inspection") == "pass"
        and phase.get("appearance") == "skipped"
        and not grade.get("bugs_found")
        and not grade.get("regressions_found")
        and not grade.get("repair_instructions")
        and (grade.get("edit_guard") or {}).get("passed") is True
        and isinstance(grade.get("ui_checks"), list)
        and grade["ui_checks"]
        and all(
            isinstance(item, dict)
            and str(item.get("status", "")).strip().lower() == "pass"
            for item in grade["ui_checks"]
        )
    )


def _contract_functionality_recheck_allowed(
    file_comm: FileComm, previous_round: int, grade: dict[str, Any]
) -> bool:
    """Retry evidence only when executable behavior was the sole scoring conflict."""
    phase = grade.get("phase_results") or {}
    criteria = grade.get("criteria") or {}
    functionality = criteria.get("functionality") if isinstance(criteria, dict) else None
    score = functionality.get("score") if isinstance(functionality, dict) else None
    ui_checks = grade.get("ui_checks")
    if (
        grade.get("overall_passed") is not False
        or not isinstance(score, (int, float))
        or isinstance(score, bool)
        or score >= criterion_threshold("functionality")
        or phase.get("render_gate") != "pass"
        or phase.get("ui_functionality") != "pass"
        or phase.get("source_inspection") != "pass"
        or phase.get("appearance") not in {"pass", "skipped"}
        or grade.get("bugs_found")
        or grade.get("regressions_found")
        or grade.get("repair_instructions")
        or (grade.get("edit_guard") or {}).get("passed") is not True
        or not isinstance(ui_checks, list)
        or not ui_checks
        or any(
            not isinstance(item, dict)
            or not item.get("check_id")
            or str(item.get("status", "")).strip().lower() != "pass"
            for item in ui_checks
        )
    ):
        return False
    evidence_path = file_comm.dir / f"browser_evidence_round_{previous_round}.json"
    try:
        records = json.loads(evidence_path.read_text(encoding="utf-8")).get("checks", [])
    except (OSError, ValueError, TypeError):
        return False
    observed = {
        str(item.get("check_id")): str(item.get("status"))
        for item in records
        if isinstance(item, dict) and item.get("check_id")
    }
    return all(observed.get(str(item["check_id"])) == "ok" for item in ui_checks)


def _conditional_fragment_recheck_allowed(
    file_comm: FileComm,
    previous_round: int,
    grade: dict[str, Any],
    checks: list[dict[str, Any]],
) -> bool:
    """Re-evaluate a dynamic target that an old static scope misclassified."""
    guard = grade.get("edit_guard") or {}
    violations = guard.get("violations") if isinstance(guard, dict) else None
    ui_checks = grade.get("ui_checks")
    if (
        grade.get("overall_passed") is not False
        or not isinstance(violations, list)
        or not violations
        or any(
            not isinstance(item, dict)
            or item.get("kind") != "expected_addition_missing"
            for item in violations
        )
        or not isinstance(ui_checks, list)
        or not ui_checks
        or any(
            not isinstance(item, dict)
            or str(item.get("status") or "").lower() != "pass"
            for item in ui_checks
        )
    ):
        return False
    planned_selectors = {
        str(action.get("selector") or "")
        for check in checks or []
        if isinstance(check, dict)
        for action in check.get("actions") or []
        if isinstance(action, dict)
    }
    missing_selectors = {
        str(item.get("fragment") or "").split("::", 1)[-1]
        for item in violations
    }
    evidence_path = file_comm.dir / f"browser_evidence_round_{previous_round}.json"
    try:
        records = json.loads(evidence_path.read_text(encoding="utf-8")).get("checks", [])
    except (OSError, ValueError, TypeError):
        return False
    return bool(missing_selectors) and missing_selectors <= planned_selectors and all(
        isinstance(item, dict) and item.get("status") == "ok" for item in records
    )


async def run_build_phase(
    ctx: HarnessContext,
    round_num: int,
    *,
    resume_state: dict[str, Any] | None = None,
) -> None:
    """执行当前目标 sprint 的 generator，并写入检查点。"""
    logger.info("[bold green]BUILD phase")
    sprint_num = ctx.sprint_state.current_target
    edit_contract = read_edit_task_contract(ctx.workdir)
    if edit_contract is not None:
        verify_preimplementation_validation(
            workdir=ctx.workdir,
            file_comm=ctx.file_comm,
            instruction_delta=ctx.user_prompt,
        )
    mode = _select_generator_mode(ctx, round_num, sprint_num, resume_state)
    frontend_dir = ctx.workdir / "frontend"
    semantic_routes = discover_page_routes(ctx.workdir) or None
    baseline_path = ctx.file_comm.dir / "edit_dom_baseline.json"
    sprint_baseline_path = ctx.file_comm.dir / sprint_baseline_name(sprint_num)
    incremental_edit = _is_incremental_edit_sprint(ctx, sprint_num)
    needs_global_baseline = is_forward_edit(ctx.workdir) and not baseline_path.exists()
    needs_sprint_baseline = incremental_edit and not sprint_baseline_path.exists()
    if needs_global_baseline or needs_sprint_baseline:
        # Capture the accepted source before the editor can touch it.  The
        # global seed frame supports provenance; the per-sprint frame prevents
        # earlier accepted edits from looking like new collateral damage. A
        # Generate run starts using this same frame from sprint two onward.
        app_stack = await start_app_stack(ctx.workdir, ctx.file_comm.dir, ctx.config, round_num)
        try:
            if (
                ctx.config.lightweight_edit_production
                and edit_contract is not None
                and not (ctx.file_comm.dir / "before.png").exists()
            ):
                baseline_runtime = await capture_runtime_snapshot(
                    app_url=app_stack.frontend_url,
                    screenshot_path=ctx.file_comm.dir / "before.png",
                    headless=ctx.config.playwright_headless,
                    retries=max(0, ctx.config.lightweight_edit_judge_retries),
                )
                if baseline_runtime.get("status") == "infrastructure_error":
                    raise RuntimeError(
                        "Unable to capture the original Edit screenshot: "
                        + str(baseline_runtime.get("error") or "unknown browser error")
                    )
            if needs_global_baseline:
                snapshot = await capture_baseline(
                    workdir=ctx.workdir, file_comm=ctx.file_comm, config=ctx.config,
                    app_url=app_stack.frontend_url,
                    routes=semantic_routes,
                )
                if needs_sprint_baseline:
                    sprint_baseline_path.write_text(
                        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            elif needs_sprint_baseline:
                await capture_sprint_source_baseline(
                    file_comm=ctx.file_comm, config=ctx.config,
                    app_url=app_stack.frontend_url, sprint_num=sprint_num,
                    routes=semantic_routes,
                )
        finally:
            await app_stack.close()
    elif (
        mode == "repair"
        and not sprint_baseline_path.exists()
        and (frontend_dir / ".git").exists()
    ):
        repair_baseline_path = ctx.file_comm.dir / repair_baseline_name(round_num)
        previous = ctx.file_comm.read_grades(round_num - 1) or {}
        render_failed = (previous.get("phase_results") or {}).get("render_gate") == "fail"
        if not repair_baseline_path.exists() and not render_failed:
            # For a repair that did not originate from an accepted Edit seed,
            # freeze the actual failed source. The repair may change its
            # declared surface, while the rest becomes a semantic frame.
            app_stack = await start_app_stack(
                ctx.workdir, ctx.file_comm.dir, ctx.config, round_num
            )
            try:
                snapshot = await snapshot_semantic_dom(
                    app_stack.frontend_url,
                    headless=ctx.config.playwright_headless,
                    routes=semantic_routes,
                )
                if snapshot.get("stable") is not True:
                    raise RuntimeError(
                        "Repair source DOM is unstable across two samples; "
                        "refusing to derive a Repair contract."
                    )
                repair_baseline_path.write_text(
                    json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            finally:
                await app_stack.close()
    guide_recommended_scope = (
        ctx.config.minimal_path_guidance_enabled
        and
        (incremental_edit or mode == "repair")
        and frontend_dir.is_dir()
    )
    if guide_recommended_scope:
        minimal_path_plan = ensure_minimal_path_plan(
            workdir=ctx.workdir,
            harness_dir=ctx.file_comm.dir,
            round_num=round_num,
            sprint_num=sprint_num,
            mode=mode,
            max_patch_lines=ctx.config.minimal_path_max_patch_lines,
            max_touched_files=ctx.config.minimal_path_max_touched_files,
            eager_dependency_context=ctx.config.edit_frozen_compound_mode,
            scope_mode=getattr(ctx.config, "minimal_path_mode", "recommended"),
        )
        atomic_plan = read_atomic_edit_plan(ctx.file_comm.dir) or {}
        atomic_plan = normalize_atomic_edit_plan_payload(
            atomic_plan, instruction_delta=ctx.user_prompt
        )
        context_plan = (
            _current_atomic_repair_context_plan(
                ctx.workdir, ctx.file_comm.dir, minimal_path_plan, round_num
            )
            if mode == "repair" and ctx.config.edit_frozen_compound_mode
            else minimal_path_plan
        )
        ensure_edit_context(
            workdir=ctx.workdir,
            harness_dir=ctx.file_comm.dir,
            plan=context_plan,
            round_num=round_num,
            source_anchors=list(atomic_plan.get("source_anchors") or []),
            include_dependency_paths=ctx.config.edit_frozen_compound_mode,
            full_repair_context=False,
            **({"max_total_chars": 120000, "max_file_chars": 80000}
               if mode == "repair"
               else {"max_total_chars": 250000, "max_file_chars": 250000}
               if (ctx.file_comm.read_state() or {}).get("supplied_atomic_plan")
               else {}),
        )
        build_integration_contract(
            workdir=ctx.workdir,
            harness_dir=ctx.file_comm.dir,
            round_num=round_num,
            plan=minimal_path_plan,
        )
    track_build = (
        (incremental_edit or mode == "repair")
        and (frontend_dir / ".git").exists()
    )
    if track_build:
        if ctx.config.minimality_guard_enabled:
            ensure_minimality_policy(ctx.file_comm.dir, ctx.config)
        record_round_build_source(
            ctx.file_comm.dir, frontend_dir, round_num=round_num,
            sprint_num=sprint_num, mode=mode,
        )
    previous_grade = ctx.file_comm.read_grades(round_num - 1) or {}
    previous_certificate = (
        previous_grade.get("minimality_certificate", {}).get("edit", {})
        if isinstance(previous_grade.get("minimality_certificate"), dict)
        else {}
    )
    certificate_artifact = previous_certificate.get("artifact")
    certificate_payload = (
        json.loads((ctx.workdir / str(certificate_artifact)).read_text(encoding="utf-8"))
        if isinstance(certificate_artifact, str)
        and certificate_artifact.startswith(".harness/")
        and (ctx.workdir / certificate_artifact).is_file()
        else {}
    )
    evidence_only_recheck = mode == "repair" and (
        _visual_style_recheck_allowed(
            previous_grade,
            certificate_payload,
            ctx.sprint_state.ui_checks_for_sprint(sprint_num),
        )
        or _dedicated_visual_recheck_allowed(previous_grade)
        or _contract_functionality_recheck_allowed(
            ctx.file_comm, round_num - 1, previous_grade
        )
        or _conditional_fragment_recheck_allowed(
            ctx.file_comm,
            round_num - 1,
            previous_grade,
            ctx.sprint_state.ui_checks_for_sprint(sprint_num),
        )
    )
    async with _agent_phase_session(ctx, phase_name=f"generator round {round_num}"):
        ctx.sprint_state.mark_sprint_in_progress(sprint_num)
        started = time.perf_counter()
        if evidence_only_recheck:
            logger.info(
                "[bold green]Generator[/] skipped for evidence-only visual/style recheck; "
                "the committed frontend remains byte-identical."
            )
            raw_stats = AgentRunStats(
                cost_usd=0.0,
                duration_ms=0,
                duration_api_ms=0,
                token_usage={},
                usage={"recovery": "evidence_only_visual_style_recheck"},
                model_usage={},
            )
        else:
            raw_stats = await run_generator(
                ctx.config, ctx.file_comm, ctx.workdir,
                round_num=round_num, sprint_num=sprint_num, mode=mode,
            )
        if track_build:
            record_round_build_destination(
                ctx.file_comm.dir, frontend_dir, round_num=round_num
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


async def _run_lightweight_edit_evaluation(ctx: HarnessContext, round_num: int) -> Verdict:
    """Run the cheap Edit gate: runtime snapshot, one screenshot, one judge."""
    sprint_num = ctx.sprint_state.current_target
    started = time.perf_counter()
    app_stack = None
    after = ctx.file_comm.dir / f"after_round_{round_num}.png"
    before = ctx.file_comm.dir / "before.png"
    try:
        app_stack = await start_app_stack(ctx.workdir, ctx.file_comm.dir, ctx.config, round_num)
        runtime = await capture_runtime_snapshot(
            app_url=app_stack.frontend_url,
            screenshot_path=after,
            headless=ctx.config.playwright_headless,
            retries=max(0, ctx.config.lightweight_edit_judge_retries),
        )
        if runtime.get("status") == "infrastructure_error":
            raise EvaluationInfrastructureError(str(runtime.get("error") or "browser snapshot failed"))
        (ctx.file_comm.dir / f"browser_evidence_round_{round_num}.json").write_text(
            json.dumps({
                "policy_version": "lightweight-edit-runtime-v1",
                "checks": [{
                    "check_id": "RUNTIME-01",
                    "status": "ok" if runtime.get("status") == "pass" else "action_failed",
                    "steps": [],
                    "notes": "Runtime snapshot only; no planner-authored browser checker.",
                }],
                "runtime": runtime,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        diff = _safe_diff(ctx.workdir)
        judge_error: Exception | None = None
        for judge_attempt in range(max(0, ctx.config.lightweight_edit_judge_retries) + 1):
            try:
                judgement, judge_stats = await judge_edit(
                    config=ctx.config,
                    instruction=ctx.user_prompt,
                    diff=diff,
                    runtime=runtime,
                    before=before if before.is_file() else None,
                    after=after,
                    round_num=round_num,
                )
                break
            except Exception as exc:
                judge_error = exc
                if judge_attempt >= max(0, ctx.config.lightweight_edit_judge_retries):
                    raise EvaluationInfrastructureError(
                        f"Lightweight Judge infrastructure failed after {judge_attempt + 1} attempts: "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
        else:  # pragma: no cover - loop always breaks or raises
            raise EvaluationInfrastructureError(str(judge_error or "Lightweight Judge failed"))
        grades = build_lightweight_grades(
            round_num=round_num,
            sprint_num=sprint_num,
            runtime=runtime,
            judgement=judgement,
            before=before if before.is_file() else None,
            after=after,
            diff=diff,
        )
        if ctx.config.minimal_path_guidance_enabled:
            grades["scope_sanity"] = post_edit_scope_sanity(
                workdir=ctx.workdir,
                harness_dir=ctx.file_comm.dir,
                round_num=round_num,
            )
        if not grades["overall_passed"]:
            # The normal generator Repair prompt consumes this bounded handoff,
            # especially when minimal-path mode is enabled.  Without it the
            # Judge findings would be persisted but not forwarded to the model.
            packet = write_repair_packet(
                workdir=ctx.workdir,
                round_num=round_num,
                sprint_num=sprint_num,
                grades=grades,
            )
            grades["repair_packet"] = {
                "status": packet.get("status"),
                "artifact": f".harness/repair_packet_round_{round_num}.json",
                "failed_check_ids": [
                    str(item.get("check_id", "unknown"))
                    for item in packet.get("failed_checks") or []
                ],
            }
        ctx.file_comm.write_grades(round_num, grades)
        ctx.file_comm.write_feedback(round_num, render_feedback_from_grades(grades))
        ctx.file_comm.write_visual_manifest(round_num, {
            "round": round_num,
            "app_url": app_stack.frontend_url,
            "screenshots": [f".harness/{after.name}"] + ([".harness/before.png"] if before.is_file() else []),
            "notes": "Lightweight Edit runtime snapshot; no browser checker was run.",
        })
        ctx.phase_metrics[f"evaluator_r{round_num}"] = judge_stats
        ctx.cost_tracker.add(f"evaluator_r{round_num}", float(judge_stats.get("cost_usd") or 0.0))
        passed = bool(grades["overall_passed"])
        recommendation = "complete" if passed else str(grades.get("mode_recommendation") or "repair")
        ctx.sprint_state.mark_sprint_outcome(sprint_num, recommendation=recommendation, grades=grades)
        _checkpoint_transaction(ctx).record_evaluate_completed(
            sprint_state=ctx.sprint_state,
            round_num=round_num,
            sprint_num=sprint_num,
            recommendation=recommendation,
        )
        logger.info("[bold yellow]Lightweight Edit Judge[/] round %s: %s", round_num, "PASS" if passed else recommendation.upper())
        return Verdict.completed if passed else Verdict.failed_review
    finally:
        if app_stack is not None:
            await app_stack.close()


def _reverse_validation_failures(validation: dict[str, Any]) -> list[str]:
    failures = [str(item) for item in validation.get("reasons") or []]
    if validation.get("error"):
        failures.append(str(validation["error"]))
    for page in validation.get("pages") or []:
        path = str(page.get("path") or "unknown page")
        if page.get("navigation_error"):
            failures.append(f"{path}: navigation error: {page['navigation_error']}")
        if isinstance(page.get("http_status"), int) and page["http_status"] >= 400:
            failures.append(f"{path}: HTTP {page['http_status']}")
        if page.get("blank"):
            failures.append(f"{path}: rendered page is blank")
        if page.get("severe_overflow"):
            failures.append(f"{path}: severe horizontal overflow")
        failures.extend(f"{path}: page error: {item}" for item in page.get("page_errors") or [])
        failures.extend(f"{path}: required asset failed: {item}" for item in page.get("essential_failures") or [])
    return list(dict.fromkeys(item for item in failures if item.strip()))


async def _run_reverse_validate_evaluation(ctx: HarnessContext, round_num: int) -> Verdict:
    """Run only the existing reverse/validate browser policy for this Edit."""
    sprint_num = ctx.sprint_state.current_target
    started = time.perf_counter()
    root = Path(ctx.config.reverse_validate_root).resolve()
    validator = root / "validate.js"
    builder = root / "build_vite_project.mjs"
    frontend = ctx.workdir / "frontend"
    if not validator.is_file() or not builder.is_file():
        raise EvaluationInfrastructureError(f"invalid reverse/validate root: {root}")

    marker = frontend / ".generation.json"
    marker_existed = marker.exists()
    if not marker_existed:
        marker.write_text(json.dumps({"edit_mother": True, "query": ctx.user_prompt}, ensure_ascii=False))
    output = ctx.file_comm.dir / f"reverse_validation_round_{round_num}.jsonl"
    output.unlink(missing_ok=True)
    command = [
        ctx.config.reverse_validate_node,
        str(validator),
        "--projects-dir", str(ctx.workdir),
        "--out", str(output),
        "--workers", "1",
        "--limit", "0",
        "--timeout", "20000",
        "--project-id", "frontend",
        "--framework-builder", str(builder),
        "--build-root", str(ctx.file_comm.dir / "reverse_builds"),
        "--build-timeout", "90000",
    ]
    env = os.environ.copy()
    if ctx.config.reverse_validate_playwright_module:
        env["PLAYWRIGHT_MODULE"] = ctx.config.reverse_validate_playwright_module
    if ctx.config.reverse_validate_chromium:
        env["CHROMIUM_EXECUTABLE"] = ctx.config.reverse_validate_chromium
    if ctx.config.reverse_validate_extra_node_modules:
        env["WEBCODING_EXTRA_NODE_MODULES"] = ctx.config.reverse_validate_extra_node_modules
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=ctx.config.reverse_validate_timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise EvaluationInfrastructureError("reverse/validate timed out") from exc
        if process.returncode:
            detail = (stderr or stdout).decode(errors="replace")[-3000:]
            raise EvaluationInfrastructureError(
                f"reverse/validate exited {process.returncode}: {detail}"
            )
    finally:
        if not marker_existed:
            marker.unlink(missing_ok=True)

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise EvaluationInfrastructureError("reverse/validate returned no result")
    validation = rows[-1]
    validation_error = str(validation.get("error") or "")
    if (
        "framework_build_failed" in (validation.get("reasons") or [])
        and (
            "Cannot resolve validator runtime dependency" in validation_error
            or "reverse/validate/build_vite_project.mjs" in validation_error
            and "ERR_MODULE_NOT_FOUND" in validation_error
        )
    ):
        raise EvaluationInfrastructureError(validation_error)
    passed = validation.get("status") == "accept"
    failures = _reverse_validation_failures(validation)
    sprint_context = ctx.sprint_state.sprint_context(sprint_num)
    feature_id = str(next(iter(sprint_context.get("feature_ids") or []), "reverse-runtime"))
    checks = [{
        "check_id": f"REVERSE-{index:02d}",
        "feature_id": feature_id,
        "critical": True,
        "task": ctx.user_prompt,
        "expected_result": "Every project page builds and renders without fatal runtime failures.",
        "status": "action_failed",
        "notes": failure,
        "steps": [],
    } for index, failure in enumerate(failures, 1)]
    if passed:
        checks = [{
            "check_id": "REVERSE-01",
            "feature_id": feature_id,
            "critical": True,
            "task": ctx.user_prompt,
            "expected_result": "Every project page builds and renders without fatal runtime failures.",
            "status": "ok",
            "notes": "reverse/validate accepted all rendered pages",
            "steps": [],
        }]
    evidence = {
        "policy_version": "reverse-validate-v1",
        "validator": str(validator),
        "checks": checks,
        "validation": validation,
    }
    (ctx.file_comm.dir / f"browser_evidence_round_{round_num}.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    grades = {
        "round": round_num,
        "sprint": sprint_num,
        "sprint_passed": passed,
        "regression_passed": True,
        "overall_passed": passed,
        "mode_recommendation": "complete" if passed else "repair",
        "phase_results": {
            "render_gate": "pass" if passed else "fail",
            "ui_functionality": "skipped",
            "appearance": "skipped",
            "source_inspection": "skipped",
        },
        "criteria": {},
        "target_exit_criteria_results": [],
        "ui_checks": [{
            "feature_id": item["feature_id"],
            "check_id": item["check_id"],
            "critical": item["critical"],
            "task": item["task"],
            "expected_result": item["expected_result"],
            "status": "pass" if item["status"] == "ok" else "fail",
            "notes": item["notes"],
        } for item in checks],
        "bugs_found": failures,
        "regressions_found": [],
        "missing_features": [],
        "repair_instructions": [
            "Repair only the concrete reverse/validate runtime, build, asset, blank-page, or overflow failures above."
        ] if failures else [],
        "repair_task_descriptions": [],
        "evidence_route": {
            "decision": "reverse_validate_only",
            "llm_evaluator_called": False,
            "browser_evidence_ref": f".harness/browser_evidence_round_{round_num}.json",
        },
    }
    if not passed:
        packet = write_repair_packet(
            workdir=ctx.workdir,
            round_num=round_num,
            sprint_num=sprint_num,
            grades=grades,
        )
        grades["repair_packet"] = {
            "status": packet.get("status"),
            "artifact": f".harness/repair_packet_round_{round_num}.json",
        }
    ctx.file_comm.write_grades(round_num, grades)
    ctx.file_comm.write_feedback(round_num, render_feedback_from_grades(grades))
    stats = AgentRunStats(
        cost_usd=0.0,
        duration_ms=0,
        duration_api_ms=0,
        token_usage={},
        usage={"validator": "reverse.validate", "status": validation.get("status")},
        model_usage={},
    )
    _record_phase_stats(ctx, f"evaluator_r{round_num}", stats, started_at=started)
    recommendation = "complete" if passed else "repair"
    ctx.sprint_state.mark_sprint_outcome(sprint_num, recommendation=recommendation, grades=grades)
    _checkpoint_transaction(ctx).record_evaluate_completed(
        sprint_state=ctx.sprint_state,
        round_num=round_num,
        sprint_num=sprint_num,
        recommendation=recommendation,
    )
    logger.info("[bold yellow]reverse/validate[/] round %s: %s", round_num, "PASS" if passed else "REPAIR")
    return Verdict.completed if passed else Verdict.failed_review


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


def _apply_accepted_tape_gate(
    grades: dict[str, Any], evidence: dict[str, Any] | None, *, round_num: int
) -> dict[str, Any]:
    if evidence is None:
        return grades
    failed = [
        str(item.get("check_id", "unknown"))
        for item in evidence.get("checks", [])
        if isinstance(item, dict) and item.get("status") == "action_failed"
    ]
    gated = json.loads(json.dumps(grades))
    gated["accepted_tape_replay"] = {
        "status": "failed" if failed else "ok",
        "failed_check_ids": failed,
        "evidence_ref": f".harness/accepted_tape_replay_round_{round_num}.json",
    }
    if not failed:
        return gated
    finding = "Previously accepted browser behavior regressed: " + ", ".join(failed)
    gated.setdefault("regressions_found", []).append(finding)
    gated.setdefault("repair_instructions", []).append(
        "Restore the failed accepted-tape behavior without widening the current Edit scope."
    )
    gated.setdefault("phase_results", {})["ui_functionality"] = "fail"
    gated["sprint_passed"] = False
    gated["regression_passed"] = False
    gated["overall_passed"] = False
    gated["mode_recommendation"] = "repair"
    return gated


def _checks_are_tape_eligible(checks: list[dict[str, Any]]) -> bool:
    if not checks or any(not isinstance(check, dict) for check in checks):
        return False
    for check in checks:
        actions = check.get("actions")
        if not isinstance(actions, list) or not actions:
            return False
        assertion_count = sum(
            1
            for action in actions
            if isinstance(action, dict)
            and action.get("action") in TYPED_ASSERTION_ACTIONS
        )
        if (
            assertion_count < 1
            or not isinstance(actions[-1], dict)
            or actions[-1].get("action") not in TYPED_ASSERTION_ACTIONS
        ):
            return False
    return True


def _apply_browser_click_evidence_gate(
    workdir: Path, round_num: int, grades: dict[str, Any]
) -> dict[str, Any]:
    """A real browser click failure cannot be overridden by a model's pass verdict.

    Evaluators occasionally use a forced or programmatic click after a normal
    user click is blocked, then report the underlying handler as working.  That
    is not equivalent to the requested interaction being usable.  Preserve the
    trace as the source of truth and route the reproduced defect to repair.
    """
    trace_path = workdir / ".harness" / "traces" / f"evaluator_round_{round_num}.jsonl"
    if not trace_path.is_file():
        return grades

    failures: list[str] = []
    forced_clicks: list[str] = []
    normal_successes: set[str] = set()
    pending_clicks: list[tuple[str, bool]] = []
    for raw_line in trace_path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "assistant":
            tool_calls = (event.get("message") or {}).get("tool_calls") or []
            for tool_call in tool_calls:
                function = tool_call.get("function") or {}
                if function.get("name") != "browser_click":
                    continue
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                selector = str(arguments.get("selector") or "<unknown>")
                pending_clicks.append((selector, bool(arguments.get("force"))))
            continue
        if event.get("event") != "tool" or event.get("name") != "browser_click":
            continue
        selector, forced = pending_clicks.pop(0) if pending_clicks else ("<unknown>", False)
        if event.get("ok") is False:
            detail = " ".join(str(event.get("output", "")).split())
            failures.append(detail[:500] if detail else "click did not complete")
        elif forced:
            forced_clicks.append(selector)
        else:
            normal_successes.add(selector)

    for selector in forced_clicks:
        if selector not in normal_successes:
            failures.append(
                f"forced browser_click for {selector} without a preceding successful normal click"
            )

    if not failures:
        return grades

    gated = json.loads(json.dumps(grades))
    detail = failures[0]
    finding = f"Observed browser_click evidence failed: {detail}"
    bugs = gated.setdefault("bugs_found", [])
    if finding not in bugs:
        bugs.append(finding)
    instructions = gated.setdefault("repair_instructions", [])
    instruction = (
        "Repair or re-evaluate the browser interaction reported by the evaluator trace; "
        "a forced or programmatic click is not evidence that a user can activate it."
    )
    if instruction not in instructions:
        instructions.append(instruction)

    phase_results = gated.setdefault("phase_results", {})
    if isinstance(phase_results, dict):
        phase_results["ui_functionality"] = "fail"
    gated["sprint_passed"] = False
    gated["regression_passed"] = False
    gated["overall_passed"] = False
    gated["mode_recommendation"] = "repair"
    logger.warning(
        "[bold yellow]Evaluator[/] trace recorded invalid browser click evidence; "
        "overriding model pass verdict and scheduling repair."
    )
    return gated


def _reconcile_action_contract_evidence(
    file_comm: FileComm, round_num: int, grades: dict[str, Any]
) -> dict[str, Any]:
    """Make planner-authored Playwright assertions authoritative per UI check.

    The LLM may still inspect a different state or fail to reproduce an action.
    It must not turn a recorded `evaluate: true` into a synthetic repair task.
    This only reconciles checks that have an executable harness contract; all
    uncontracted UI, visual, and source-review findings remain untouched.
    """
    path = file_comm.dir / f"browser_evidence_round_{round_num}.json"
    if not path.is_file():
        return grades
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
        records = evidence.get("checks", [])
    except (OSError, ValueError, TypeError):
        return grades
    if not isinstance(records, list):
        return grades

    status_by_id = {
        str(item.get("check_id")): str(item.get("status"))
        for item in records if isinstance(item, dict) and item.get("check_id")
    }
    if not status_by_id:
        return grades
    reconciled = json.loads(json.dumps(grades))
    ui_checks = reconciled.get("ui_checks")
    if not isinstance(ui_checks, list):
        return reconciled
    changed: list[str] = []
    feature_status: dict[str, bool] = {}
    for check in ui_checks:
        if not isinstance(check, dict):
            continue
        check_id = str(check.get("check_id", ""))
        observed = status_by_id.get(check_id)
        if observed not in {"ok", "action_failed"}:
            continue
        passed = observed == "ok"
        previous = str(check.get("status", "")).lower()
        check["status"] = "pass" if passed else "fail"
        check["notes"] = (
            "Harness action contract " + ("passed" if passed else "failed")
            + "; this recorded browser assertion supersedes conflicting exploratory evaluation."
        )
        if previous != check["status"]:
            changed.append(check_id)
        feature_id = str(check.get("feature_id", ""))
        if feature_id:
            feature_status[feature_id] = feature_status.get(feature_id, True) and passed

    # Typed browser actions are the authoritative functionality oracle when
    # they cover every evaluator UI check and all executions passed.  Preserve
    # the model's score above threshold, but do not let an exploratory score
    # below threshold contradict complete executable evidence.
    grade_check_ids = {
        str(check.get("check_id"))
        for check in ui_checks
        if isinstance(check, dict) and check.get("check_id")
    }
    contracts_complete = bool(grade_check_ids) and all(
        status_by_id.get(check_id) == "ok" for check_id in grade_check_ids
    )
    functionality_calibrated = False
    calibration_route = str(
        (reconciled.get("evidence_route") or {}).get("decision") or ""
    )
    if contracts_complete and calibration_route in {
        "contract_only_semantic_pass",
        "deterministic_typed_pass",
    }:
        criteria = reconciled.get("criteria")
        functionality = criteria.get("functionality") if isinstance(criteria, dict) else None
        if isinstance(functionality, dict):
            score = functionality.get("score")
            threshold = criterion_threshold("functionality")
            if isinstance(score, (int, float)) and not isinstance(score, bool) and score < threshold:
                functionality["score"] = threshold
                functionality["passed"] = True
                prior_notes = str(functionality.get("notes", "")).strip()
                evidence_note = (
                    "All typed browser action contracts passed; functionality was "
                    "calibrated to the deterministic acceptance threshold."
                )
                functionality["notes"] = f"{prior_notes} {evidence_note}".strip()
                functionality_calibrated = True

    if not changed and not functionality_calibrated:
        return reconciled
    for result in reconciled.get("target_exit_criteria_results", []):
        if not isinstance(result, dict):
            continue
        feature_id = str(result.get("feature_id", ""))
        if feature_id not in feature_status:
            continue
        result["passed"] = feature_status[feature_id]
        result["notes"] = "Matched to the harness action-contract result for feature " + feature_id + "."
    reconciled["browser_action_contract_reconciliation"] = {
        "changed_check_ids": changed,
        "functionality_calibrated": functionality_calibrated,
        "evidence_ref": f".harness/browser_evidence_round_{round_num}.json",
    }
    logger.warning(
        "[bold yellow]Evaluator[/] reconciled %s conflicting UI verdict(s) with harness action evidence.",
        len(changed),
    )
    return reconciled


def _action_contract_grade_conflicts(file_comm: FileComm, round_num: int, grades: dict[str, Any]) -> list[str]:
    """Return checks where an LLM grade contradicts a complete browser contract.

    Reconciliation can correct a checkbox, but it cannot safely repair model
    prose such as invented missing features or repair instructions.  Such a
    grade is not a valid natural-repair trajectory and must be re-evaluated.
    """
    path = file_comm.dir / f"browser_evidence_round_{round_num}.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8")).get("checks", [])
    except (OSError, ValueError, TypeError):
        return []
    expected = {
        str(item.get("check_id")): "pass" if item.get("status") == "ok" else "fail"
        for item in records if isinstance(item, dict) and item.get("status") in {"ok", "action_failed"}
    }
    conflicts: list[str] = []
    for check in grades.get("ui_checks", []):
        if not isinstance(check, dict):
            continue
        check_id = str(check.get("check_id", ""))
        if check_id in expected and str(check.get("status", "")).lower() != expected[check_id]:
            conflicts.append(check_id)
    return conflicts


def _non_visual_gates_passed(
    grades: dict[str, Any],
    browser_evidence: dict[str, Any] | None,
    accepted_tape_evidence: dict[str, Any] | None,
    guard_result: dict[str, Any] | None,
) -> bool:
    """Whether deterministic behavior/source gates permit dedicated vision."""
    phase = grades.get("phase_results") or {}
    legacy_positive = not phase and grades.get("overall_passed") is True
    phase_failed = bool(phase) and (
        phase.get("render_gate") != "pass"
        or phase.get("ui_functionality") != "pass"
        # Source inspection is optional when browser and semantic guards are
        # already authoritative. A full evaluator may legitimately mark it
        # skipped; that must not suppress the required independent visual gate.
        or phase.get("source_inspection") == "fail"
    )
    if (
        grades.get("evaluation_infrastructure_failure")
        or (not legacy_positive and (not phase or phase_failed))
        or grades.get("bugs_found")
        or grades.get("regressions_found")
        or (guard_result is not None and guard_result.get("passed") is not True)
    ):
        return False
    for evidence in (browser_evidence, accepted_tape_evidence):
        if evidence is None:
            continue
        observed = evidence.get("checks") if isinstance(evidence, dict) else None
        if not isinstance(observed, list) or any(
            not isinstance(item, dict) or item.get("status") != "ok"
            for item in observed
        ):
            return False
    return True


def _edit_visual_diagnostics_ready(
    grades: dict[str, Any],
    browser_evidence: dict[str, Any] | None,
    accepted_tape_evidence: dict[str, Any] | None,
    guard_result: dict[str, Any] | None,
) -> bool:
    """Allow one-repair Edit runs to collect all actionable findings at once."""
    phase = grades.get("phase_results") or {}
    if (
        grades.get("evaluation_infrastructure_failure")
        or phase.get("render_gate") != "pass"
        or phase.get("source_inspection") == "fail"
        or (guard_result is not None and guard_result.get("passed") is not True)
    ):
        return False
    for evidence in (browser_evidence, accepted_tape_evidence):
        if evidence is None:
            continue
        checks = evidence.get("checks") if isinstance(evidence, dict) else None
        if not isinstance(checks, list) or any(
            not isinstance(item, dict) or item.get("status") != "ok"
            for item in checks
        ):
            return False
    return True


def _raise_navigation_infrastructure(evidence: dict[str, Any]) -> None:
    failures = [item for item in evidence.get('checks', [])
                if isinstance(item, dict) and item.get('status') == 'navigation_failed']
    if failures:
        detail = '; '.join(f"{item.get('check_id')}: {item.get('navigation_error')}" for item in failures)
        raise EvaluationInfrastructureError('Browser navigation infrastructure failed: ' + detail)


def _has_observed_valid_test_failure(evidence: dict[str, Any] | None) -> bool:
    """Return true for one executed valid test that observed a product failure.

    ``action_failed`` is already distinct from an invalid test contract or an
    infrastructure error. No second execution is required to enter Repair.
    """
    return bool(
        isinstance(evidence, dict)
        and any(
            isinstance(item, dict) and item.get("status") == "action_failed"
            for item in evidence.get("checks") or []
        )
    )


def _append_hidden_risk_repairs(
    *,
    grades: dict[str, Any],
    hidden_checks: list[dict[str, Any]],
    hidden_evidence: dict[str, Any],
    failed_check_ids: list[str],
) -> None:
    """Turn an observed hidden risk failure into WebCompass Repair metadata."""
    checks_by_id = {
        str(item.get("id") or ""): item
        for item in hidden_checks
        if isinstance(item, dict)
    }
    evidence_by_id = {
        str(item.get("check_id") or ""): item
        for item in hidden_evidence.get("checks") or []
        if isinstance(item, dict)
    }
    descriptions = grades.setdefault("repair_task_descriptions", [])
    instructions = grades.setdefault("repair_instructions", [])
    existing_description_keys = {
        (str(item.get("task_type") or ""), tuple(item.get("evidence_ids") or []))
        for item in descriptions
        if isinstance(item, dict)
    }
    for check_id in failed_check_ids:
        check = checks_by_id.get(check_id, {})
        if check.get("origin") != "source_edit_risk_analysis":
            continue
        repair_type = str(check.get("repair_type") or "").strip()
        if not repair_type:
            continue
        record = evidence_by_id.get(check_id, {})
        issue_payloads = []
        for step in record.get("steps") or []:
            if (not isinstance(step, dict) or step.get("ok") is not False
                    or step.get("action") != "assert_webcompass_risk"):
                continue
            output = step.get("output") if isinstance(step.get("output"), dict) else {}
            actual = output.get("actual") if isinstance(output.get("actual"), dict) else {}
            if actual.get("defect_type") != repair_type or actual.get("passed") is not False:
                continue
            issue_payloads.extend(
                item for item in actual.get("issues") or [] if isinstance(item, dict)
            )
        if not issue_payloads:
            continue
        rendered_issues = "; ".join(
            f"{item.get('kind', 'defect')} at {item.get('element', 'target surface')}"
            for item in issue_payloads
        )
        symptom = rendered_issues or "the changed target surface failed its browser risk audit"
        description = f"{repair_type} reproduced after the normal Edit: {symptom}."
        key = (repair_type, (check_id,))
        if key not in existing_description_keys:
            descriptions.append({
                "task_type": repair_type,
                "description": description,
                "evidence_ids": [check_id],
            })
            existing_description_keys.add(key)
        repair_instruction = (
            f"Repair the observed {repair_type} on the changed UI: {symptom}. "
            "Preserve the requested Edit and accepted surrounding behavior."
        )
        if repair_instruction not in instructions:
            instructions.append(repair_instruction)


def _merge_visual_evidence_manifest(
    manifest: dict[str, Any] | None,
    *,
    round_num: int,
    app_url: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    """Add harness-owned screenshots without discarding evaluator captures."""
    existing = manifest if isinstance(manifest, dict) else {}
    refs = existing.get("screenshots")
    screenshots = [str(ref) for ref in refs] if isinstance(refs, list) else []
    for ref in evidence_refs:
        if ref not in screenshots:
            screenshots.append(ref)
    prior_notes = str(existing.get("notes", "")).strip()
    note = "Harness independently captured initial views and successful interaction states."
    return {
        "round": round_num,
        "app_url": app_url,
        "screenshots": screenshots,
        "notes": f"{prior_notes} {note}".strip(),
    }


def _visual_routes_for_checks(checks: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(
        str(check.get("route", "/"))
        for check in checks
        if isinstance(check, dict)
    )) or ["/"]


async def _capture_independent_visual_evidence(
    ctx: HarnessContext, *, app_url: str, round_num: int
) -> None:
    """Capture both top and scrolled viewport states for the visual reviewer.

    This is deliberately harness-owned: an evaluator may legitimately inspect a
    hidden-on-load control, but a visual scorer still needs a rendered state.
    The app is never altered to make an element visible for a screenshot.
    """
    from playwright.async_api import async_playwright
    from src.utils.playwright_browser import launch_chromium

    checks = ctx.sprint_state.ui_checks_for_sprint(ctx.sprint_state.current_target)
    routes = _visual_routes_for_checks(checks)
    evidence_path = ctx.file_comm.dir / f"browser_evidence_round_{round_num}.json"
    evidence = json.loads(evidence_path.read_text()) if evidence_path.is_file() else {}
    refs = [f".harness/{Path(item['screenshot']).name}" for item in evidence.get("checks", [])
            if item.get("status") == "ok" and item.get("screenshot")
            and (ctx.file_comm.dir / Path(item["screenshot"]).name).is_file()]
    if refs:
        ctx.file_comm.write_visual_manifest(round_num, {
            "round": round_num, "app_url": app_url, "screenshots": refs,
            "notes": "Successful target interaction states; planned visible feature regions are captured in full where available.",
        })
        return
    async with async_playwright() as playwright:
        browser = await launch_chromium(playwright, headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            for index, route in enumerate(routes[:3], 1):
                route_url = _same_origin_route_url(app_url, route)
                top_ref = (
                    f".harness/visual_round_{round_num}_auto_top.png"
                    if index == 1
                    else f".harness/visual_round_{round_num}_auto_route_{index}_top.png"
                )
                top_path = ctx.workdir / top_ref
                top_path.parent.mkdir(parents=True, exist_ok=True)
                await page.goto(route_url, wait_until="load", timeout=30_000)
                await page.screenshot(path=str(top_path))
                refs.append(top_ref)
                can_scroll = await page.evaluate(
                    "document.documentElement.scrollHeight > window.innerHeight + 80"
                )
                if can_scroll:
                    await page.evaluate(
                        "document.documentElement.scrollTop = Math.min(document.documentElement.scrollHeight - window.innerHeight, Math.max(500, window.innerHeight));"
                    )
                    await page.wait_for_timeout(350)
                    scrolled_ref = (
                        f".harness/visual_round_{round_num}_auto_scrolled.png"
                        if index == 1
                        else f".harness/visual_round_{round_num}_auto_route_{index}_scrolled.png"
                    )
                    await page.screenshot(path=str(ctx.workdir / scrolled_ref))
                    refs.append(scrolled_ref)
        finally:
            await browser.close()

    manifest = _merge_visual_evidence_manifest(
        ctx.file_comm.read_visual_manifest(round_num),
        round_num=round_num,
        app_url=app_url,
        evidence_refs=refs,
    )
    ctx.file_comm.write_visual_manifest(round_num, manifest)


async def run_evaluate_phase(ctx: HarnessContext, round_num: int) -> Verdict:
    """执行 evaluator 与视觉复核，推进 sprint 状态并写入检查点。"""
    if ctx.config.reverse_validate_root and read_edit_task_contract(ctx.workdir) is not None:
        return await _run_reverse_validate_evaluation(ctx, round_num)
    if ctx.config.lightweight_edit_production and read_edit_task_contract(ctx.workdir) is not None:
        return await _run_lightweight_edit_evaluation(ctx, round_num)
    logger.info("[bold yellow]EVALUATE phase")
    sprint_num = ctx.sprint_state.current_target
    sprint_ctx = ctx.sprint_state.sprint_context(sprint_num)
    started = time.perf_counter()
    ev_stats = None
    startup_error: Exception | None = None
    guard_result: dict[str, Any] | None = None
    grades: dict[str, Any] = {}
    passed = False
    browser_evidence: dict[str, Any] | None = None
    hidden_oracle_evidence: dict[str, Any] | None = None
    accepted_tape_evidence: dict[str, Any] | None = None
    hidden_checks: list[dict[str, Any]] = []
    frozen_validation: dict[str, Any] | None = None
    if read_edit_task_contract(ctx.workdir) is not None:
        frozen_validation = verify_preimplementation_validation(
            workdir=ctx.workdir,
            file_comm=ctx.file_comm,
            instruction_delta=ctx.user_prompt,
        )

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
                # A continuous Edit uses its one planner-authored browser flow
                # as the only acceptance check. The legacy DOM scope guard can
                # mistake explicitly requested additions for collateral edits.
                if frozen_validation is None and not ctx.config.lightweight_edit_production:
                    if (
                        read_edit_task_contract(ctx.workdir) is not None
                        and str(getattr(ctx.config, "minimal_path_mode", "recommended")).lower()
                        == "recommended"
                    ):
                        guard_result = post_edit_scope_sanity(
                            workdir=ctx.workdir,
                            harness_dir=ctx.file_comm.dir,
                            round_num=round_num,
                        )
                    else:
                        guard_result = await evaluate_guard(
                            workdir=ctx.workdir, file_comm=ctx.file_comm, config=ctx.config,
                            app_url=app_stack.frontend_url, round_num=round_num,
                            sprint_num=sprint_num,
                        )
                edit_card = read_edit_card(ctx.file_comm.dir)
                try:
                    if edit_card is not None and not ctx.config.lightweight_edit_production:
                        regression_selection = select_accepted_replay_checks(
                            ctx.file_comm.dir,
                            edit_card=edit_card,
                            accepted_edit_index=len(ctx.sprint_state.accepted) + 1,
                            full_replay_interval=ctx.config.edit_full_replay_interval,
                            before_round=round_num,
                            replay_all=ctx.config.edit_replay_all_accepted_checks,
                        )
                        replay_checks = list(regression_selection.pop("checks"))
                        (ctx.file_comm.dir / f"regression_selection_round_{round_num}.json").write_text(
                            json.dumps(regression_selection, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                    else:
                        regression_selection = None
                        replay_checks = []
                except AcceptedTapeError as exc:
                    raise EvaluationInfrastructureError(str(exc)) from exc
                if replay_checks and not ctx.config.lightweight_edit_production:
                    accepted_tape_evidence = await asyncio.wait_for(
                        collect_browser_evidence(
                            app_url=app_stack.frontend_url,
                            checks=replay_checks,
                            output_path=ctx.file_comm.dir
                            / f"accepted_tape_replay_round_{round_num}.json",
                            headless=ctx.config.playwright_headless,
                            fail_fast=True,
                        ),
                        timeout=75,
                    )
                    _raise_navigation_infrastructure(accepted_tape_evidence)
                    invalid_tapes = [
                        str(item.get("check_id", "unknown"))
                        for item in accepted_tape_evidence.get("checks", [])
                        if isinstance(item, dict)
                        and item.get("status")
                        in {"invalid_test_contract", "no_action_contract"}
                    ]
                    if invalid_tapes:
                        raise EvaluationInfrastructureError(
                            "Accepted tape is no longer executable for checks "
                            + ", ".join(invalid_tapes)
                        )
                # Run planner-authored concrete actions independently.  Empty
                # legacy plans remain valid; their evaluator falls back to the
                # existing exploratory path.
                visible_checks = (
                    frozen_checks_for_sprint(frozen_validation, sprint_num)
                    if frozen_validation is not None
                    else ctx.sprint_state.ui_checks_for_sprint(sprint_num)
                )
                browser_evidence_path = ctx.file_comm.dir / f"browser_evidence_round_{round_num}.json"
                try:
                    browser_evidence = await asyncio.wait_for(
                        collect_browser_evidence(
                            app_url=app_stack.frontend_url,
                            checks=visible_checks,
                            output_path=browser_evidence_path,
                            headless=ctx.config.playwright_headless,
                            capture_screenshots=True,
                            visual_sanity_selectors=(guard_result or {}).get(
                                "expected_new_fragments"
                            ) or [],
                        ),
                        timeout=75,
                    )
                except asyncio.TimeoutError as exc:
                    raise EvaluationInfrastructureError(
                        "Browser action-contract execution exceeded its 75s hard timeout; "
                        "refusing to fabricate a repair from missing evidence."
                    ) from exc
                _raise_navigation_infrastructure(browser_evidence)
                invalid_contracts = [
                    str(item.get("check_id", "unknown"))
                    for item in (browser_evidence.get("checks") or [])
                    if isinstance(item, dict) and item.get("status") == "invalid_test_contract"
                ]
                if invalid_contracts:
                    raise EvaluationInfrastructureError(
                        "Planner-authored browser contract is invalid for checks "
                        + ", ".join(invalid_contracts)
                        + "; refusing to fabricate a code repair from a broken test."
                    )
                hidden_checks = ([] if ctx.config.lightweight_edit_production else (
                    frozen_hidden_oracle_checks(frozen_validation)
                    if frozen_validation is not None
                    else read_hidden_oracle_checks(ctx.file_comm.dir)
                ))
                if hidden_checks:
                    source_risks = await asyncio.wait_for(collect_source_risk_baseline(
                        ctx.workdir, hidden_checks, headless=ctx.config.playwright_headless), timeout=75)
                    hidden_oracle_evidence = await asyncio.wait_for(
                        collect_browser_evidence(
                            app_url=app_stack.frontend_url,
                            checks=hidden_checks,
                            output_path=ctx.file_comm.dir
                            / f"hidden_oracle_evidence_round_{round_num}.json",
                            headless=ctx.config.playwright_headless,
                            capture_screenshots=True,
                            fail_fast=False,
                        ),
                        timeout=75,
                    )
                    _raise_navigation_infrastructure(hidden_oracle_evidence)
                    distinguish_preexisting_risks(hidden_oracle_evidence, source_risks)
                    (ctx.file_comm.dir / f"hidden_oracle_evidence_round_{round_num}.json").write_text(
                        json.dumps(hidden_oracle_evidence, ensure_ascii=False, indent=2) + "\n")
                    invalid_hidden = [
                        str(item.get("check_id", "unknown"))
                        for item in hidden_oracle_evidence.get("checks") or []
                        if isinstance(item, dict)
                        and item.get("status")
                        in {"invalid_test_contract", "no_action_contract"}
                    ]
                    if invalid_hidden:
                        raise EvaluationInfrastructureError(
                            "Harness-owned hidden oracle is invalid for checks "
                            + ", ".join(invalid_hidden)
                        )
                observed_test_failure = (
                    _has_observed_valid_test_failure(browser_evidence)
                    or _has_observed_valid_test_failure(hidden_oracle_evidence)
                    or _has_observed_valid_test_failure(accepted_tape_evidence)
                    or (guard_result is not None and guard_result.get("passed") is not True)
                )
                if observed_test_failure:
                    passed, grades, ev_stats = build_deterministic_failure_grades(
                        file_comm=ctx.file_comm,
                        round_num=round_num,
                        sprint_num=sprint_num,
                        sprint_context=sprint_ctx,
                        ui_checks=visible_checks,
                        edit_guard=guard_result,
                    )
                elif ctx.config.lightweight_edit_production:
                    passed, grades, ev_stats = build_lightweight_browser_grades(
                        file_comm=ctx.file_comm, round_num=round_num,
                        sprint_num=sprint_num, sprint_context=sprint_ctx,
                        ui_checks=visible_checks, evidence=browser_evidence,
                    )
                else:
                    try:
                        passed, grades, ev_stats = await asyncio.wait_for(
                            run_evaluator(
                                ctx.config, ctx.file_comm, ctx.workdir,
                                round_num=round_num, app_url=app_stack.frontend_url, edit_guard=guard_result,
                            ),
                            timeout=ctx.config.agent_phase_timeout_seconds,
                        )
                    except asyncio.TimeoutError as exc:
                        raise EvaluationInfrastructureError(
                            f"Evaluator exceeded its {ctx.config.agent_phase_timeout_seconds}s hard timeout; refusing to fabricate a repair "
                            "from an incomplete evaluation."
                        ) from exc
                conflicts = _action_contract_grade_conflicts(ctx.file_comm, round_num, grades)
                if conflicts:
                    raise EvaluationInfrastructureError(
                        "Evaluator grade contradicted complete harness browser evidence for: "
                        + ", ".join(conflicts)
                    )
                grades = _reconcile_action_contract_evidence(ctx.file_comm, round_num, grades)
                grades = _apply_browser_click_evidence_gate(ctx.workdir, round_num, grades)
                grades = _apply_accepted_tape_gate(
                    grades, accepted_tape_evidence, round_num=round_num
                )
                if regression_selection is not None:
                    grades["regression_selection"] = {
                        **regression_selection,
                        "artifact": f".harness/regression_selection_round_{round_num}.json",
                    }
                passed = _determine_passed(grades)
                if _non_visual_gates_passed(
                    grades, browser_evidence, accepted_tape_evidence, guard_result
                ) and visual_evidence_required(
                    edit_card, has_image_input=_task_has_image_input(ctx.workdir)
                ):
                    try:
                        await _capture_independent_visual_evidence(
                            ctx, app_url=app_stack.frontend_url, round_num=round_num
                        )
                    except Exception as exc:
                        logger.warning(
                            "[bold yellow]Visual evidence capture[/] skipped without "
                            f"changing the evaluator verdict: {type(exc).__name__}: {exc}"
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
                        instructions = grades.setdefault("repair_instructions", [])
                        violations = guard_result.get("violations") or []
                        for violation in violations:
                            if not isinstance(violation, dict):
                                continue
                            if violation.get("kind") == "expected_addition_missing":
                                selector = str(violation.get("fragment") or "").split("::", 1)[-1]
                                instruction = (
                                    "Implement the missing requested target element "
                                    f"{selector}; the browser checks passed but this exact "
                                    "planned DOM postcondition was absent from the guard snapshot."
                                )
                                if instruction not in instructions:
                                    instructions.append(instruction)
                        generic = (
                            "Restore every out-of-scope DOM/ARIA surface, or narrow the edit "
                            "to the declared roots."
                        )
                        if not violations and generic not in instructions:
                            instructions.append(generic)
            finally:
                await app_stack.close()

    hidden_oracle_failure = _has_observed_valid_test_failure(hidden_oracle_evidence)
    if hidden_oracle_evidence is not None:
        failed_hidden_ids = [
            str(item.get("check_id", "unknown"))
            for item in hidden_oracle_evidence.get("checks") or []
            if isinstance(item, dict) and item.get("status") not in {"ok", "blocked_by_setup"}
        ]
        grades["hidden_oracle"] = {
            "status": "failed" if failed_hidden_ids else "passed",
            "failed_check_ids": failed_hidden_ids,
            "evidence_ref": f".harness/hidden_oracle_evidence_round_{round_num}.json",
        }
        if failed_hidden_ids:
            passed = False
            grades["sprint_passed"] = False
            grades["regression_passed"] = False
            grades["overall_passed"] = False
            grades.setdefault("phase_results", {})["ui_functionality"] = "fail"
            grades.setdefault("bugs_found", []).append(
                "Harness-owned hidden browser oracle failed: "
                + ", ".join(failed_hidden_ids)
            )
            grades.setdefault("repair_instructions", []).append(
                "Repair the reproduced user-visible behavior without exposing or targeting hidden evaluator details."
            )
            _append_hidden_risk_repairs(
                grades=grades,
                hidden_checks=hidden_checks,
                hidden_evidence=hidden_oracle_evidence,
                failed_check_ids=failed_hidden_ids,
            )

    # A semantic scope violation or a failed real browser click is a reproduced
    # defect, even when the evaluator left unrelated checks unverified.
    concrete_guard_failure = _edit_guard_requires_repair(
        guard_result, grades, evaluator_mode=ctx.config.evaluator_mode
    )
    trace_click_failure = any(
        "Observed browser_click evidence failed:" in str(item)
        for item in (grades.get("bugs_found") or [])
    )
    if not passed and evaluation_is_inconclusive(grades) and not (
        concrete_guard_failure or trace_click_failure or hidden_oracle_failure
    ):
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
    # An observed valid browser failure already establishes a repair source. A
    # costly visual review cannot turn that failure into an accepted edit and
    # should not delay the next repair round.
    edit_card = read_edit_card(ctx.file_comm.dir)
    needs_visual = visual_evidence_required(
        edit_card, has_image_input=_task_has_image_input(ctx.workdir)
    )
    is_edit_task = read_edit_task_contract(ctx.workdir) is not None
    visual_ready = _non_visual_gates_passed(
        grades, browser_evidence, accepted_tape_evidence, guard_result
    )
    if (
        not visual_ready
        and is_edit_task
        and ctx.config.edit_collect_visual_failures_before_repair
    ):
        visual_ready = _edit_visual_diagnostics_ready(
            grades, browser_evidence, accepted_tape_evidence, guard_result
        )
    grades["visual_evidence_decision"] = {
        "owner": "harness", "task_mode": "edit" if is_edit_task else "generate",
        "originality_required": not is_edit_task or ctx.config.edit_originality_required,
        "reason": "Product Session novelty belongs to the product direction, not each atomic Edit.",
    }
    if (not ctx.config.lightweight_edit_production and
            ctx.config.evaluator_mode == "full" and startup_error is None and visual_ready and needs_visual):
        async with _agent_phase_session(ctx, phase_name=f"visual review round {round_num}"):
            try:
                grades, vs_stats = await asyncio.wait_for(
                    apply_dedicated_visual_review(
                        config=ctx.config, file_comm=ctx.file_comm, workdir=ctx.workdir,
                        round_num=round_num, sprint_num=sprint_num, sprint_context=sprint_ctx,
                        grades=grades, manifest=visual_manifest,
                    ),
                    timeout=ctx.config.evaluator_vision_timeout_seconds + 5,
                )
            except asyncio.TimeoutError:
                grades["evaluation_infrastructure_failure"] = {
                    "phase": "visual_review",
                    "reason": f"visual review exceeded {ctx.config.evaluator_vision_timeout_seconds + 5}s hard timeout",
                }
    if vs_stats is not None:
        _record_phase_stats(
            ctx,
            f"visual_score_r{round_num}",
            _coerce_stats(vs_stats),
            started_at=vs_started,
        )
    elif ctx.config.evaluator_mode == "full" and visual_ready and not needs_visual:
        grades.setdefault("phase_results", {})["appearance"] = "skipped"
        grades["visual_evidence_decision"] = {
            "status": "not_required",
            "reason": str((edit_card or {}).get("visual_evidence_reason") or "behavior/state-only Edit"),
            "evidence_route": ["dom", "ax_semantics", "internal_state", "real_browser"],
        }

    passed = _determine_passed(grades)

    if passed:
        if (
            read_edit_task_contract(ctx.workdir) is not None
            and str(getattr(ctx.config, "minimal_path_mode", "recommended")).lower()
            == "recommended"
        ):
            minimality = {
                "status": "skipped_by_scope_policy",
                "reason": "recommended_scope_uses_post_edit_scope_sanity",
            }
        elif not ctx.config.minimality_guard_enabled:
            minimality = {"status": "skipped_by_user_policy", "reason": "minimality_guard_disabled"}
        elif not _counterfactual_minimality_is_sound(
            grades,
            is_edit=read_edit_task_contract(ctx.workdir) is not None,
        ):
            minimality = {
                "status": "not_applicable",
                "reason": "typed_counterfactual_oracle_incomplete",
            }
        else:
            try:
                minimality = await certify_round_minimality(
                    run_dir=ctx.workdir,
                    config=ctx.config,
                    round_num=round_num,
                    sprint_num=sprint_num,
                    checks=[*visible_checks, *hidden_checks],
                    visual_accepted=(grades.get("phase_results") or {}).get("appearance") == "pass",
                )
            except asyncio.TimeoutError:
                minimality = {
                    "status": "inconclusive",
                    "reason": "minimality_oracle_hard_timeout",
                }
        if minimality is not None:
            summaries: dict[str, Any] = {}
            for kind, certificate in (minimality.get("certificates") or {}).items():
                summaries[kind] = {
                    "status": certificate.get("status"),
                    "reason": certificate.get("reason"),
                    "kept_change_ids": certificate.get("kept_change_ids", []),
                    "redundant_change_ids": certificate.get("redundant_change_ids", []),
                    "artifact": f".harness/minimality_round_{round_num}_{kind}.json",
                }
            if not summaries:
                summaries["runtime"] = minimality
            grades["minimality_certificate"] = summaries

            # A forward sprint is accepted only when its final source-to-dest
            # edit is already irreducible.  Repair-delta certificates are an
            # additional export gate and do not invalidate an otherwise clean
            # edit when the round merely reverted earlier collateral churn.
            gate = summaries.get("edit") or summaries.get("repair")
            gate_status = gate.get("status") if isinstance(gate, dict) else None
            repairable_complexity = (
                gate_status == "inconclusive"
                and isinstance(gate, dict)
                and gate.get("reason") == "too_many_atomic_changes"
            )
            if gate_status in {"non_minimal", "candidate_failed"} or repairable_complexity:
                passed = False
                grades["regression_passed"] = False
                grades["overall_passed"] = False
                redundant = ", ".join(gate.get("redundant_change_ids") or [])
                message = (
                    "Counterfactual patch guard rejected the destination: "
                    + str(gate.get("reason") or gate_status)
                )
                if redundant:
                    message += f". Removable change atoms: {redundant}"
                grades.setdefault("regressions_found", []).append(message)
                grades.setdefault("repair_instructions", []).append(
                    (
                        "Consolidate the implementation below the configured atomic-change "
                        "limit while preserving the passing browser and DOM/ARIA contracts."
                        if repairable_complexity
                        else "Remove the redundant atomic changes identified in the minimality "
                        "certificate while preserving the passing browser and DOM/ARIA contracts."
                    )
                )
            elif gate_status not in {None, "certified", "not_applicable"}:
                grades["evaluation_infrastructure_failure"] = {
                    "phase": "counterfactual_minimality",
                    "reason": str(gate.get("reason") or gate_status),
                }

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
    if not passed and read_edit_task_contract(ctx.workdir) is not None:
        packet = write_repair_packet(
            workdir=ctx.workdir,
            round_num=round_num,
            sprint_num=sprint_num,
            grades=grades,
        )
        if packet.get("status") != "repairable":
            raise EvaluationInfrastructureError(
                "Failed Edit has no identifiable browser/semantic evidence; refusing an open-ended Repair round."
            )
        grades["repair_packet"] = {
            "status": packet["status"],
            "artifact": f".harness/repair_packet_round_{round_num}.json",
            "failed_check_ids": [
                str(item.get("check_id", "unknown")) for item in packet.get("failed_checks") or []
            ],
        }
    # Keep Skill learning separate from the repair itself.  The packet is the
    # current code handoff; this artifact is a proposed reusable change that
    # remains disabled until the same sample and regression flow pass.
    if read_edit_task_contract(ctx.workdir) is not None:
        # Keep feedback as its own artifact.  ``Grades`` is a strict persisted
        # schema and must not be extended with the routing payload.
        write_skill_feedback(workdir=ctx.workdir, round_num=round_num, grades=grades)
    if isinstance(grades.get("criteria"), dict) and "round" in grades:
        current_checks = ctx.sprint_state.ui_checks_for_sprint(sprint_num)
        if (
            recommendation in {"generate_next_sprint", "complete"}
            and browser_evidence is not None
            and _checks_are_tape_eligible(current_checks)
        ):
            try:
                tape_path = append_accepted_tape(
                    harness_dir=ctx.file_comm.dir,
                    sprint_num=sprint_num,
                    round_num=round_num,
                    checks=current_checks,
                    evidence=browser_evidence,
                )
            except AcceptedTapeError as exc:
                raise EvaluationInfrastructureError(
                    "Accepted checkpoint could not be recorded as a typed tape: " + str(exc)
                ) from exc
            grades["accepted_tape"] = {
                "status": "recorded",
                "artifact": f".harness/{tape_path.name}",
                "sprint": sprint_num,
                "round": round_num,
            }
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
