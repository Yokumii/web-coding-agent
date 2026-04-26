import json
import tempfile
from pathlib import Path

from src.orchestration.file_comm import FileComm


def test_write_and_read_spec():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        comm.write_spec("# My Spec\nHello")
        assert comm.read_spec() == "# My Spec\nHello"


def test_feedback_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        comm.write_feedback(1, "Fix the login button")
        assert comm.read_feedback(1) == "Fix the login button"
        assert comm.read_feedback(2) == ""


def test_grades_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        grades = {"round": 1, "overall_passed": True, "criteria": {}}
        comm.write_grades(1, grades)
        result = comm.read_grades(1)
        assert result["round"] == 1
        assert result["overall_passed"] is True


def test_missing_files():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        assert comm.read_spec() == ""
        assert comm.read_design_tokens() is None
        assert comm.read_feature_list() is None
        assert comm.read_sprint_plan() is None
        assert comm.read_ui_verification_plan() is None
        assert comm.read_accepted_sprints() is None
        assert comm.read_progress() == ""
        assert comm.read_grades(99) is None
        assert comm.read_state() is None


def test_state_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        state = {"round": 2, "status": "building"}
        comm.write_state(state)
        result = comm.read_state()
        assert result["round"] == 2


def test_planning_artifact_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        design_tokens = {"theme_name": "editorial"}
        feature_list = {"features": [{"id": "F001", "name": "Hero"}]}
        sprint_plan = {"total_sprints": 1, "sprints": [{"number": 1}]}
        verification_plan = {"sprints": [{"sprint": 1, "checks": []}]}
        accepted_sprints = {"accepted": [], "current_target": 1}

        comm.write_design_tokens(design_tokens)
        comm.write_feature_list(feature_list)
        comm.write_sprint_plan(sprint_plan)
        comm.write_ui_verification_plan(verification_plan)
        comm.write_accepted_sprints(accepted_sprints)

        assert comm.read_design_tokens() == design_tokens
        assert comm.read_feature_list() == feature_list
        assert comm.read_sprint_plan() == sprint_plan
        assert comm.read_ui_verification_plan() == verification_plan
        assert comm.read_accepted_sprints() == accepted_sprints


def test_progress_round_trip_and_append():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        comm.write_progress("# Progress Log")
        comm.append_progress_entry("## 2026-04-27T10:30:00Z\n- Phase: planning")
        comm.append_progress_entry("## 2026-04-27T10:45:00Z\n- Phase: build")

        progress = comm.read_progress()
        assert "# Progress Log" in progress
        assert "Phase: planning" in progress
        assert "Phase: build" in progress
        assert progress.count("## 2026-04-27T") == 2


def test_reset_run_artifacts_clears_new_planning_files():
    with tempfile.TemporaryDirectory() as tmp:
        harness_dir = Path(tmp) / ".harness"
        comm = FileComm(harness_dir)
        comm.write_spec("# Spec")
        comm.write_design_tokens({"theme_name": "editorial"})
        comm.write_feature_list({"features": []})
        comm.write_sprint_plan({"total_sprints": 1, "sprints": []})
        comm.write_ui_verification_plan({"sprints": []})
        comm.write_accepted_sprints({"accepted": [], "current_target": 1})
        comm.write_progress("# Progress")
        comm.write_build_log("build")
        comm.write_state({"round": 1})
        comm.write_feedback(1, "feedback")
        comm.write_grades(1, {"round": 1})
        logs_dir = harness_dir / "logs"
        traces_dir = harness_dir / "traces"
        logs_dir.mkdir()
        traces_dir.mkdir()
        (logs_dir / "frontend.log").write_text("log")
        (traces_dir / "planner.jsonl").write_text("trace")

        comm.reset_run_artifacts()

        assert comm.read_spec() == ""
        assert comm.read_design_tokens() is None
        assert comm.read_feature_list() is None
        assert comm.read_sprint_plan() is None
        assert comm.read_ui_verification_plan() is None
        assert comm.read_accepted_sprints() is None
        assert comm.read_progress() == ""
        assert comm.read_build_log() == ""
        assert comm.read_state() is None
        assert comm.read_feedback(1) == ""
        assert comm.read_grades(1) is None
        assert not logs_dir.exists()
        assert not traces_dir.exists()
