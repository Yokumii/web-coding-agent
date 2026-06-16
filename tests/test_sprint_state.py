from pathlib import Path

import pytest

from src.orchestration.file_comm import FileComm
from src.orchestration.sprint_state import SprintState


@pytest.fixture
def file_comm(tmp_path):
    fc = FileComm(tmp_path / ".harness")
    fc.write_sprint_plan({
        "total_sprints": 3,
        "sprints": [
            {"number": 1, "title": "S1", "goal": "g1", "deliverables": ["d1"],
             "feature_ids": ["F-001"], "exit_criteria": ["e1"]},
            {"number": 2, "title": "S2", "goal": "g2", "deliverables": ["d2"],
             "feature_ids": ["F-002"], "exit_criteria": ["e2"]},
            {"number": 3, "title": "S3", "goal": "g3", "deliverables": ["d3"],
             "feature_ids": ["F-003"], "exit_criteria": ["e3"]},
        ],
    })
    fc.write_feature_list({
        "features": [
            {"id": "F-001", "name": "F1", "description": "", "priority": "P0",
             "depends_on": [], "acceptance_criteria": ["e1"], "status": "planned", "sprint": 1},
            {"id": "F-002", "name": "F2", "description": "", "priority": "P0",
             "depends_on": [], "acceptance_criteria": ["e2"], "status": "planned", "sprint": 2},
            {"id": "F-003", "name": "F3", "description": "", "priority": "P0",
             "depends_on": [], "acceptance_criteria": ["e3"], "status": "planned", "sprint": 3},
        ]
    })
    fc.write_ui_verification_plan({
        "sprints": [
            {
                "sprint": 2,
                "checks": [
                    {
                        "id": "UI-002",
                        "feature_id": "F-002",
                        "task": "Check sprint 2.",
                        "expected_result": "Sprint 2 works.",
                        "critical": True,
                        "category": "core_interaction",
                    }
                ],
            }
        ]
    })
    fc.write_accepted_sprints({"accepted": [], "current_target": 1, "last_evaluated_round": 0})
    return fc


def test_load_initializes_from_disk(file_comm):
    state = SprintState.load(file_comm)
    assert state.current_target == 1
    assert state.total_sprints == 3
    assert state.accepted == []
    assert state.last_evaluated_round == 0


def test_load_falls_back_when_files_missing(tmp_path):
    fc = FileComm(tmp_path / ".harness")  # nothing written
    state = SprintState.load(fc)
    assert state.current_target == 1
    assert state.accepted == []
    assert state.last_evaluated_round == 0
    assert state.total_sprints == 0


def test_advance_to_next_sprint(file_comm):
    state = SprintState.load(file_comm)
    state.advance(sprint_num=1, round_num=1, recommendation="generate_next_sprint")
    assert state.current_target == 2
    assert state.accepted == [1]
    # Persisted to disk:
    on_disk = file_comm.read_accepted_sprints()
    assert on_disk["current_target"] == 2
    assert on_disk["accepted"] == [1]
    assert on_disk["last_evaluated_round"] == 1


def test_advance_repair_keeps_sprint(file_comm):
    state = SprintState.load(file_comm)
    state.advance(sprint_num=1, round_num=1, recommendation="repair")
    assert state.current_target == 1
    assert state.accepted == []


def test_advance_complete_marks_accepted(file_comm):
    state = SprintState.load(file_comm)
    state.advance(sprint_num=3, round_num=5, recommendation="complete")
    assert state.current_target == 4  # advances past total
    assert state.accepted == [3]
    assert state.last_evaluated_round == 5


def test_advance_idempotent_on_already_accepted(file_comm):
    state = SprintState.load(file_comm)
    state.advance(sprint_num=1, round_num=1, recommendation="generate_next_sprint")
    state.advance(sprint_num=1, round_num=2, recommendation="generate_next_sprint")
    assert state.accepted == [1]


def test_compute_advance_does_not_write(file_comm):
    state = SprintState.load(file_comm)
    payload = state.compute_advance(sprint_num=1, round_num=1, recommendation="generate_next_sprint")
    assert payload == {"accepted": [1], "current_target": 2, "last_evaluated_round": 1}
    # In-memory state unchanged
    assert state.current_target == 1
    assert state.accepted == []
    # Disk unchanged
    on_disk = file_comm.read_accepted_sprints()
    assert on_disk["current_target"] == 1
    assert on_disk["accepted"] == []


def test_sprint_context(file_comm):
    state = SprintState.load(file_comm)
    ctx = state.sprint_context(2)
    assert ctx["title"] == "S2"
    assert ctx["feature_ids"] == ["F-002"]


