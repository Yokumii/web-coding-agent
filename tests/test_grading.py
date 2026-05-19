import math

import pytest

from src.prompts.grading import CRITERIA, check_grades


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
