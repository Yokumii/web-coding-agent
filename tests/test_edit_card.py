from __future__ import annotations

import json

import pytest

from src.orchestration.edit_card import (
    EditCardBlockedError,
    materialize_edit_card,
    visual_evidence_required,
)


def _plans():
    sprint_plan = {
        "total_sprints": 1,
        "sprints": [{
            "number": 1,
            "title": "Archive catalog items",
            "goal": "Add archive behavior without breaking comparison state.",
            "feature_ids": ["F001"],
            "deliverables": ["Archive control"],
            "exit_criteria": ["Archived items leave the active catalog"],
            "requirement_changes": [{
                "requirement_id": "REQ-archive",
                "relation": "add",
                "prior_requirement_ids": [],
                "rationale": "The source has no archive behavior.",
            }],
            "impact_tags": ["catalog", "shared-store"],
            "unresolved_conflicts": [],
            "visual_evidence": "not_required",
            "visual_evidence_reason": "The change is state and behavior only.",
        }],
    }
    verification_plan = {
        "sprints": [{
            "sprint": 1,
            "checks": [{
                "id": "UI-archive",
                "feature_id": "F001",
                "requirement_id": "REQ-archive",
                "impact_tags": ["catalog", "shared-store"],
                "task": "Archive one item",
                "expected_result": "The item leaves the active catalog",
                "critical": True,
                "category": "state",
                "route": "/catalog",
                "actions": [{"action": "assert_hidden", "selector": "#archived-item"}],
            }],
        }],
    }
    return sprint_plan, verification_plan


def test_materialized_edit_card_keeps_exact_instruction_and_plan_links(tmp_path):
    sprint_plan, verification_plan = _plans()

    card = materialize_edit_card(
        harness_dir=tmp_path,
        instruction_delta="Archive products from the catalog.",
        edit_contract={"requested_target_routes": ["/catalog"]},
        sprint_plan=sprint_plan,
        verification_plan=verification_plan,
    )

    assert card["instruction_delta"] == "Archive products from the catalog."
    assert card["target_routes"] == ["/catalog"]
    assert card["target_check_ids"] == ["UI-archive"]
    assert card["impact_tags"] == ["catalog", "shared-store"]
    assert card["status"] == "ready"
    assert json.loads((tmp_path / "edit_card.json").read_text()) == card


def test_edit_card_blocks_unresolved_requirement_conflicts(tmp_path):
    sprint_plan, verification_plan = _plans()
    sprint_plan["sprints"][0]["unresolved_conflicts"] = [{
        "requirement_ids": ["REQ-old", "REQ-archive"],
        "description": "Archive retention conflicts with permanent deletion.",
    }]

    with pytest.raises(EditCardBlockedError, match="unresolved requirement conflict"):
        materialize_edit_card(
            harness_dir=tmp_path,
            instruction_delta="Archive products from the catalog.",
            edit_contract={"requested_target_routes": ["/catalog"]},
            sprint_plan=sprint_plan,
            verification_plan=verification_plan,
        )

    assert json.loads((tmp_path / "edit_card.json").read_text())["status"] == "blocked"


def test_edit_card_rejects_multiple_sprints_for_one_edit(tmp_path):
    sprint_plan, verification_plan = _plans()
    sprint_plan["total_sprints"] = 2
    sprint_plan["sprints"].append({**sprint_plan["sprints"][0], "number": 2})

    with pytest.raises(ValueError, match="exactly one Sprint"):
        materialize_edit_card(
            harness_dir=tmp_path,
            instruction_delta="Archive products from the catalog.",
            edit_contract={"requested_target_routes": ["/catalog"]},
            sprint_plan=sprint_plan,
            verification_plan=verification_plan,
        )


def test_visual_evidence_routes_only_when_edit_card_requires_it():
    assert visual_evidence_required({"visual_evidence": "required"}) is True
    assert visual_evidence_required({"visual_evidence": "not_required"}) is False
    assert visual_evidence_required({
        "visual_evidence": "conditional",
        "target_check_categories": ["state"],
    }) is False
    assert visual_evidence_required({
        "visual_evidence": "conditional",
        "target_check_categories": ["responsive"],
    }) is True
    assert visual_evidence_required(
        {"visual_evidence": "conditional", "target_check_categories": ["state"]},
        has_image_input=True,
    ) is True
