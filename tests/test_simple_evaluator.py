from src.agents.simple_evaluator import build_simple_grades


def test_simple_grades_pass_clean_render():
    grades = build_simple_grades(round_num=2, sprint_num=1, title="Ledger", body_text="Useful dashboard content", page_errors=[])
    assert grades["overall_passed"] is True
    assert grades["mode_recommendation"] == "generate_next_sprint"


def test_simple_grades_fail_runtime_error():
    grades = build_simple_grades(round_num=2, sprint_num=1, title="Ledger", body_text="Useful dashboard content", page_errors=["boom"])
    assert grades["overall_passed"] is False
    assert grades["mode_recommendation"] == "repair"
