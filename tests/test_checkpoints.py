from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.orchestration.checkpoints import (
    CheckpointTransaction,
    ResumeError,
    reconcile_completed_evaluation,
    restore_resume_state,
)
from src.orchestration.file_comm import FileComm
from src.orchestration.sprint_state import SprintState


class TrackingFileComm(FileComm):
    def __init__(self, harness_dir: Path) -> None:
        super().__init__(harness_dir)
        self.writes: list[str] = []

    def write_state(self, state: dict[str, Any]) -> Path:
        self.writes.append("state")
        return super().write_state(state)

    def write_accepted_sprints(self, accepted_sprints: dict[str, Any]) -> Path:
        self.writes.append("accepted_sprints")
        return super().write_accepted_sprints(accepted_sprints)


def _write_sprint_plan(file_comm: FileComm) -> None:
    file_comm.write_sprint_plan(
        {
            "total_sprints": 2,
            "sprints": [
                {
                    "number": 1,
                    "title": "Sprint 1",
                    "goal": "Ship sprint 1.",
                    "feature_ids": ["F001"],
                    "deliverables": ["S1 UI."],
                    "exit_criteria": ["S1 works."],
                },
                {
                    "number": 2,
                    "title": "Sprint 2",
                    "goal": "Ship sprint 2.",
                    "feature_ids": ["F002"],
                    "deliverables": ["S2 UI."],
                    "exit_criteria": ["S2 works."],
                },
            ],
        }
    )


def test_record_build_completed_captures_current_accepted_payload(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_accepted_sprints(
        {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
    )
    transaction = CheckpointTransaction(
        file_comm=file_comm,
        prompt="build a counter",
        costs={"planner": 0.1, "generator_r1": 0.2},
        phase_metrics={"planner": {"cost_usd": 0.1}},
    )

    transaction.record_build_completed(
        round_num=1,
        current_sprint=1,
        generator_mode="generate",
    )

    state = file_comm.read_state()
    assert state is not None
    assert state["last_completed_phase"] == "build_r1"
    assert state["round_num"] == 1
    assert state["prompt"] == "build a counter"
    assert state["accepted_sprints_payload"] == {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 0,
    }
    assert state["last_verdict"] == "awaiting_review"


def test_record_evaluate_completed_writes_checkpoint_before_accepted_sprints(
    tmp_path: Path,
):
    file_comm = TrackingFileComm(tmp_path / ".harness")
    _write_sprint_plan(file_comm)
    file_comm.write_accepted_sprints(
        {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
    )
    sprint_state = SprintState.load(file_comm)
    file_comm.writes.clear()
    transaction = CheckpointTransaction(
        file_comm=file_comm,
        prompt="build a counter",
        costs={"planner": 0.1, "generator_r1": 0.2, "evaluator_r1": 0.3},
        phase_metrics={},
    )

    transaction.record_evaluate_completed(
        sprint_state=sprint_state,
        round_num=1,
        sprint_num=1,
        recommendation="generate_next_sprint",
    )

    assert file_comm.writes == ["state", "accepted_sprints"]
    expected_payload = {"accepted": [1], "current_target": 2, "last_evaluated_round": 1}
    assert file_comm.read_state()["accepted_sprints_payload"] == expected_payload
    assert file_comm.read_state()["last_verdict"] == "accepted_review"
    assert file_comm.read_accepted_sprints() == expected_payload


def test_restore_resume_state_reconciles_accepted_sprints_file(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_accepted_sprints(
        {"accepted": [1], "current_target": 2, "last_evaluated_round": 1}
    )
    state = {
        "accepted_sprints_payload": {
            "accepted": [],
            "current_target": 1,
            "last_evaluated_round": 0,
        }
    }

    restore_resume_state(file_comm, state)

    assert file_comm.read_accepted_sprints() == {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 0,
    }


def test_restore_resume_state_rejects_legacy_state(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    with pytest.raises(ResumeError, match="older version"):
        restore_resume_state(file_comm, {"accepted_sprints": []})


def test_reconcile_completed_evaluation_reopens_false_accepted_scope_failure(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_accepted_sprints(
        {"accepted": [1], "current_target": 2, "last_evaluated_round": 2}
    )
    file_comm.write_grades(
        2,
        {
            "round": 2,
            "sprint": 1,
            "sprint_passed": True,
            "regression_passed": False,
            "overall_passed": False,
            "criteria": {
                name: {"score": score, "passed": True, "notes": "ok"}
                for name, score in {
                    "design_quality": 7.0,
                    "functionality": 7.0,
                    "originality": 6.0,
                    "craft": 7.0,
                }.items()
            },
        },
    )
    state = {
        "last_completed_phase": "evaluate_r2",
        "round_num": 2,
        "current_sprint": 1,
        "generator_mode": "generate",
        "last_verdict": "accepted_review",
        "accepted_sprints_payload": {
            "accepted": [1], "current_target": 2, "last_evaluated_round": 2
        },
    }

    reconciled = reconcile_completed_evaluation(file_comm, state)

    assert reconciled["last_verdict"] == "failed_review"
    assert reconciled["generator_mode"] == "repair"
    assert reconciled["accepted_sprints_payload"] == {
        "accepted": [], "current_target": 1, "last_evaluated_round": 2
    }
    assert file_comm.read_accepted_sprints() == reconciled["accepted_sprints_payload"]
    assert file_comm.read_state()["last_verdict"] == "failed_review"
