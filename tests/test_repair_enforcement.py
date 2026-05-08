"""Tests for the repair-completion enforcement hook.

The harness writes ``.harness/repair_targets_round_N.json`` from the previous
round's grade JSON. The generator must write ``.harness/repair_report_round_N.json``
addressing each target before its Stop is allowed. After ``max_block_attempts``
the hook gives up, writes ``.harness/repair_incomplete_round_N.json``, and lets
Stop through so the harness can advance to the next round.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.generator import build_repair_targets_payload
from src.agents.sdk_runner import make_repair_completion_hook
from src.orchestration.file_comm import FileComm


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _grade_with_failures() -> dict:
    return {
        "round": 3,
        "sprint": 2,
        "ui_checks": [
            {
                "check_id": "UI-007",
                "feature_id": "F002",
                "critical": True,
                "status": "partial",
                "task": "Verify positive/negative color coding on price change",
                "notes": "Current price ($187.32) renders white, not tinted.",
            },
            {
                "check_id": "UI-006",
                "feature_id": "F002",
                "critical": True,
                "status": "pass",
                "task": "Render dashboard cards.",
            },
        ],
        "target_exit_criteria_results": [
            {
                "criterion_id": "EXIT-02-04",
                "feature_id": "F002",
                "critical": True,
                "passed": False,
                "criterion": "Chart click-drag pan and scroll-wheel zoom",
                "notes": "Pan broken at frontend/src/components/PriceChart.jsx:100",
            },
            {
                "criterion_id": "EXIT-02-01",
                "feature_id": "F002",
                "critical": True,
                "passed": True,
                "criterion": "Selecting a stock populates dashboard",
            },
        ],
        "repair_instructions": [
            "Convert isPanning to a ref to fix stale closure in PriceChart.jsx.",
        ],
        "bugs_found": [
            {
                "id": "BUG-1",
                "severity": "critical",
                "summary": "Wheel zoom blocked by React 18 passive listener",
                "file": "frontend/src/components/PriceChart.jsx",
            },
        ],
    }


def test_build_repair_targets_payload_includes_failed_ui_checks_and_exit_criteria():
    payload = build_repair_targets_payload(
        grades=_grade_with_failures(),
        sprint_num=2,
        round_num=4,
    )

    assert payload["round"] == 4
    assert payload["sprint"] == 2
    target_ids = {t["id"] for t in payload["targets"]}
    # Failed UI check (partial+critical) and failed exit criterion are in.
    assert "UI-007" in target_ids
    assert "EXIT-02-04" in target_ids
    # Critical bug is in.
    assert "BUG-1" in target_ids
    # Passed items are NOT in.
    assert "UI-006" not in target_ids
    assert "EXIT-02-01" not in target_ids


def test_build_repair_targets_payload_extracts_file_hints_from_notes():
    payload = build_repair_targets_payload(
        grades=_grade_with_failures(),
        sprint_num=2,
        round_num=4,
    )
    exit_target = next(t for t in payload["targets"] if t["id"] == "EXIT-02-04")
    assert "frontend/src/components/PriceChart.jsx" in exit_target["file_hints"]


@pytest.mark.anyio
async def test_repair_hook_blocks_when_report_missing(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    targets_path = file_comm.dir / "repair_targets_round_4.json"
    report_path = file_comm.dir / "repair_report_round_4.json"
    file_comm.write_repair_targets(
        4,
        {
            "round": 4,
            "sprint": 2,
            "targets": [
                {"id": "UI-007", "kind": "ui_check", "summary": "color coding", "file_hints": []},
            ],
        },
    )

    hook = make_repair_completion_hook(
        targets_path=targets_path,
        report_path=report_path,
        max_block_attempts=3,
        file_comm=file_comm,
        round_num=4,
    )
    out = await hook({"hook_event_name": "Stop", "stop_hook_active": False}, None, {})
    assert out.get("decision") == "block"
    assert "repair_report_round_4.json" in out["reason"]


@pytest.mark.anyio
async def test_repair_hook_blocks_when_target_not_in_report(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_repair_targets(
        4,
        {
            "round": 4,
            "sprint": 2,
            "targets": [
                {"id": "UI-007", "kind": "ui_check", "summary": "color coding", "file_hints": []},
                {"id": "EXIT-02-04", "kind": "exit_criterion", "summary": "pan", "file_hints": []},
            ],
        },
    )
    file_comm.write_repair_report(
        4,
        {
            "round": 4,
            "addressed": [
                {"target_id": "UI-007", "addressed": True, "files_modified": [], "notes": "fixed"},
            ],
        },
    )

    hook = make_repair_completion_hook(
        targets_path=file_comm.dir / "repair_targets_round_4.json",
        report_path=file_comm.dir / "repair_report_round_4.json",
        max_block_attempts=3,
        file_comm=file_comm,
        round_num=4,
    )
    out = await hook({"hook_event_name": "Stop", "stop_hook_active": False}, None, {})
    assert out.get("decision") == "block"
    assert "EXIT-02-04" in out["reason"]


@pytest.mark.anyio
async def test_repair_hook_blocks_when_target_addressed_false(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_repair_targets(
        4,
        {
            "round": 4,
            "sprint": 2,
            "targets": [
                {"id": "UI-007", "kind": "ui_check", "summary": "color", "file_hints": []},
            ],
        },
    )
    file_comm.write_repair_report(
        4,
        {
            "round": 4,
            "addressed": [
                {"target_id": "UI-007", "addressed": False, "reason": "skipped"},
            ],
        },
    )

    hook = make_repair_completion_hook(
        targets_path=file_comm.dir / "repair_targets_round_4.json",
        report_path=file_comm.dir / "repair_report_round_4.json",
        max_block_attempts=3,
        file_comm=file_comm,
        round_num=4,
    )
    out = await hook({"hook_event_name": "Stop", "stop_hook_active": False}, None, {})
    assert out.get("decision") == "block"
    assert "UI-007" in out["reason"]


@pytest.mark.anyio
async def test_repair_hook_allows_stop_when_all_targets_addressed(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_repair_targets(
        4,
        {
            "round": 4,
            "sprint": 2,
            "targets": [
                {"id": "UI-007", "kind": "ui_check", "summary": "color", "file_hints": []},
                {"id": "EXIT-02-04", "kind": "exit_criterion", "summary": "pan", "file_hints": []},
            ],
        },
    )
    file_comm.write_repair_report(
        4,
        {
            "round": 4,
            "addressed": [
                {"target_id": "UI-007", "addressed": True, "files_modified": ["frontend/src/x.css"], "notes": "ok"},
                {"target_id": "EXIT-02-04", "addressed": True, "files_modified": ["frontend/src/y.jsx"], "notes": "ok"},
            ],
        },
    )

    hook = make_repair_completion_hook(
        targets_path=file_comm.dir / "repair_targets_round_4.json",
        report_path=file_comm.dir / "repair_report_round_4.json",
        max_block_attempts=3,
        file_comm=file_comm,
        round_num=4,
    )
    out = await hook({"hook_event_name": "Stop", "stop_hook_active": False}, None, {})
    assert out.get("decision") != "block"
    assert out.get("continue_") is True


@pytest.mark.anyio
async def test_repair_hook_gives_up_after_max_block_attempts(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_repair_targets(
        4,
        {
            "round": 4,
            "sprint": 2,
            "targets": [
                {"id": "UI-007", "kind": "ui_check", "summary": "color", "file_hints": []},
            ],
        },
    )
    # No report ever written.

    hook = make_repair_completion_hook(
        targets_path=file_comm.dir / "repair_targets_round_4.json",
        report_path=file_comm.dir / "repair_report_round_4.json",
        max_block_attempts=2,
        file_comm=file_comm,
        round_num=4,
    )
    # First two attempts: block.
    for _ in range(2):
        out = await hook({"hook_event_name": "Stop", "stop_hook_active": False}, None, {})
        assert out.get("decision") == "block"
    # Third attempt: give up, allow stop, write incomplete file.
    out = await hook({"hook_event_name": "Stop", "stop_hook_active": False}, None, {})
    assert out.get("decision") != "block"
    incomplete_path = file_comm.dir / "repair_incomplete_round_4.json"
    assert incomplete_path.exists()


@pytest.mark.anyio
async def test_repair_hook_does_not_recurse_when_stop_hook_already_active(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_repair_targets(
        4,
        {
            "round": 4,
            "sprint": 2,
            "targets": [{"id": "UI-007", "kind": "ui_check", "summary": "x", "file_hints": []}],
        },
    )

    hook = make_repair_completion_hook(
        targets_path=file_comm.dir / "repair_targets_round_4.json",
        report_path=file_comm.dir / "repair_report_round_4.json",
        max_block_attempts=3,
        file_comm=file_comm,
        round_num=4,
    )
    # stop_hook_active=True → must allow stop without blocking.
    out = await hook({"hook_event_name": "Stop", "stop_hook_active": True}, None, {})
    assert out.get("decision") != "block"


# --- file_comm helpers ---


def test_file_comm_round_trips_repair_targets_and_report(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    targets = {"round": 5, "sprint": 2, "targets": [{"id": "X", "kind": "bug"}]}
    report = {"round": 5, "addressed": [{"target_id": "X", "addressed": True}]}
    file_comm.write_repair_targets(5, targets)
    file_comm.write_repair_report(5, report)

    assert file_comm.read_repair_targets(5) == targets
    assert file_comm.read_repair_report(5) == report
    assert file_comm.read_repair_targets(99) is None
    assert file_comm.read_repair_report(99) is None
