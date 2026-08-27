from __future__ import annotations

import json

import pytest

from src.orchestration.accepted_tapes import (
    AcceptedTapeError,
    accepted_replay_checks,
    append_accepted_tape,
    recover_missing_accepted_tapes,
    select_accepted_replay_checks,
)


def _check(check_id: str = "UI-001"):
    return {
        "id": check_id,
        "route": "/catalog",
        "actions": [{"action": "assert_visible", "selector": "#catalog"}],
    }


def test_accepted_tape_is_append_only_and_resume_idempotent(tmp_path):
    evidence = {"checks": [{"check_id": "UI-001", "status": "ok"}]}

    path = append_accepted_tape(
        harness_dir=tmp_path,
        sprint_num=1,
        round_num=1,
        checks=[_check()],
        evidence=evidence,
    )
    append_accepted_tape(
        harness_dir=tmp_path,
        sprint_num=1,
        round_num=1,
        checks=[_check()],
        evidence=evidence,
    )

    assert len(path.read_text().splitlines()) == 1
    assert accepted_replay_checks(tmp_path) == [_check()]


def test_accepted_tape_allows_related_multi_assertion_check(tmp_path):
    check = _check()
    check["actions"].append(
        {"action": "assert_count", "selector": "#catalog", "count": 1}
    )
    evidence = {"checks": [{"check_id": "UI-001", "status": "ok"}]}

    append_accepted_tape(
        harness_dir=tmp_path,
        sprint_num=1,
        round_num=1,
        checks=[check],
        evidence=evidence,
    )

    assert accepted_replay_checks(tmp_path) == [check]


def test_recover_missing_tape_uses_only_accepted_passing_browser_evidence(tmp_path):
    harness = tmp_path / ".harness"
    harness.mkdir()
    check = _check()
    check["actions"].append(
        {"action": "assert_count", "selector": "#catalog", "count": 1}
    )
    (harness / "accepted_sprints.json").write_text(
        json.dumps({"accepted": [1]}), encoding="utf-8"
    )
    (harness / "ui_verification_plan.json").write_text(
        json.dumps({"sprints": [{"sprint": 1, "checks": [check]}]}),
        encoding="utf-8",
    )
    (harness / "grade_round_1.json").write_text(
        json.dumps(
            {
                "sprint": 1,
                "overall_passed": True,
                "sprint_passed": True,
                "regression_passed": True,
            }
        ),
        encoding="utf-8",
    )
    (harness / "browser_evidence_round_1.json").write_text(
        json.dumps({"checks": [{"check_id": "UI-001", "status": "ok"}]}),
        encoding="utf-8",
    )

    first = recover_missing_accepted_tapes(tmp_path)
    second = recover_missing_accepted_tapes(tmp_path)

    assert first == {"recovered": [{"sprint": 1, "round": 1}], "already_present": []}
    assert second == {"recovered": [], "already_present": [{"sprint": 1, "round": 1}]}
    assert len((harness / "accepted_tapes.jsonl").read_text().splitlines()) == 1


def test_accepted_tape_rejects_legacy_evaluate_contract(tmp_path):
    check = {
        "id": "UI-legacy",
        "actions": [{"action": "evaluate", "expression": "true"}],
    }

    with pytest.raises(AcceptedTapeError, match="final typed assertion"):
        append_accepted_tape(
            harness_dir=tmp_path,
            sprint_num=1,
            round_num=1,
            checks=[check],
            evidence={"checks": [{"status": "ok"}]},
        )


def test_accepted_tape_rejects_malformed_historical_line(tmp_path):
    (tmp_path / "accepted_tapes.jsonl").write_text(json.dumps({"schema_version": "old"}) + "\n")

    with pytest.raises(AcceptedTapeError, match="accepted-tape-v1"):
        accepted_replay_checks(tmp_path)


def test_impact_selection_replays_related_obligations_and_route_sentinels(tmp_path):
    checks = [
        {
            **_check("UI-catalog-filter"),
            "requirement_id": "REQ-filter",
            "impact_tags": ["catalog", "shared-store"],
            "critical": True,
        },
        {
            **_check("UI-settings-theme"),
            "route": "/settings",
            "requirement_id": "REQ-theme",
            "impact_tags": ["settings"],
            "critical": True,
        },
        {
            **_check("UI-settings-density"),
            "route": "/settings",
            "requirement_id": "REQ-density",
            "impact_tags": ["settings"],
            "critical": False,
        },
    ]
    append_accepted_tape(
        harness_dir=tmp_path,
        sprint_num=1,
        round_num=1,
        checks=checks,
        evidence={"checks": [
            {"check_id": item["id"], "status": "ok"} for item in checks
        ]},
    )

    selection = select_accepted_replay_checks(
        tmp_path,
        edit_card={
            "impact_tags": ["shared-store"],
            "target_routes": ["/catalog"],
            "retired_requirement_ids": [],
        },
        accepted_edit_index=2,
        full_replay_interval=5,
    )

    assert [item["id"] for item in selection["checks"]] == [
        "UI-catalog-filter",
        "UI-settings-theme",
    ]
    assert selection["mode"] == "impact_scoped"
    assert selection["selected_reasons"]["UI-settings-theme"] == "protected_route_sentinel"


def test_impact_selection_excludes_explicitly_retired_requirement(tmp_path):
    check = {
        **_check("UI-old"),
        "requirement_id": "REQ-old",
        "impact_tags": ["catalog"],
        "critical": True,
    }
    append_accepted_tape(
        harness_dir=tmp_path,
        sprint_num=1,
        round_num=1,
        checks=[check],
        evidence={"checks": [{"check_id": "UI-old", "status": "ok"}]},
    )

    selection = select_accepted_replay_checks(
        tmp_path,
        edit_card={
            "impact_tags": ["catalog"],
            "target_routes": ["/catalog"],
            "retired_requirement_ids": ["REQ-old"],
        },
        accepted_edit_index=2,
        full_replay_interval=5,
    )

    assert selection["checks"] == []
    assert selection["skipped_reasons"]["UI-old"] == "requirement_retired"


def test_impact_selection_periodically_runs_full_replay(tmp_path):
    checks = [
        {**_check("UI-a"), "route": "/a", "requirement_id": "REQ-a", "impact_tags": ["a"]},
        {**_check("UI-b"), "route": "/b", "requirement_id": "REQ-b", "impact_tags": ["b"]},
    ]
    append_accepted_tape(
        harness_dir=tmp_path,
        sprint_num=1,
        round_num=1,
        checks=checks,
        evidence={"checks": [{"check_id": item["id"], "status": "ok"} for item in checks]},
    )

    selection = select_accepted_replay_checks(
        tmp_path,
        edit_card={"impact_tags": ["new"], "target_routes": ["/new"], "retired_requirement_ids": []},
        accepted_edit_index=5,
        full_replay_interval=5,
    )

    assert selection["mode"] == "periodic_full"
    assert [item["id"] for item in selection["checks"]] == ["UI-a", "UI-b"]
