from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any


@dataclass
class GradingCriterion:
    """描述单个评分维度的名称、权重与及格阈值。"""

    name: str
    description: str
    weight: float
    threshold: float


CRITERIA: list[GradingCriterion] = [
    GradingCriterion(
        name="design_quality",
        description=(
            "Does the design feel coherent, intentional, and emotionally "
            "distinct rather than assembled from generic parts?"
        ),
        weight=0.30,
        threshold=6.0,
    ),
    GradingCriterion(
        name="functionality",
        description=(
            "Does the application actually work when you use it? Can users "
            "complete core tasks without errors?"
        ),
        weight=0.25,
        threshold=6.0,
    ),
    GradingCriterion(
        name="originality",
        description=(
            "Does the interface show deliberate creative choices rather than "
            "template layouts, stock defaults, or generic AI patterns?"
        ),
        weight=0.25,
        threshold=5.0,
    ),
    GradingCriterion(
        name="craft",
        description=(
            "Are the fundamentals executed well: typography, spacing, color, "
            "responsiveness, contrast, and interaction polish?"
        ),
        weight=0.20,
        threshold=6.0,
    ),
]

_CRITERIA_THRESHOLDS: dict[str, float] = {
    criterion.name: criterion.threshold for criterion in CRITERIA
}
VISION_OWNED_CRITERIA = ("design_quality", "originality", "craft")
_TRUTHY_STRINGS = frozenset({"true", "yes", "1", "y", "t", "pass", "passed", "ok"})
_FALSEY_STRINGS = frozenset({"false", "no", "0", "n", "f", "fail", "failed"})
_FAIL_STATUSES = frozenset({"fail", "failed", "partial"})
_UNVERIFIED_MARKERS = (
    "could not verify",
    "could not be fully verified",
    "not verified",
    "unable to verify",
    "not conclusively",
    "not explicitly tested",
    "not fully tested",
    "not fully verified",
    "within evaluation budget",
    "cannot confirm",
    "not confirmed",
    "not observed",
    "not directly verified",
    "may not",
    "appear incomplete",
    "not captured",
    "budget exhaustion",
    "not tested",
    "testing constraints",
)


def criterion_threshold(name: str, *, default: float = 0.0) -> float:
    """读取评分项阈值；未知名称按调用方指定默认值处理。"""
    return _CRITERIA_THRESHOLDS.get(name, default)


def check_grades(grades: dict) -> bool:
    """检查所有评分项是否都具备合法分数且达到对应阈值。"""
    criteria_block = grades.get("criteria") if isinstance(grades, dict) else None
    if not isinstance(criteria_block, dict):
        return False

    for criterion in CRITERIA:
        score_data = criteria_block.get(criterion.name)
        if not isinstance(score_data, dict):
            return False
        score = score_data.get("score")
        # Python 中 bool 是 int 的子类，这里显式排除，避免误判为有效分数。
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return False
        score_float = float(score)
        if math.isnan(score_float) or math.isinf(score_float):
            return False
        if score_float < criterion.threshold:
            return False
    return True


