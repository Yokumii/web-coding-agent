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
