"""Append-only browser contracts captured at accepted Edit checkpoints."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from src.orchestration.ui_action_contracts import TYPED_ASSERTION_ACTIONS


TAPE_NAME = "accepted_tapes.jsonl"
MAX_REPLAY_CHECKS = 32


class AcceptedTapeError(RuntimeError):
    """Accepted behavior evidence is missing, malformed, or too broad to replay."""


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AcceptedTapeError(
                f"accepted tape line {line_number} is invalid JSON: {exc}"
            ) from exc
        if not isinstance(record, dict) or record.get("schema_version") != "accepted-tape-v1":
            raise AcceptedTapeError(
                f"accepted tape line {line_number} is not accepted-tape-v1"
            )
        records.append(record)
    return records


def _validate_checks(checks: list[dict[str, Any]]) -> None:
    for check in checks:
        actions = check.get("actions") if isinstance(check, dict) else None
        if not isinstance(actions, list) or not actions:
            raise AcceptedTapeError("accepted tape checks require executable actions")
        typed = [
            action
            for action in actions
            if isinstance(action, dict)
            and action.get("action") in TYPED_ASSERTION_ACTIONS
        ]
        if not 1 <= len(typed) <= 4 or actions[-1] not in typed:
            raise AcceptedTapeError(
                "accepted tape checks require 1 to 4 related typed assertions and a final typed assertion"
            )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptedTapeError(f"cannot read accepted-checkpoint evidence {path}: {exc}") from exc


def append_accepted_tape(
    *,
    harness_dir: Path,
    sprint_num: int,
    round_num: int,
    checks: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> Path:
    """Durably append one accepted checkpoint, idempotently across resume."""
    _validate_checks(checks)
    observed = evidence.get("checks") if isinstance(evidence, dict) else None
    if not isinstance(observed, list) or len(observed) != len(checks) or any(
        not isinstance(item, dict) or item.get("status") != "ok" for item in observed
    ):
        raise AcceptedTapeError("only fully passing browser evidence may become an accepted tape")
    path = harness_dir / TAPE_NAME
    if any(
        item.get("sprint") == sprint_num and item.get("round") == round_num
        for item in _records(path)
    ):
        return path
    record = {
        "schema_version": "accepted-tape-v1",
        "status": "ok",
        "sprint": sprint_num,
        "round": round_num,
        "checks": checks,
        "evidence_ref": f".harness/browser_evidence_round_{round_num}.json",
    }
    harness_dir.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path


def accepted_replay_checks(
    harness_dir: Path, *, before_sprint: int | None = None
) -> list[dict[str, Any]]:
    """Return all accepted contracts in lineage order, failing closed on overflow."""
    records = sorted(_records(harness_dir / TAPE_NAME), key=lambda item: int(item["sprint"]))
    checks = [
        check
        for record in records
        if before_sprint is None or int(record["sprint"]) < before_sprint
        for check in record.get("checks", [])
        if isinstance(check, dict)
    ]
    _validate_checks(checks)
    if len(checks) > MAX_REPLAY_CHECKS:
        raise AcceptedTapeError(
            f"accepted tape has {len(checks)} checks; maximum replay budget is {MAX_REPLAY_CHECKS}"
        )
    return checks


def select_accepted_replay_checks(
    harness_dir: Path,
    *,
    edit_card: dict[str, Any] | None,
    accepted_edit_index: int,
    full_replay_interval: int = 5,
    before_round: int | None = None,
) -> dict[str, Any]:
    """Select historical obligations by impact and keep one route sentinel.

    Legacy tapes without requirement/impact metadata are replayed in full. A
    periodic full sweep prevents a narrow impact map from hiding long-range
    regressions. Explicitly retired requirements are retained in lineage but no
    longer gate the current accepted state.
    """
    records = sorted(
        (
            record
            for record in _records(Path(harness_dir) / TAPE_NAME)
            if before_round is None or int(record["round"]) < before_round
        ),
        key=lambda item: (int(item["sprint"]), int(item["round"])),
    )
    all_checks = [
        check
        for record in records
        for check in record.get("checks", [])
        if isinstance(check, dict)
    ]
    _validate_checks(all_checks)
    card = edit_card or {}
    retired = {str(item) for item in card.get("retired_requirement_ids") or []}
    target_routes = {str(item) for item in card.get("target_routes") or []}
    impact_tags = {str(item) for item in card.get("impact_tags") or []}
    interval = max(1, int(full_replay_interval))
    periodic_full = accepted_edit_index > 0 and accepted_edit_index % interval == 0
    legacy_full = any(
        not isinstance(check.get("requirement_id"), str)
        or not isinstance(check.get("impact_tags"), list)
        for check in all_checks
    )

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    selected_reasons: dict[str, str] = {}
    skipped_reasons: dict[str, str] = {}

    def include(check: dict[str, Any], reason: str) -> None:
        check_id = str(check.get("id", ""))
        if check_id and check_id not in selected_ids:
            selected.append(check)
            selected_ids.add(check_id)
            selected_reasons[check_id] = reason

    active: list[dict[str, Any]] = []
    for check in all_checks:
        check_id = str(check.get("id", ""))
        requirement_id = str(check.get("requirement_id", ""))
        if requirement_id and requirement_id in retired:
            skipped_reasons[check_id] = "requirement_retired"
            continue
        active.append(check)

    if periodic_full or legacy_full:
        mode = "periodic_full" if periodic_full else "legacy_full"
        for check in active:
            include(check, mode)
    else:
        mode = "impact_scoped"
        for check in active:
            check_tags = {str(item) for item in check.get("impact_tags") or []}
            if str(check.get("route", "/")) in target_routes:
                include(check, "target_route")
            elif impact_tags & check_tags:
                include(check, "impact_tag")

        # One critical historical behavior per non-target route remains a cheap
        # sentinel even when the impact map predicts no dependency.
        sentinel_routes: set[str] = set()
        for check in active:
            route = str(check.get("route", "/"))
            if (
                route not in target_routes
                and route not in sentinel_routes
                and bool(check.get("critical", False))
            ):
                include(check, "protected_route_sentinel")
                sentinel_routes.add(route)

        for check in active:
            check_id = str(check.get("id", ""))
            if check_id not in selected_ids:
                skipped_reasons.setdefault(check_id, "outside_impact_slice")

    if len(selected) > MAX_REPLAY_CHECKS:
        raise AcceptedTapeError(
            f"selected accepted tape has {len(selected)} checks; maximum replay budget is {MAX_REPLAY_CHECKS}"
        )
    return {
        "schema_version": "regression-selection-v1",
        "mode": mode,
        "accepted_edit_index": accepted_edit_index,
        "full_replay_interval": interval,
        "before_round": before_round,
        "checks": selected,
        "selected_check_ids": [str(item.get("id", "")) for item in selected],
        "selected_reasons": selected_reasons,
        "skipped_reasons": skipped_reasons,
    }


def accepted_obligation_summary(harness_dir: Path) -> list[dict[str, Any]]:
    """Return bounded semantic metadata for the Planner, never full trajectories."""
    records = sorted(
        _records(Path(harness_dir) / TAPE_NAME), key=lambda item: int(item["sprint"])
    )
    summary: list[dict[str, Any]] = []
    for record in records:
        for check in record.get("checks") or []:
            if not isinstance(check, dict):
                continue
            summary.append({
                "check_id": str(check.get("id", "")),
                "requirement_id": str(check.get("requirement_id", "")),
                "route": str(check.get("route", "/")),
                "impact_tags": [str(item) for item in check.get("impact_tags") or []],
                "task": str(check.get("task", ""))[:300],
            })
    if len(summary) > MAX_REPLAY_CHECKS:
        return summary[-MAX_REPLAY_CHECKS:]
    return summary


def recover_missing_accepted_tapes(run_dir: Path) -> dict[str, list[dict[str, int]]]:
    """Append tapes missing because of an older recorder bug, from immutable evidence.

    This is deliberately narrower than reconstructing a trajectory from git. A
    checkpoint is recoverable only when the persisted sprint state marks it
    accepted, its terminal grade is fully passing, and its same-round browser
    action evidence is complete. Existing result files are never rewritten.
    """
    harness_dir = run_dir / ".harness"
    state = _read_json(harness_dir / "accepted_sprints.json")
    accepted_raw = state.get("accepted") if isinstance(state, dict) else None
    if not isinstance(accepted_raw, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in accepted_raw
    ):
        raise AcceptedTapeError("accepted_sprints.json must contain an integer accepted list")
    accepted = set(accepted_raw)

    plan = _read_json(harness_dir / "ui_verification_plan.json")
    sprint_rows = plan.get("sprints") if isinstance(plan, dict) else None
    if not isinstance(sprint_rows, list):
        raise AcceptedTapeError("ui_verification_plan.json must contain sprints")
    checks_by_sprint: dict[int, list[dict[str, Any]]] = {}
    for row in sprint_rows:
        sprint = row.get("sprint") if isinstance(row, dict) else None
        checks = row.get("checks") if isinstance(row, dict) else None
        if isinstance(sprint, int) and isinstance(checks, list):
            checks_by_sprint[sprint] = checks

    terminal: dict[int, tuple[int, dict[str, Any]]] = {}
    for grade_path in harness_dir.glob("grade_round_*.json"):
        try:
            round_num = int(grade_path.stem.rsplit("_", 1)[-1])
        except ValueError:
            continue
        grade = _read_json(grade_path)
        sprint = grade.get("sprint") if isinstance(grade, dict) else None
        if (
            not isinstance(sprint, int)
            or sprint not in accepted
            or grade.get("overall_passed") is not True
            or grade.get("sprint_passed") is not True
            or grade.get("regression_passed") is not True
        ):
            continue
        prior = terminal.get(sprint)
        if prior is None or round_num > prior[0]:
            terminal[sprint] = (round_num, grade)

    existing = {
        (int(record.get("sprint", -1)), int(record.get("round", -1)))
        for record in _records(harness_dir / TAPE_NAME)
    }
    result: dict[str, list[dict[str, int]]] = {
        "recovered": [],
        "already_present": [],
    }
    for sprint in sorted(accepted):
        if sprint not in terminal:
            raise AcceptedTapeError(
                f"accepted sprint {sprint} has no terminal fully passing grade"
            )
        round_num, _grade = terminal[sprint]
        checks = checks_by_sprint.get(sprint)
        if not checks:
            raise AcceptedTapeError(f"accepted sprint {sprint} has no browser contracts")
        _validate_checks(checks)
        evidence = _read_json(harness_dir / f"browser_evidence_round_{round_num}.json")
        key = (sprint, round_num)
        append_accepted_tape(
            harness_dir=harness_dir,
            sprint_num=sprint,
            round_num=round_num,
            checks=checks,
            evidence=evidence,
        )
        bucket = "already_present" if key in existing else "recovered"
        result[bucket].append({"sprint": sprint, "round": round_num})
    return result


__all__ = [
    "AcceptedTapeError",
    "MAX_REPLAY_CHECKS",
    "TAPE_NAME",
    "accepted_replay_checks",
    "accepted_obligation_summary",
    "append_accepted_tape",
    "recover_missing_accepted_tapes",
    "select_accepted_replay_checks",
]