def parse_tristate(value: Any) -> bool | None:
    """将 agent 输出解析为 True / False / 未知三态值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUTHY_STRINGS:
            return True
        if normalized in _FALSEY_STRINGS:
            return False
    return None


def _has_failed_critical_ui_checks(grades: dict[str, Any]) -> bool:
    for check in grades.get("ui_checks", []):
        if not isinstance(check, dict):
            continue
        if parse_tristate(check.get("critical")) is not True:
            continue
        status = str(check.get("status", "")).strip().lower()
        if status in _FAIL_STATUSES:
            return True
    return False


def _only_unverified_partial_blockers(grades: dict[str, Any]) -> bool:
    """Accept when the sole negative signal is evaluator coverage uncertainty."""
    if not check_grades(grades) or _has_failed_critical_exit_criteria(grades):
        return False
    phase_results = grades.get("phase_results") or {}
    if any(str(value).lower() == "fail" for value in phase_results.values()):
        return False
    found_partial = False
    for check in grades.get("ui_checks", []):
        if not isinstance(check, dict) or parse_tristate(check.get("critical")) is not True:
            continue
        status = str(check.get("status", "")).strip().lower()
        if status in {"fail", "failed"}:
            return False
        if status == "partial":
            found_partial = True
            notes = str(check.get("notes", "")).lower()
            if not any(marker in notes for marker in _UNVERIFIED_MARKERS):
                return False
    return found_partial


def _has_failed_critical_exit_criteria(grades: dict[str, Any]) -> bool:
    for result in grades.get("target_exit_criteria_results", []):
        if not isinstance(result, dict):
            continue
        if parse_tristate(result.get("critical")) is not True:
            continue
        if parse_tristate(result.get("passed")) is False:
            return True
    return False


def evaluation_is_inconclusive(grades: dict[str, Any] | None) -> bool:
    """True when a fail verdict contains uncertainty but no reproduced defect."""
    if not grades:
        return False
    phase = grades.get("phase_results") or {}
    if str(phase.get("render_gate", "")).lower() == "fail":
        return False
    negative_notes: list[str] = []
    for item in grades.get("target_exit_criteria_results", []):
        if isinstance(item, dict) and parse_tristate(item.get("passed")) is False:
            negative_notes.append(str(item.get("notes", "")))
    for item in grades.get("ui_checks", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("status", "")).lower() in _FAIL_STATUSES:
            negative_notes.append(str(item.get("notes", "")))
    if not negative_notes:
        return False
    return all(
        any(marker in note.lower() for marker in _UNVERIFIED_MARKERS)
        for note in negative_notes
    )


def determine_passed(grades: dict[str, Any] | None) -> bool:
    """按关键字段与评分阈值综合判断当前轮是否通过。"""
    if not grades:
        return False

    # A regression/scope audit is a hard acceptance gate.  A feature may work
    # while still mutating protected DOM or ARIA surfaces, which must schedule
    # repair rather than advance the sprint state.
    if parse_tristate(grades.get("regression_passed")) is False:
        return False

    if _only_unverified_partial_blockers(grades):
        return True

    if parse_tristate(grades.get("sprint_passed")) is False:
        return False

    if _has_failed_critical_ui_checks(grades):
        return False

    if _has_failed_critical_exit_criteria(grades):
        return False

    overall_passed = parse_tristate(grades.get("overall_passed"))
    if overall_passed is not None:
        return overall_passed and check_grades(grades)

    return check_grades(grades)


def visual_review_failure(
    grades: dict[str, Any], reason: str, *, infrastructure_failure: bool = False
) -> dict[str, Any]:
    """复制 grades，并把视觉评分负责的字段统一标记为失败。"""
    merged = json.loads(json.dumps(grades))

    phase_results = merged.setdefault("phase_results", {})
    if isinstance(phase_results, dict):
        phase_results["appearance"] = "fail"

    criteria = merged.setdefault("criteria", {})
    if isinstance(criteria, dict):
        for name in VISION_OWNED_CRITERIA:
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
    if infrastructure_failure:
        merged["evaluation_infrastructure_failure"] = {
            "phase": "visual_review",
            "reason": reason,
        }
    if merged.get("sprint_passed") is True:
        merged["sprint_passed"] = False
    return merged


def apply_visual_review_scores(
    grades: dict[str, Any], normalized: dict[str, Any]
) -> dict[str, Any]:
    """把视觉复核结果合并回 grades，并重新计算总体通过状态。"""
    merged = json.loads(json.dumps(grades))
    phase_results = merged.setdefault("phase_results", {})
    if isinstance(phase_results, dict):
        phase_results["appearance"] = normalized["phase_result"]

    merged["appearance_review"] = normalized["appearance_review"]
    criteria = merged.setdefault("criteria", {})
    if isinstance(criteria, dict):
        for name, value in normalized["criteria_scores"].items():
            score = value["score"]
            criteria[name] = {
                "score": score,
                "passed": score >= criterion_threshold(name),
                "notes": value["notes"],
            }

    merged["overall_passed"] = check_grades(merged)
    if merged["overall_passed"] is False:
        merged["mode_recommendation"] = "repair"
        if merged.get("sprint_passed") is True:
            merged["sprint_passed"] = False
        merged_criteria = merged.get("criteria")
        visual_failures = (
            [
                str(merged_criteria.get(name, {}).get("notes", "")).strip()
                for name in VISION_OWNED_CRITERIA
                if merged_criteria.get(name, {}).get("passed") is False
            ]
            if isinstance(merged_criteria, dict)
            else []
        )
        if visual_failures:
            repair_instruction = (
                "Address the visual-review finding while preserving the accepted "
                "interaction behavior: " + " ".join(visual_failures)
            )
            instructions = merged.setdefault("repair_instructions", [])
            if repair_instruction not in instructions:
                instructions.append(repair_instruction)
    return merged
