from __future__ import annotations

from pathlib import Path
from typing import Any

from src.agents.sdk_runner import AgentRunStats
from src.agents.vision_scorer import normalize_visual_review, run_visual_appearance_review
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.round_artifacts import RoundArtifacts
from src.orchestration.visual_review_round import VisualReviewRound


def discover_visual_screenshots(
    file_comm: FileComm,
    round_num: int,
    manifest: dict[str, Any] | None,
    grades: dict[str, Any] | None = None,
) -> list[str]:
    """按 manifest、既有 grades、文件兜底三层顺序收集截图列表。"""
    return RoundArtifacts(file_comm, round_num).visual_screenshot_refs(
        manifest=manifest,
        grades=grades,
    )


async def apply_dedicated_visual_review(
    *,
    config: HarnessConfig,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
    sprint_num: int,
    sprint_context: dict[str, Any],
    grades: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> tuple[dict[str, Any], AgentRunStats | None]:
    """执行独立视觉复核，并把结果合并回 evaluator 评分。"""
    return await VisualReviewRound(
        config=config,
        file_comm=file_comm,
        workdir=workdir,
        round_num=round_num,
        sprint_num=sprint_num,
        sprint_context=sprint_context,
    ).apply(
        grades=grades,
        manifest=manifest,
        reviewer=run_visual_appearance_review,
        normalizer=normalize_visual_review,
    )


def render_feedback_from_grades(grades: dict[str, Any]) -> str:
    """将结构化评分结果整理成可读的 Markdown 反馈。"""
    round_num = grades.get("round", 0)
    sprint_num = grades.get("sprint", 0)
    sprint_result = "PASS" if grades.get("sprint_passed") else "FAIL"
    regression_result = "PASS" if grades.get("regression_passed", True) else "FAIL"
    recommendation = str(grades.get("mode_recommendation", "repair"))
    phase_results = grades.get("phase_results", {})
    appearance = grades.get("appearance_review", {})

    def _phase_status(name: str) -> str:
        value = ""
        if isinstance(phase_results, dict):
            value = str(phase_results.get(name, "skipped")).strip().lower()
        mapping = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIPPED"}
        return mapping.get(value, value.upper() or "SKIPPED")

    lines = [
        f"# Round {round_num} Feedback",
        "",
        "## Verdict",
        f"- Sprint: {sprint_num}",
        f"- Sprint Result: {sprint_result}",
        f"- Regression Result: {regression_result}",
        f"- Recommendation: {recommendation}",
        "",
        "## Phase Summary",
        f"- Render Gate: {_phase_status('render_gate')}",
        f"- UI Functionality: {_phase_status('ui_functionality')}",
        f"- Appearance: {_phase_status('appearance')}",
        f"- Source Inspection: {_phase_status('source_inspection')}",
        "",
        "## Exit Criteria Check",
    ]

    exit_results = grades.get("target_exit_criteria_results", [])
    if isinstance(exit_results, list) and exit_results:
        for index, item in enumerate(exit_results, start=1):
            if not isinstance(item, dict):
                continue
            status = "PASS" if item.get("passed") else "FAIL"
            criterion = str(item.get("criterion", "")).strip()
            notes = str(item.get("notes", "")).strip()
            lines.append(f"{index}. [{status}] {criterion} {notes}".strip())
    else:
        lines.append("1. None recorded.")

    lines.extend(["", "## UI Checks"])
    ui_checks = grades.get("ui_checks", [])
    if isinstance(ui_checks, list) and ui_checks:
        for index, item in enumerate(ui_checks, start=1):
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "unknown")).upper()
            task = str(item.get("task", "")).strip()
            notes = str(item.get("notes", "")).strip()
            lines.append(f"{index}. [{status}] {task} {notes}".strip())
    else:
        lines.append("1. None recorded.")

    lines.extend(["", "## Appearance Review"])
    screenshot_line = ", ".join(appearance.get("screenshots", [])) if isinstance(appearance, dict) else ""
    if screenshot_line:
        lines.append(f"1. Screenshots: {screenshot_line}")
    lines.append(
        "2. "
        f"render_stability={appearance.get('render_stability', '')}, "
        f"content_relevance={appearance.get('content_relevance', '')}, "
        f"layout_harmony={appearance.get('layout_harmony', '')}, "
        f"modernness_memorability={appearance.get('modernness_memorability', '')}, "
        f"token_adherence={appearance.get('token_adherence', '')}"
    )
    lines.append(f"3. {str(appearance.get('notes', '')).strip() or 'No notes recorded.'}")

    lines.extend(["", "## Bugs"])
    bugs = grades.get("bugs_found", [])
    if isinstance(bugs, list) and bugs:
        for index, bug in enumerate(bugs, start=1):
            lines.append(f"{index}. {bug}")
    else:
        lines.append("1. None recorded.")

    lines.extend(["", "## Regressions"])
    regressions = grades.get("regressions_found", [])
    if isinstance(regressions, list) and regressions:
        for index, item in enumerate(regressions, start=1):
            lines.append(f"{index}. {item}")
    else:
        lines.append("1. None recorded.")

    lines.extend(["", "## Repair Instructions"])
    repairs = grades.get("repair_instructions", [])
    if isinstance(repairs, list) and repairs:
        for index, item in enumerate(repairs, start=1):
            lines.append(f"{index}. {item}")
    else:
        lines.append("1. None recorded.")

    return "\n".join(lines)
