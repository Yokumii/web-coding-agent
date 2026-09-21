"""Freeze Edit tests before the candidate implementation exists.

The existing plan/execution views and hidden checks remain authoritative. This
module stores only their compact fingerprints in the existing harness
checkpoint, avoiding another validation artifact and another test-body copy.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from src.orchestration.edit_task_contract import read_edit_task_contract
from src.orchestration.file_comm import FileComm
from src.orchestration.hidden_oracle_checks import read_hidden_oracle_checks
from src.orchestration.edit_risk_tests import read_edit_risk_tests


SCHEMA_VERSION = "edit-freeze-v1"


class PreimplementationValidationError(RuntimeError):
    """The target-blind test contract is missing, stale, or was authored too late."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _git(frontend: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=frontend, text=True, capture_output=True
    )
    if result.returncode != 0:
        raise PreimplementationValidationError(
            f"git {' '.join(args)} failed while freezing Edit tests: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def _test_plan(file_comm: FileComm) -> list[dict[str, Any]]:
    verification = file_comm.read_ui_verification_plan() or {}
    sprints = verification.get("sprints")
    if not isinstance(sprints, list) or not sprints:
        raise PreimplementationValidationError(
            "Edit validation requires planner-authored executable checks before Build."
        )
    frozen: list[dict[str, Any]] = []
    for sprint in sprints:
        if not isinstance(sprint, dict) or not isinstance(sprint.get("sprint"), int):
            raise PreimplementationValidationError(
                "ui_verification_plan.json contains an invalid Sprint test block."
            )
        checks = sprint.get("checks")
        if not isinstance(checks, list) or not checks:
            raise PreimplementationValidationError(
                f"Sprint {sprint['sprint']} has no executable pre-implementation checks."
            )
        frozen.append({"sprint": sprint["sprint"], "checks": checks})
    return frozen


def freeze_preimplementation_validation(
    *, workdir: Path, file_comm: FileComm, instruction_delta: str
) -> dict[str, Any]:
    """Freeze compact test fingerprints while HEAD is the accepted source."""
    contract = read_edit_task_contract(workdir)
    if contract is None:
        raise PreimplementationValidationError(
            "pre-implementation validation currently requires an explicit Edit task contract"
        )
    baseline = str(contract.get("baseline_commit") or "").strip()
    frontend = Path(workdir) / "frontend"
    if not baseline or not (frontend / ".git").is_dir():
        raise PreimplementationValidationError(
            "Edit validation cannot bind tests to an accepted source commit."
        )
    head = _git(frontend, "rev-parse", "HEAD")
    if head != baseline:
        raise PreimplementationValidationError(
            "Tests must be frozen before target implementation: frontend HEAD no longer "
            "matches the accepted Edit baseline."
        )
    if _git(frontend, "status", "--porcelain"):
        raise PreimplementationValidationError(
            "Tests must be frozen before target implementation: the accepted source "
            "worktree already contains changes."
        )

    test_plan = _test_plan(file_comm)
    hidden_oracle_checks = read_hidden_oracle_checks(file_comm.dir)
    edit_risk_tests = read_edit_risk_tests(file_comm.dir)
    visible_ids = {
        str(check.get("id") or "")
        for sprint in test_plan
        for check in sprint["checks"]
        if isinstance(check, dict)
    }
    hidden_ids = {
        str(check.get("id") or "")
        for check in hidden_oracle_checks
        if isinstance(check, dict)
    }
    collisions = sorted((visible_ids & hidden_ids) - {""})
    if collisions:
        raise PreimplementationValidationError(
            "hidden oracle ids collide with visible checks: " + ", ".join(collisions)
        )
    atomic_plan_path = file_comm.dir / "atomic_edit_plan.json"
    if not atomic_plan_path.is_file():
        raise PreimplementationValidationError(
            "atomic_edit_plan.json is required before Edit tests can be frozen."
        )
    atomic_plan = json.loads(atomic_plan_path.read_text(encoding="utf-8"))
    fingerprint = {
        "schema_version": SCHEMA_VERSION,
        "instruction_sha256": _sha256(instruction_delta),
        "atomic_plan_sha256": _sha256(atomic_plan),
        "test_plan_sha256": _sha256(test_plan),
        "hidden_oracle_sha256": _sha256(hidden_oracle_checks),
        "edit_risk_tests_sha256": _sha256(edit_risk_tests),
    }
    state = file_comm.read_state() or {}
    state["edit_freeze"] = fingerprint
    file_comm.write_state(state)
    return fingerprint


def verify_preimplementation_validation(
    *, workdir: Path, file_comm: FileComm, instruction_delta: str
) -> dict[str, Any]:
    """Verify current canonical tests against the checkpointed fingerprints."""
    state = file_comm.read_state() or {}
    payload = state.get("edit_freeze")
    if not isinstance(payload, dict):
        raise PreimplementationValidationError(
            "Missing Edit test freeze in harness_state.json; refusing an Edit whose "
            "tests cannot be proven to predate the target implementation."
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise PreimplementationValidationError(
            "pre-implementation validation provenance is invalid"
        )

    contract = read_edit_task_contract(workdir) or {}
    baseline = str(contract.get("baseline_commit") or "")
    frontend = Path(workdir) / "frontend"
    if not baseline or not (frontend / ".git").is_dir():
        raise PreimplementationValidationError(
            "frozen tests no longer match an accepted source contract"
        )
    if payload.get("instruction_sha256") != _sha256(instruction_delta):
        raise PreimplementationValidationError(
            "frozen tests were authored for a different Edit instruction"
        )

    test_plan = _test_plan(file_comm)
    if _sha256(test_plan) != payload.get("test_plan_sha256"):
        raise PreimplementationValidationError(
            "ui_verification_plan.json changed after tests were frozen"
        )
    hidden_oracle_checks = read_hidden_oracle_checks(file_comm.dir)
    if _sha256(hidden_oracle_checks) != payload.get("hidden_oracle_sha256"):
        raise PreimplementationValidationError(
            "hidden_oracle_checks.json changed after tests were frozen"
        )
    edit_risk_tests = read_edit_risk_tests(file_comm.dir)
    if _sha256(edit_risk_tests) != payload.get("edit_risk_tests_sha256"):
        raise PreimplementationValidationError(
            "edit_risk_tests.json changed after tests were frozen"
        )
    atomic_plan_path = file_comm.dir / "atomic_edit_plan.json"
    if not atomic_plan_path.is_file():
        raise PreimplementationValidationError("atomic Edit plan disappeared after freeze")
    atomic_plan = json.loads(atomic_plan_path.read_text(encoding="utf-8"))
    if payload.get("atomic_plan_sha256") != _sha256(atomic_plan):
        raise PreimplementationValidationError(
            "atomic_edit_plan.json changed after tests were frozen"
        )
    return {
        **payload,
        "test_plan": test_plan,
        "hidden_oracle_checks": hidden_oracle_checks,
    }


def frozen_checks_for_sprint(
    payload: dict[str, Any], sprint_num: int
) -> list[dict[str, Any]]:
    """Read the authoritative pre-target checks for one Edit Sprint."""
    for sprint in payload.get("test_plan") or []:
        if isinstance(sprint, dict) and sprint.get("sprint") == sprint_num:
            return [
                check for check in sprint.get("checks") or [] if isinstance(check, dict)
            ]
    return []


def frozen_hidden_oracle_checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Read Harness-owned hidden checks from the same pre-target freeze."""
    return [
        check
        for check in payload.get("hidden_oracle_checks") or []
        if isinstance(check, dict)
    ]


__all__ = [
    "PreimplementationValidationError",
    "freeze_preimplementation_validation",
    "frozen_checks_for_sprint",
    "frozen_hidden_oracle_checks",
    "verify_preimplementation_validation",
]
