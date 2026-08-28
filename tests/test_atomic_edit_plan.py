import json
from pathlib import Path

from src.agents.edit_planner import AtomicEditPlannerToolPolicy
from src.agents.planner import _validate_planning_bundle
from src.config import HarnessConfig
from src.orchestration.atomic_edit_plan import (
    materialize_atomic_edit_compatibility_bundle,
    normalize_atomic_edit_plan_payload,
    write_atomic_edit_plan,
)
from src.orchestration.file_comm import FileComm
from src.prompts.edit_planner import ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT


def _write_edit_contract(workdir: Path) -> None:
    harness = workdir / ".harness"
    harness.mkdir(parents=True, exist_ok=True)
    (harness / "edit_task_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "edit-task-contract-v1",
                "owner": "harness",
                "task_mode": "edit",
                "baseline_commit": "abc123",
                "requested_target_routes": ["/catalog.html"],
                "protect_non_target_routes": True,
                "source_policy": {},
            }
        ),
        encoding="utf-8",
    )


def _atomic_plan() -> dict:
    return {
        "schema_version": "atomic-edit-plan-v1",
        "title": "Add an in-stock filter",
        "goal": "Let catalog users show only products that are in stock.",
        "deliverables": ["One route-local filter control and filtered list behavior."],
        "exit_criteria": ["The filter hides out-of-stock items without changing other pages."],
        "requirement_changes": [
            {
                "requirement_id": "REQ-CATALOG-STOCK",
                "relation": "add",
                "prior_requirement_ids": [],
                "rationale": "This is the newly requested catalog behavior.",
            }
        ],
        "impact_tags": ["route:/catalog.html", "catalog-filter"],
        "unresolved_conflicts": [],
        "visual_evidence": "conditional",
        "visual_evidence_reason": "DOM state and visibility checks establish the behavior.",
        "checks": [
            {
                "id": "UI-STOCK-1",
                "task": "Enable the in-stock filter.",
                "expected_result": "Only in-stock product cards remain visible.",
                "critical": True,
                "category": "interaction",
                "requirement_id": "REQ-CATALOG-STOCK",
                "impact_tags": ["route:/catalog.html", "catalog-filter"],
                "route": "/catalog.html",
                "fixtures": ["In stock"],
                "actions": [
                    {"action": "click", "selector": "#in-stock-filter"},
                    {"action": "assert_count", "selector": ".product-card:not([hidden])", "count": 2},
                ],
            }
        ],
    }


def test_atomic_plan_materializes_valid_legacy_views_without_model_authorship(
    tmp_path: Path,
):
    _write_edit_contract(tmp_path)
    file_comm = FileComm(tmp_path / ".harness")
    plan = _atomic_plan()
    write_atomic_edit_plan(file_comm.dir, plan)

    materialize_atomic_edit_compatibility_bundle(
        file_comm=file_comm,
        instruction_delta="Add an in-stock filter to the catalog page.",
        plan=plan,
    )

    _validate_planning_bundle(file_comm, HarnessConfig())
    assert file_comm.read_sprint_plan()["total_sprints"] == 1
    assert file_comm.read_feature_list()["features"][0]["id"] == "EDIT-001"
    assert "accepted-project-preservation" == file_comm.read_design_tokens()["theme_name"]


def test_atomic_edit_planner_cannot_explore_or_mutate_frontend():
    policy = AtomicEditPlannerToolPolicy()

    assert policy.check("Read", {"file_path": "frontend/catalog.js"}) is not None
    assert policy.check("Write", {"file_path": ".harness/spec.md"}) is not None
    assert policy.check(
        "Write", {"file_path": ".harness/atomic_edit_plan.json"}
    ) is None
    assert policy.check("Bash", {"command": "find frontend"}) is not None


def test_atomic_edit_prompt_excludes_heavy_planning_outputs():
    prompt = ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT.lower()

    assert "goal, source_anchors, visual_evidence, checks" in prompt
    assert "deliverables" not in prompt
    assert "exit_criteria" not in prompt
    assert "requirement_changes" not in prompt
    assert "do not design a new product" in prompt
    assert "multiple sprints" in prompt


