from pathlib import Path

import pytest

from src.orchestration.hidden_oracle_checks import (
    merge_hidden_oracle_checks,
    read_hidden_oracle_checks,
    write_hidden_oracle_checks,
)


def _check(check_id: str, route: str = "/"):
    return {
        "id": check_id,
        "route": route,
        "actions": [{"action": "assert_hash", "value": ""}],
    }


def test_hidden_oracle_round_trip_stays_harness_owned(tmp_path: Path):
    path = write_hidden_oracle_checks(
        tmp_path, [_check("ORACLE-NO-HASH")], target_routes=["/"]
    )

    assert path.name == "hidden_oracle_checks.json"
    assert read_hidden_oracle_checks(tmp_path) == [_check("ORACLE-NO-HASH")]


def test_hidden_oracle_preserves_bounded_risk_provenance(tmp_path: Path):
    check = {
        "id": "RISK-LOSS",
        "route": "/",
        "origin": "source_edit_risk_analysis",
        "repair_type": "Loss of Interactivity",
        "risk_reason": "planner requires a click",
        "actions": [{
            "action": "assert_webcompass_risk",
            "selector": "#save",
            "defect_type": "Loss of Interactivity",
        }],
    }
    write_hidden_oracle_checks(tmp_path, [check], target_routes=["/"])

    assert read_hidden_oracle_checks(tmp_path) == [check]


def test_hidden_oracle_rejects_off_target_routes_and_visible_id_collisions(
    tmp_path: Path,
):
    with pytest.raises(ValueError, match="outside target routes"):
        write_hidden_oracle_checks(
            tmp_path, [_check("ORACLE-ADMIN", "/admin")], target_routes=["/"]
        )
    write_hidden_oracle_checks(
        tmp_path, [_check("ORACLE-NO-HASH")], target_routes=["/"]
    )
    with pytest.raises(ValueError, match="collide"):
        merge_hidden_oracle_checks(tmp_path, [_check("ORACLE-NO-HASH")])
