from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from src.orchestration.file_comm import FileComm
from src.orchestration.sprint_state import SprintState
from src.prompts.grading import determine_passed
from src.utils.logger import get_logger

logger = get_logger(__name__)


class ResumeError(RuntimeError):
    """恢复执行时，检查点状态无法与当前目录协调时抛出。"""


def _copy_phase_metrics(
    metrics: Mapping[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    return {
        name: dict(payload)
        for name, payload in metrics.items()
        if isinstance(payload, dict)
    }


def _last_verdict_for_recommendation(recommendation: str) -> str:
    if recommendation == "complete":
        return "completed"
    if recommendation == "generate_next_sprint":
        return "accepted_review"
    return "failed_review"


def _generator_mode_for_recommendation(recommendation: str) -> str:
    return "repair" if recommendation == "repair" else "generate"


@dataclass
class CheckpointTransaction:
    file_comm: FileComm
    prompt: str
    costs: Mapping[str, float]
    phase_metrics: Mapping[str, dict[str, Any]]

    def record_plan_completed(self) -> None:
        self._write_checkpoint("plan", 0, last_verdict="planned")

    def record_design_completed(self, metadata: dict[str, Any]) -> None:
        self._write_checkpoint(
            "design",
            0,
            last_verdict=metadata.get("design_status"),
            design_metadata=metadata,
        )

    def record_build_completed(
        self,
        *,
        round_num: int,
        current_sprint: int,
        generator_mode: str,
    ) -> None:
        self._write_checkpoint(
            f"build_r{round_num}",
            round_num,
            current_sprint=current_sprint,
            generator_mode=generator_mode,
            last_verdict="awaiting_review",
        )

    def record_evaluate_completed(
        self,
        *,
        sprint_state: SprintState,
        round_num: int,
        sprint_num: int,
        recommendation: str,
    ) -> None:
        accepted_sprints_payload = sprint_state.compute_advance(
            sprint_num=sprint_num,
            round_num=round_num,
            recommendation=recommendation,
        )
        self._write_checkpoint(
            f"evaluate_r{round_num}",
            round_num,
            current_sprint=sprint_num,
            generator_mode=_generator_mode_for_recommendation(recommendation),
            last_verdict=_last_verdict_for_recommendation(recommendation),
            accepted_sprints_payload=accepted_sprints_payload,
        )
        sprint_state.advance(
            sprint_num=sprint_num,
            round_num=round_num,
            recommendation=recommendation,
        )

    def _write_checkpoint(
        self,
        phase: str,
        round_num: int,
        *,
        current_sprint: int | None = None,
        generator_mode: str | None = None,
        last_verdict: str | None = None,
        accepted_sprints_payload: dict[str, Any] | None = None,
        design_metadata: dict[str, Any] | None = None,
    ) -> None:
        if accepted_sprints_payload is None:
            accepted_sprints_payload = self.file_comm.read_accepted_sprints() or {}
        state = {
            "last_completed_phase": phase,
            "round_num": round_num,
            "prompt": self.prompt,
            "costs": dict(self.costs),
            "phase_metrics": _copy_phase_metrics(self.phase_metrics),
            "current_sprint": current_sprint,
            "generator_mode": generator_mode,
            "accepted_sprints": accepted_sprints_payload.get("accepted", []),
            "accepted_sprints_payload": accepted_sprints_payload,
            "last_verdict": last_verdict,
            "timestamp": datetime.now().isoformat(),
        }
        if design_metadata:
            state.update(design_metadata)
        self.file_comm.write_state(state)
        logger.debug(f"Checkpoint saved: {phase} (round {round_num})")


def restore_resume_state(file_comm: FileComm, state: dict[str, Any]) -> None:
    """用检查点中的 accepted_sprints 内容恢复当前目录状态。"""
    payload = state.get("accepted_sprints_payload")
    if not isinstance(payload, dict) or "accepted" not in payload:
        raise ResumeError(
            "harness_state.json was written by an older version of this tool; "
            "delete the workdir's .harness/ directory and start a fresh run."
        )
    if file_comm.read_accepted_sprints() != payload:
        file_comm.write_accepted_sprints(payload)


def reconcile_completed_evaluation(file_comm: FileComm, state: dict[str, Any]) -> dict[str, Any]:
    """Reopen a checkpoint that an older verdict normalizer accepted incorrectly.

    This recovery path is deliberately narrow: it only corrects an accepted
    evaluation checkpoint when the persisted grade itself fails the current
    acceptance predicate (for example, a failed DOM-scope regression audit).
    """
    phase = str(state.get("last_completed_phase", ""))
    accepted_verdicts = {"accepted_review", "completed"}
    if not phase.startswith("evaluate_r") or state.get("last_verdict") not in accepted_verdicts:
        return state
    try:
        round_num = int(state["round_num"])
        sprint_num = int(state["current_sprint"])
    except (KeyError, TypeError, ValueError):
        return state
    grades = file_comm.read_grades(round_num)
    # Legacy lightweight checkpoints may legitimately omit the grade artifact;
    # only reopen when persisted grade evidence positively exists.
    if grades is None:
        return state
    if determine_passed(grades):
        return state

    previous_payload = state.get("accepted_sprints_payload")
    if not isinstance(previous_payload, dict):
        return state
    reconciled = dict(state)
    accepted = [
        int(item) for item in previous_payload.get("accepted", [])
        if isinstance(item, int) and item < sprint_num
    ]
    payload = {
        "accepted": accepted,
        "current_target": sprint_num,
        "last_evaluated_round": round_num,
    }
    reconciled["accepted_sprints"] = accepted
    reconciled["accepted_sprints_payload"] = payload
    reconciled["current_sprint"] = sprint_num
    reconciled["generator_mode"] = "repair"
    reconciled["last_verdict"] = "failed_review"
    file_comm.write_accepted_sprints(payload)
    file_comm.write_state(reconciled)
    logger.warning(
        "Reopened evaluation round %s for repair because persisted grades fail acceptance.",
        round_num,
    )
    return reconciled
