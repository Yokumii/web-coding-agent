from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agents.sdk_runner import AgentRunStats
from src.agents.vision_scorer import normalize_visual_review, run_visual_appearance_review
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.prompts.grading import CRITERIA, check_grades
from src.utils.logger import get_logger

logger = get_logger(__name__)
_CRITERION_THRESHOLDS = {criterion.name: criterion.threshold for criterion in CRITERIA}


def _criterion_threshold(name: str) -> float:
    """读取评分项阈值；未知名称按 0 处理。"""
    return _CRITERION_THRESHOLDS.get(name, 0.0)


def discover_visual_screenshots(
    file_comm: FileComm,
    round_num: int,
    manifest: dict[str, Any] | None,
    grades: dict[str, Any] | None = None,
) -> list[str]:
    """按 manifest、既有 grades、文件兜底三层顺序收集截图列表。"""
    if isinstance(manifest, dict):
        screenshots = manifest.get("screenshots")
        if isinstance(screenshots, list):
            normalized = [str(item).strip() for item in screenshots if str(item).strip()]
            if normalized:
                return normalized

    if isinstance(grades, dict):
        appearance_review = grades.get("appearance_review")
        if isinstance(appearance_review, dict):
            screenshots = appearance_review.get("screenshots")
            if isinstance(screenshots, list):
                normalized = [str(item).strip() for item in screenshots if str(item).strip()]
                if normalized:
                    return normalized

    matches = sorted(file_comm.dir.glob(f"visual_round_{round_num}_*.png"))
    return [f".harness/{path.name}" for path in matches]


_VISION_OWNED_CRITERIA = ("design_quality", "originality", "craft")


def _force_visual_review_failure(
    grades: dict[str, Any], reason: str
) -> dict[str, Any]:
    """复制 grades，并把视觉评分负责的字段统一标记为失败。"""
    merged = json.loads(json.dumps(grades))

    phase_results = merged.setdefault("phase_results", {})
    if isinstance(phase_results, dict):
        phase_results["appearance"] = "fail"

    criteria = merged.setdefault("criteria", {})
    if isinstance(criteria, dict):
        for name in _VISION_OWNED_CRITERIA:
            criteria[name] = {
                "score": 0.0,
                "passed": False,
                "notes": f"vision scorer unavailable: {reason}",
            }

    appearance = merged.setdefault("appearance_review", {})
    if isinstance(appearance, dict):
        appearance.setdefault("screenshots", [])
        appearance["notes"] = f"Visual review failed: {reason}"

    merged["overall_passed"] = False
    merged["mode_recommendation"] = "repair"
    if merged.get("sprint_passed") is True:
        merged["sprint_passed"] = False
    return merged


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
    screenshot_paths = discover_visual_screenshots(file_comm, round_num, manifest, grades)
    if not screenshot_paths:
        logger.warning(
            f"[bold yellow]Visual review[/] round {round_num} found no screenshots; "
            f"failing the appearance phase closed"
        )
        return _force_visual_review_failure(grades, "no screenshots available"), None

    try:
        review, vision_stats = await run_visual_appearance_review(
            config=config,
            file_comm=file_comm,
            workdir=workdir,
            sprint_num=sprint_num,
            sprint_context=sprint_context,
            screenshot_paths=screenshot_paths,
        )
    except Exception as exc:
        logger.warning(
            f"[bold yellow]Visual review[/] round {round_num} failed; "
            f"failing the appearance phase closed: {exc}"
        )
        return _force_visual_review_failure(grades, str(exc)), None

    normalized = normalize_visual_review(review, screenshot_paths)
    merged = json.loads(json.dumps(grades))
    phase_results = merged.setdefault("phase_results", {})
    if isinstance(phase_results, dict):
        phase_results["appearance"] = normalized["phase_result"]

    merged["appearance_review"] = normalized["appearance_review"]
    criteria = merged.setdefault("criteria", {})
    if isinstance(criteria, dict):
        for name, value in normalized["criteria_scores"].items():
            threshold = _criterion_threshold(name)
            criteria[name] = {
                "score": value["score"],
                "passed": value["score"] >= threshold,
                "notes": value["notes"],
            }

    merged["overall_passed"] = check_grades(merged)
    if merged["overall_passed"] is False:
        merged["mode_recommendation"] = "repair"
        if merged.get("sprint_passed") is True:
            merged["sprint_passed"] = False
    return merged, vision_stats


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
