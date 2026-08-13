"""harness 的三个阶段实现。

每个阶段函数负责本阶段的检查点写入、成本累计与 phase_metrics 记录。
"""
from __future__ import annotations

import asyncio
import json
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
from src.orchestration.edit_dom_guard import (
    capture_baseline,
    capture_sprint_source_baseline,
    evaluate_guard,
    is_forward_edit,
    repair_baseline_name,
    snapshot_semantic_dom,
    sprint_baseline_name,
)
from src.orchestration.browser_evidence import collect_browser_evidence
from src.orchestration.minimality_runtime import (
    certify_round_minimality,
    ensure_minimality_policy,
    record_round_build_destination,
    record_round_build_source,
)
from src.orchestration.minimal_path_guidance import (
    discover_page_routes,
    ensure_minimal_path_plan,
)
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
    frontend_dir = ctx.workdir / "frontend"
    semantic_routes = discover_page_routes(ctx.workdir) or None
    baseline_path = ctx.file_comm.dir / "edit_dom_baseline.json"
    sprint_baseline_path = ctx.file_comm.dir / sprint_baseline_name(sprint_num)
    if is_forward_edit(ctx.workdir) and (
        not baseline_path.exists() or not sprint_baseline_path.exists()
    ):
        # Capture the accepted source before the editor can touch it.  The
        # global seed frame supports provenance; the per-sprint frame prevents
        # earlier accepted edits from looking like new collateral damage.
        app_stack = await start_app_stack(ctx.workdir, ctx.file_comm.dir, ctx.config, round_num)
        try:
            if not baseline_path.exists():
                snapshot = await capture_baseline(
                    workdir=ctx.workdir, file_comm=ctx.file_comm, config=ctx.config,
                    app_url=app_stack.frontend_url,
                    routes=semantic_routes,
                )
                if not sprint_baseline_path.exists():
                    sprint_baseline_path.write_text(
                        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            elif not sprint_baseline_path.exists():
                await capture_sprint_source_baseline(
                    file_comm=ctx.file_comm, config=ctx.config,
                    app_url=app_stack.frontend_url, sprint_num=sprint_num,
                    routes=semantic_routes,
                )
        finally:
            await app_stack.close()
    elif mode == "repair" and (frontend_dir / ".git").exists():
        repair_baseline_path = ctx.file_comm.dir / repair_baseline_name(round_num)
        previous = ctx.file_comm.read_grades(round_num - 1) or {}
        render_failed = (previous.get("phase_results") or {}).get("render_gate") == "fail"
        if not repair_baseline_path.exists() and not render_failed:
            # For a repair that did not originate from a forward-edit seed,
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
                repair_baseline_path.write_text(
                    json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            finally:
                await app_stack.close()
    guide_minimal_path = (
        ctx.config.minimal_path_guidance_enabled
        and (is_forward_edit(ctx.workdir) or mode == "repair")
        and frontend_dir.is_dir()
    )
    if guide_minimal_path:
        ensure_minimal_path_plan(
            workdir=ctx.workdir,
            harness_dir=ctx.file_comm.dir,
            round_num=round_num,
            sprint_num=sprint_num,
            mode=mode,
            max_patch_lines=ctx.config.minimal_path_max_patch_lines,
            max_touched_files=ctx.config.minimal_path_max_touched_files,
        )
    track_minimality = (
        ctx.config.minimality_guard_enabled
        and (is_forward_edit(ctx.workdir) or mode == "repair")
        and (frontend_dir / ".git").exists()
    )
    if track_minimality:
        ensure_minimality_policy(ctx.file_comm.dir, ctx.config)
        record_round_build_source(
            ctx.file_comm.dir, frontend_dir, round_num=round_num,
            sprint_num=sprint_num, mode=mode,
        )
    async with _agent_phase_session(ctx, phase_name=f"generator round {round_num}"):
        ctx.sprint_state.mark_sprint_in_progress(sprint_num)
        started = time.perf_counter()
        raw_stats = await run_generator(
            ctx.config, ctx.file_comm, ctx.workdir,
            round_num=round_num, sprint_num=sprint_num, mode=mode,
        )
        if track_minimality:
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
            feature_status[feature_id] = passed

    if not changed:
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
    note = "Harness independently captured top and scrolled visual evidence."
    return {
        "round": round_num,
        "app_url": app_url,
        "screenshots": screenshots,
        "notes": f"{prior_notes} {note}".strip(),
    }


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

    refs = [f".harness/visual_round_{round_num}_auto_top.png"]
    top_path = ctx.workdir / refs[0]
    top_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await launch_chromium(playwright, headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1440, "height": 900})
            await page.goto(app_url, wait_until="networkidle", timeout=30_000)
            await page.screenshot(path=str(top_path))
            can_scroll = await page.evaluate(
                "document.documentElement.scrollHeight > window.innerHeight + 80"
            )
            if can_scroll:
                await page.evaluate(
                    "window.scrollTo(0, Math.min(document.documentElement.scrollHeight - window.innerHeight, Math.max(500, window.innerHeight)));"
                )
                await page.wait_for_timeout(350)
                scrolled_ref = f".harness/visual_round_{round_num}_auto_scrolled.png"
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
    logger.info("[bold yellow]EVALUATE phase")
    sprint_num = ctx.sprint_state.current_target
    sprint_ctx = ctx.sprint_state.sprint_context(sprint_num)
    started = time.perf_counter()
    ev_stats = None
    startup_error: Exception | None = None
    guard_result: dict[str, Any] | None = None
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
                    sprint_num=sprint_num,
                )
                # Run planner-authored concrete actions independently.  Empty
                # legacy plans remain valid; their evaluator falls back to the
                # existing exploratory path.
                browser_evidence_path = ctx.file_comm.dir / f"browser_evidence_round_{round_num}.json"
                try:
                    browser_evidence = await asyncio.wait_for(
                        collect_browser_evidence(
                            app_url=app_stack.frontend_url,
                            checks=ctx.sprint_state.ui_checks_for_sprint(sprint_num),
                            output_path=browser_evidence_path,
                            headless=ctx.config.playwright_headless,
                        ),
                        timeout=75,
                    )
                except asyncio.TimeoutError as exc:
                    raise EvaluationInfrastructureError(
                        "Browser action-contract execution exceeded its 75s hard timeout; "
                        "refusing to fabricate a repair from missing evidence."
                    ) from exc
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
                try:
                    passed, grades, ev_stats = await asyncio.wait_for(
                        run_evaluator(
                            ctx.config, ctx.file_comm, ctx.workdir,
                            round_num=round_num, app_url=app_stack.frontend_url, edit_guard=guard_result,
                        ),
                        timeout=180,
                    )
                except asyncio.TimeoutError as exc:
                    raise EvaluationInfrastructureError(
                        "Evaluator exceeded its 180s hard timeout; refusing to fabricate a repair "
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
                passed = _determine_passed(grades)
                if passed:
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
                        grades.setdefault("repair_instructions", []).append(
                            "Restore every out-of-scope DOM/ARIA surface, or narrow the edit to the declared roots."
                        )
            finally:
                await app_stack.close()

    # A semantic scope violation or a failed real browser click is a reproduced
    # defect, even when the evaluator left unrelated checks unverified.
    concrete_guard_failure = _edit_guard_requires_repair(
        guard_result, grades, evaluator_mode=ctx.config.evaluator_mode
    )
    trace_click_failure = any(
        "Observed browser_click evidence failed:" in str(item)
        for item in (grades.get("bugs_found") or [])
    )
    if not passed and evaluation_is_inconclusive(grades) and not (concrete_guard_failure or trace_click_failure):
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
    # A reproduced browser failure already establishes a repair source.  A
    # costly visual review cannot turn that failure into an accepted edit and
    # should not delay the next repair round.
    if ctx.config.evaluator_mode == "full" and startup_error is None and passed:
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

    if passed:
        try:
            minimality = await certify_round_minimality(
                run_dir=ctx.workdir,
                config=ctx.config,
                round_num=round_num,
                sprint_num=sprint_num,
                checks=ctx.sprint_state.ui_checks_for_sprint(sprint_num),
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
            if gate_status in {"non_minimal", "candidate_failed"}:
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
                    "Remove the redundant atomic changes identified in the minimality "
                    "certificate while preserving the passing browser and DOM/ARIA contracts."
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
