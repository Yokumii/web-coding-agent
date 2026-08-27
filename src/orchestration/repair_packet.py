"""Build a bounded, evidence-backed handoff for the next Repair round."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _failed_checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in payload.get("checks") or []
        if isinstance(item, dict) and item.get("status") == "action_failed"
    ]


def write_repair_packet(
    *, workdir: Path, round_num: int, sprint_num: int, grades: dict[str, Any]
) -> dict[str, Any]:
    harness = Path(workdir) / ".harness"
    browser_ref = harness / f"browser_evidence_round_{round_num}.json"
    tape_ref = harness / f"accepted_tape_replay_round_{round_num}.json"
    plan_ref = harness / f"minimal_path_plan_round_{round_num}.json"
    browser = _read_json(browser_ref)
    tape = _read_json(tape_ref)
    plan = _read_json(plan_ref)
    cone = plan.get("source_change_cone") or {}
    budgets = plan.get("budgets") or cone.get("budgets") or {}
    max_files = max(1, int(budgets.get("max_touched_files", 3)))
    max_patch_lines = max(1, int(budgets.get("max_patch_lines", 120)))
    allowed_paths = sorted({
        str(path)
        for key in ("candidate_paths", "local_paths", "dependency_paths", "planned_new_paths")
        for path in cone.get(key) or []
        if str(path)
    })
    packet = {
        "schema_version": "repair-packet-v1",
        "owner": "harness",
        "status": "repairable",
        "source_round": round_num,
        "sprint": sprint_num,
        "target_routes": list((plan.get("route_scope") or {}).get("target_routes") or []),
        "failed_checks": _failed_checks(browser),
        "failed_regressions": _failed_checks(tape),
        "bugs": list(grades.get("bugs_found") or []),
        "regressions": list(grades.get("regressions_found") or []),
        "required_actions": list(grades.get("repair_instructions") or []),
        "allowed_source_paths": allowed_paths,
        "budgets": {
            "max_touched_files": max_files,
            "max_changed_lines": max_files * max_patch_lines,
        },
        "evidence_refs": [
            ref
            for ref, path in (
                (f".harness/browser_evidence_round_{round_num}.json", browser_ref),
                (f".harness/accepted_tape_replay_round_{round_num}.json", tape_ref),
                (f".harness/minimal_path_plan_round_{round_num}.json", plan_ref),
            )
            if path.is_file()
        ],
    }
    if not (packet["failed_checks"] or packet["failed_regressions"] or packet["bugs"] or packet["regressions"] or packet["required_actions"]):
        packet["status"] = "unidentifiable"
    path = harness / f"repair_packet_round_{round_num}.json"
    path.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return packet


__all__ = ["write_repair_packet"]