def test_sprint_context_missing_returns_empty(file_comm):
    state = SprintState.load(file_comm)
    assert state.sprint_context(99) == {}


def test_feature_ids_for_sprint(file_comm):
    state = SprintState.load(file_comm)
    assert state.feature_ids_for_sprint(2) == {"F-002"}
    assert state.feature_ids_for_sprint(99) == set()


def test_sprint_run_context_gathers_sprint_artifacts(file_comm):
    state = SprintState.load(file_comm)
    ctx = state.sprint_run_context(2)

    assert ctx.sprint_num == 2
    assert ctx.sprint_context["title"] == "S2"
    assert ctx.accepted_sprints == {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 0,
    }
    assert [feature["id"] for feature in ctx.features] == ["F-002"]
    assert [check["id"] for check in ctx.ui_checks] == ["UI-002"]
    assert ctx.exit_criterion_map == [
        {
            "criterion_id": "EXIT-02-01",
            "feature_id": "F-002",
            "criterion": "e2",
            "critical": True,
        }
    ]


def test_current_sprint_run_context_uses_current_target(file_comm):
    file_comm.write_accepted_sprints(
        {"accepted": [1], "current_target": 2, "last_evaluated_round": 1}
    )
    state = SprintState.load(file_comm)

    ctx = state.current_run_context()

    assert ctx.sprint_num == 2
    assert ctx.sprint_context["title"] == "S2"


def test_required_sprint_run_context_rejects_missing_sprint(file_comm):
    state = SprintState.load(file_comm)

    with pytest.raises(RuntimeError, match="Generator could not find sprint 99"):
        state.required_run_context(99, owner="Generator")


def test_required_sprint_run_context_rejects_missing_accepted_sprints(tmp_path):
    fc = FileComm(tmp_path / ".harness")
    fc.write_sprint_plan({
        "total_sprints": 1,
        "sprints": [
            {"number": 1, "title": "S1", "goal": "g1", "deliverables": ["d1"],
             "feature_ids": ["F-001"], "exit_criteria": ["e1"]},
        ],
    })
    state = SprintState.load(fc)

    with pytest.raises(RuntimeError, match="Generator requires .harness/accepted_sprints.json"):
        state.required_run_context(1, owner="Generator")


def test_mark_sprint_in_progress(file_comm):
    state = SprintState.load(file_comm)
    state.mark_sprint_in_progress(1)
    fl = file_comm.read_feature_list()
    statuses = {f["id"]: f["status"] for f in fl["features"]}
    assert statuses["F-001"] == "in_progress"
    assert statuses["F-002"] == "planned"


def test_mark_sprint_outcome_accepted(file_comm):
    state = SprintState.load(file_comm)
    grades = {"overall_passed": True, "ui_checks": [], "target_exit_criteria_results": []}
    state.mark_sprint_outcome(1, recommendation="generate_next_sprint", grades=grades)
    fl = file_comm.read_feature_list()
    statuses = {f["id"]: f["status"] for f in fl["features"]}
    assert statuses["F-001"] == "accepted"


def test_mark_sprint_outcome_repair_required_when_check_fails(file_comm):
    state = SprintState.load(file_comm)
    grades = {
        "overall_passed": False,
        "ui_checks": [{"feature_id": "F-001", "status": "fail", "notes": "broken"}],
        "target_exit_criteria_results": [],
    }
    state.mark_sprint_outcome(1, recommendation="repair", grades=grades)
    fl = file_comm.read_feature_list()
    statuses = {f["id"]: f["status"] for f in fl["features"]}
    assert statuses["F-001"] == "repair_required"


def test_mark_sprint_outcome_implemented_when_no_specific_failure(file_comm):
    state = SprintState.load(file_comm)
    grades = {
        "overall_passed": True,
        "ui_checks": [{"feature_id": "F-001", "status": "pass", "notes": ""}],
        "target_exit_criteria_results": [],
    }
    state.mark_sprint_outcome(1, recommendation="repair", grades=grades)
    fl = file_comm.read_feature_list()
    statuses = {f["id"]: f["status"] for f in fl["features"]}
    # recommendation=repair but no specific failure → "implemented"
    assert statuses["F-001"] == "implemented"


def test_overall_failure_marks_all_sprint_features_as_failing(file_comm):
    state = SprintState.load(file_comm)
    grades = {
        "overall_passed": False,
        "ui_checks": [],  # no specific feature blamed
        "target_exit_criteria_results": [],
    }
    state.mark_sprint_outcome(1, recommendation="repair", grades=grades)
    fl = file_comm.read_feature_list()
    statuses = {f["id"]: f["status"] for f in fl["features"]}
    assert statuses["F-001"] == "repair_required"  # overall_passed=False, so all sprint features
