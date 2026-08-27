from __future__ import annotations

import json

from src.orchestration.repair_packet import write_repair_packet


def test_repair_packet_collects_exact_failed_evidence_and_dynamic_scope(tmp_path):
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "browser_evidence_round_1.json").write_text(json.dumps({
        "checks": [
            {"check_id": "UI-pass", "route": "/catalog", "status": "ok"},
            {
                "check_id": "UI-archive",
                "route": "/catalog",
                "status": "action_failed",
                "steps": [{"action": "assert_hidden", "ok": False, "output": {"actual": False, "expected": True}}],
            },
        ]
    }))
    (harness / "minimal_path_plan_round_1.json").write_text(json.dumps({
        "source_change_cone": {
            "initial_paths": ["frontend/catalog.js"],
            "candidate_paths": ["frontend/catalog.js", "frontend/store.js"],
            "budgets": {"max_touched_files": 2, "max_patch_lines": 80},
        },
        "route_scope": {"target_routes": ["/catalog"]},
    }))

    packet = write_repair_packet(
        workdir=tmp_path,
        round_num=1,
        sprint_num=1,
        grades={
            "bugs_found": ["Archive state remained visible."],
            "regressions_found": [],
            "repair_instructions": ["Update the archive transition."],
            "minimality_certificate": None,
        },
    )

    assert packet["status"] == "repairable"
    assert [item["check_id"] for item in packet["failed_checks"]] == ["UI-archive"]
    assert packet["allowed_source_paths"] == ["frontend/catalog.js", "frontend/store.js"]
    assert packet["budgets"] == {"max_touched_files": 2, "max_changed_lines": 160}
    assert (harness / "repair_packet_round_1.json").is_file()
