import math

import pytest

from src.prompts.grading import (
    CRITERIA,
    VISION_OWNED_CRITERIA,
    apply_visual_review_scores,
    check_grades,
    criterion_threshold,
    determine_passed,
    evaluation_is_inconclusive,
    visual_review_failure,
)


def test_all_pass():
    grades = {
        "criteria": {
            "design_quality": {"score": 7.0, "passed": True, "notes": "ok"},
            "functionality": {"score": 8.0, "passed": True, "notes": "ok"},
            "originality": {"score": 6.0, "passed": True, "notes": "ok"},
            "craft": {"score": 7.0, "passed": True, "notes": "ok"},
        },
        "overall_passed": True,
        "bugs_found": [],
        "missing_features": [],
    }
    assert check_grades(grades) is True


def test_one_fail():
    grades = {
        "criteria": {
            "design_quality": {"score": 5.0, "passed": False, "notes": "generic"},
            "functionality": {"score": 8.0, "passed": True, "notes": "ok"},
            "originality": {"score": 6.0, "passed": True, "notes": "ok"},
            "craft": {"score": 7.0, "passed": True, "notes": "ok"},
        },
        "overall_passed": False,
        "bugs_found": ["missing interactions"],
        "missing_features": ["drag and drop"],
    }
    assert check_grades(grades) is False


def test_missing_criterion():
    grades = {
        "criteria": {
            "design_quality": {"score": 7.0, "passed": True, "notes": "ok"},
        },
        "overall_passed": True,
        "bugs_found": [],
        "missing_features": [],
    }
    assert check_grades(grades) is False


def test_thresholds():
    for c in CRITERIA:
        assert c.threshold > 0
        assert c.weight > 0
    total_weight = sum(c.weight for c in CRITERIA)
    assert abs(total_weight - 1.0) < 0.01


def _grades_with(design_score):
    return {
        "criteria": {
            "design_quality": {"score": design_score, "passed": True, "notes": ""},
            "functionality": {"score": 8.0, "passed": True, "notes": ""},
            "originality": {"score": 6.0, "passed": True, "notes": ""},
            "craft": {"score": 7.0, "passed": True, "notes": ""},
        },
    }


@pytest.mark.parametrize(
    "score,expected",
    [
        (5.99, False),
        (6.0, True),  # exactly the design_quality threshold
        (6, True),
        (None, False),
        ("7", False),
        ("ok", False),
        (math.nan, False),
        (math.inf, False),
        (-math.inf, False),
        (-1.0, False),
        (True, False),  # bool is subclass of int but must be rejected
        (False, False),
    ],
)
def test_check_grades_score_type_robustness(score, expected):
    assert check_grades(_grades_with(score)) is expected


def test_check_grades_handles_non_dict_criteria_block():
    assert check_grades({"criteria": None}) is False
    assert check_grades({"criteria": "not-a-dict"}) is False
    assert check_grades({}) is False


def test_check_grades_handles_non_dict_score_data():
    grades = {
        "criteria": {
            "design_quality": "oops",
            "functionality": {"score": 8.0},
            "originality": {"score": 6.0},
            "craft": {"score": 7.0},
        },
    }
    assert check_grades(grades) is False


def test_criterion_threshold_returns_configured_value_or_default():
    assert criterion_threshold("design_quality") == 6.0
    assert criterion_threshold("unknown", default=6.0) == 6.0


def test_determine_passed_rejects_failed_critical_ui_check_without_overall_flag():
    grades = _grades_with(7.0)
    grades["ui_checks"] = [
        {
            "critical": True,
            "status": "partial",
        }
    ]

    assert determine_passed(grades) is False


def test_determine_passed_rejects_failed_critical_exit_criterion_without_overall_flag():
    grades = _grades_with(7.0)
    grades["target_exit_criteria_results"] = [
        {
            "critical": "yes",
            "passed": "failed",
        }
    ]

    assert determine_passed(grades) is False


def test_determine_passed_ignores_unverified_partial_when_all_evidence_passes():
    grades = _grades_with(7.0)
    grades.update({
        "overall_passed": False,
        "sprint_passed": False,
        "phase_results": {
            "render_gate": "pass", "ui_functionality": "pass", "appearance": "pass"
        },
        "target_exit_criteria_results": [
            {"critical": True, "passed": True, "notes": "Filter works."}
        ],
        "ui_checks": [{
            "critical": True,
            "status": "partial",
            "notes": "Filtering was not conclusively demonstrated by automation.",
        }],
    })
    assert determine_passed(grades) is True


def test_evaluation_is_inconclusive_when_all_failures_are_unverified():
    grades = {
        "phase_results": {"render_gate": "pass", "ui_functionality": "fail"},
        "target_exit_criteria_results": [{
            "critical": True,
            "passed": False,
            "notes": "Calendar behavior was not verified with browser evidence.",
        }],
        "ui_checks": [{
            "critical": True,
            "status": "fail",
            "notes": "Conflict badge was not observed.",
        }],
    }
    assert evaluation_is_inconclusive(grades) is True


def test_evaluation_is_not_inconclusive_with_reproduced_failure():
    grades = {
        "phase_results": {"render_gate": "pass", "ui_functionality": "fail"},
        "ui_checks": [{
            "critical": True,
            "status": "fail",
            "notes": "Clicked Save; the item count remained 0 and console logged TypeError.",
        }],
    }
    assert evaluation_is_inconclusive(grades) is False


def test_visual_review_failure_marks_visual_criteria_and_repair():
    grades = _grades_with(7.0)
    grades["sprint_passed"] = True
    grades["mode_recommendation"] = "complete"

    failed = visual_review_failure(grades, "no screenshots available")

    assert failed["overall_passed"] is False
    assert failed["mode_recommendation"] == "repair"
    assert failed["sprint_passed"] is False
    assert failed["phase_results"]["appearance"] == "fail"
    for name in VISION_OWNED_CRITERIA:
        assert failed["criteria"][name]["score"] == 0.0
        assert failed["criteria"][name]["passed"] is False
    assert grades["mode_recommendation"] == "complete"


def test_apply_visual_review_scores_recomputes_overall_passed():
    grades = _grades_with(7.0)
    grades["overall_passed"] = True
    grades["mode_recommendation"] = "complete"
    normalized = {
        "phase_result": "pass",
        "appearance_review": {"screenshots": ["home.png"], "notes": "ok"},
        "criteria_scores": {
            "design_quality": {"score": 7.0, "notes": "ok"},
            "originality": {"score": 6.0, "notes": "ok"},
            "craft": {"score": 5.0, "notes": "below threshold"},
        },
    }

    merged = apply_visual_review_scores(grades, normalized)

    assert merged["phase_results"]["appearance"] == "pass"
    assert merged["appearance_review"]["screenshots"] == ["home.png"]
    assert merged["criteria"]["craft"]["passed"] is False
    assert merged["overall_passed"] is False
    assert merged["mode_recommendation"] == "repair"
    assert grades["overall_passed"] is True