def test_minimal_authored_atomic_plan_gets_deterministic_compatibility_fields():
    normalized = normalize_atomic_edit_plan_payload(
        {
            "schema_version": "atomic-edit-plan-v1",
            "goal": "Toggle the Public Transit block.",
            "source_anchors": ["Public Transit"],
            "visual_evidence": "not_required",
            "checks": [
                {
                    "id": "EDIT-CHECK-1",
                    "route": "/",
                    "actions": [
                        {"action": "click", "selector": "#toggle"},
                        {"action": "assert_hidden", "selector": "#transit"},
                    ],
                }
            ],
        },
        instruction_delta="Add a toggle for the Public Transit block.",
    )

    assert normalized["title"] == "Toggle the Public Transit block."
    assert normalized["deliverables"] == ["Toggle the Public Transit block."]
    assert normalized["exit_criteria"] == ["All target browser assertions pass."]
    assert normalized["requirement_changes"][0]["requirement_id"] == "REQ-EDIT-001"
    assert normalized["checks"][0]["requirement_id"] == "REQ-EDIT-001"
    assert normalized["checks"][0]["task"] == "Verify the requested atomic Edit."


def test_atomic_plan_normalizes_structured_visual_evidence_alias():
    payload = _atomic_plan()
    payload["visual_evidence"] = {
        "type": "conditional",
        "reason": "DOM visibility is sufficient unless layout shifts.",
    }
    payload.pop("visual_evidence_reason")

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["visual_evidence"] == "conditional"
    assert normalized["visual_evidence_reason"] == (
        "DOM visibility is sufficient unless layout shifts."
    )


def test_atomic_plan_normalizes_common_action_field_aliases():
    payload = _atomic_plan()
    payload["checks"][0]["actions"] = [
        {"action": "click", "target": "#toggle"},
        {
            "action": "assert_attribute",
            "target": "#toggle",
            "attribute": "aria-pressed",
            "value": "true",
        },
        {"action": "assert_text", "target": "#status", "text": "Shown"},
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["checks"][0]["actions"] == [
        {"action": "click", "selector": "#toggle"},
        {
            "action": "assert_attribute",
            "selector": "#toggle",
            "name": "aria-pressed",
            "value": "true",
        },
        {"action": "assert_text", "selector": "#status", "value": "Shown"},
    ]


def test_atomic_plan_replaces_unscoped_locator_alternatives_with_stable_testids():
    payload = _atomic_plan()
    payload["checks"][0]["actions"] = [
        {"action": "assert_visible", "selector": "text=Public Transit"},
        {
            "action": "click",
            "selector": "button[aria-label='Transit'], input[type='checkbox']",
        },
        {"action": "assert_hidden", "selector": "text=Public Transit"},
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["checks"][0]["actions"] == [
        {
            "action": "assert_visible",
            "selector": "[data-testid='edit-target-public-transit']",
        },
        {
            "action": "click",
            "selector": "[data-testid='edit-control-1']",
        },
        {
            "action": "assert_hidden",
            "selector": "[data-testid='edit-target-public-transit']",
        },
    ]

def test_atomic_plan_materializes_requirement_delta_when_provider_omits_it():
    payload = _atomic_plan()
    payload["requirement_changes"] = []

    normalized = normalize_atomic_edit_plan_payload(
        payload, instruction_delta="Add the requested toggle."
    )

    assert normalized["requirement_changes"] == [
        {
            "requirement_id": "REQ-EDIT-001",
            "relation": "add",
            "prior_requirement_ids": [],
            "rationale": "Add the requested toggle.",
        }
    ]
    assert normalized["source_anchors"] == []


def test_atomic_plan_uses_user_quoted_literal_as_source_anchor_fallback():
    payload = _atomic_plan()

    normalized = normalize_atomic_edit_plan_payload(
        payload,
        instruction_delta="Toggle the 'Public Transit' block without changing the footer.",
    )

    assert normalized["source_anchors"] == ["Public Transit"]


def test_atomic_plan_links_checks_to_local_requirement_and_trims_unasserted_tail():
    payload = _atomic_plan()
    payload["requirement_changes"] = []
    payload["checks"][0]["requirement_id"] = None
    payload["checks"][0]["actions"] = [
        {"action": "assert_visible", "selector": "#toggle"},
        {"action": "click", "selector": "#toggle"},
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["checks"][0]["requirement_id"] == "REQ-EDIT-001"
    assert normalized["checks"][0]["actions"] == [
        {"action": "assert_visible", "selector": "#toggle"}
    ]
