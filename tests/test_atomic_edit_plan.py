import json
from pathlib import Path

import pytest

from src.agents.edit_planner import (
    AtomicEditPlannerToolPolicy,
    _AcceptedSourceControlParser,
    _ensure_source_surface_entry,
    _ensure_direct_hash_setup,
    _planned_testid_is_grounded,
    _rewrite_observed_item_selectors,
    run_atomic_edit_planner,
)
from src.agents.planner import _validate_planning_bundle
from src.config import HarnessConfig
from src.orchestration.atomic_edit_plan import (
    AtomicEditCheck,
    materialize_atomic_edit_compatibility_bundle,
    normalize_atomic_edit_plan_payload,
    write_atomic_edit_plan,
)
from src.orchestration.file_comm import FileComm
from src.orchestration.ui_action_contracts import TYPED_ASSERTION_ACTIONS
from src.prompts.edit_planner import ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT


def test_single_check_accepts_long_continuous_action_flow():
    payload = {
        "id": "UI-001",
        "task": "Exercise one complete journey.",
        "expected_result": "The final state persists.",
        "category": "persistence",
        "requirement_id": "REQ-EDIT-001",
        "impact_tags": ["atomic-edit"],
        "actions": [
            {"action": "click", "selector": f"#control-{index}"}
            for index in range(29)
        ] + [{"action": "assert_text", "selector": "#result", "value": "Saved"}],
    }
    assert len(AtomicEditCheck.model_validate(payload).actions) == 30
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


def test_atomic_plan_preserves_more_than_twelve_source_anchors(tmp_path: Path):
    payload = _atomic_plan()
    payload["source_anchors"] = [f"Existing record {index}" for index in range(19)]
    path = write_atomic_edit_plan(tmp_path, payload)
    assert json.loads(path.read_text())["source_anchors"] == payload["source_anchors"]


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


@pytest.mark.anyio
async def test_upstream_atomic_plan_is_reused_without_provider_call(tmp_path: Path):
    _write_edit_contract(tmp_path)
    file_comm = FileComm(tmp_path / ".harness")
    write_atomic_edit_plan(file_comm.dir, _atomic_plan())

    stats = await run_atomic_edit_planner(
        HarnessConfig(agent_runtime="openai", planner_model="qwen-test"),
        "Add an in-stock-filter for product-card items on the catalog page.",
        file_comm,
        tmp_path,
    )

    assert stats.cost_usd == 0
    assert stats.usage == {"recovery": "upstream_atomic_plan"}
    trace = (file_comm.dir / "traces" / "planner.jsonl").read_text()
    assert "upstream_atomic_plan_reuse" in trace


def test_atomic_edit_planner_cannot_explore_or_mutate_frontend():
    policy = AtomicEditPlannerToolPolicy()

    assert policy.check("Read", {"file_path": "frontend/catalog.js"}) is not None
    assert policy.check("Write", {"file_path": ".harness/spec.md"}) is not None
    assert policy.check(
        "Write", {"file_path": ".harness/atomic_edit_plan.json"}
    ) is None
    assert policy.check("Bash", {"command": "find frontend"}) is not None


def test_atomic_plan_reuses_stable_named_source_item_instead_of_invented_testid():
    payload = _atomic_plan()
    payload["checks"][0]["actions"] = [
        {
            "action": "assert_visible",
            "selector": (
                "[data-testid='sidebar-item-audio-studio-driver']"
                "[class~='highlighted']"
            ),
        }
    ]
    contract = {
        "pages": [{
            "route": "/",
            "addressable_items": [{
                "data_name": "Audio Studio Driver",
                "selector": 'a.sidebar-item[data-name="Audio Studio Driver"]',
            }],
        }]
    }

    normalized = _rewrite_observed_item_selectors(payload, contract)

    assert normalized["checks"][0]["actions"][0]["selector"] == (
        'a.sidebar-item[data-name="Audio Studio Driver"][class~=\'highlighted\']'
    )


def test_atomic_plan_injects_each_named_items_direct_hash_setup():
    payload = _atomic_plan()
    payload["checks"][0]["actions"] = [{
        "action": "assert_visible",
        "selector": 'a.sidebar-item[data-name="Audio Studio Driver"]',
    }]

    normalized = _ensure_direct_hash_setup(
        payload,
        user_prompt="Enable direct linking via URL hash fragments such as #audio-studio-driver.",
    )

    assert normalized["checks"][0]["actions"][0] == {
        "action": "set_hash",
        "value": "#audio-studio-driver",
        "settle_ms": 150,
    }


def test_new_testid_grounding_accepts_simple_plural_instruction_form():
    assert _planned_testid_is_grounded(
        "previous-position",
        "Capture the previous and new positions for each reorder event.",
    )


def test_new_heading_testid_requires_requested_semantic_stem():
    assert _planned_testid_is_grounded("shipment-tracking-section-heading", "Show shipment tracking separately.")
    assert not _planned_testid_is_grounded("financial-forecast-section-heading", "Show shipment tracking separately.")
    assert _planned_testid_is_grounded("shipment-tracking-header-date", "Show shipment tracking and its date.")
    assert not _planned_testid_is_grounded("shipment-tracking-header-revenue", "Show shipment tracking and its date.")


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
        {
            "action": "assert_text",
            "selector": "#status",
            "value": "Shown",
            "match": "contains",
        },
    ]


def test_atomic_plan_normalizes_doc_drag_source_and_nonempty_text_placeholder():
    payload = _atomic_plan()
    payload["checks"][0]["actions"] = [
        {
            "action": "drag_and_drop",
            "selector": ".card:nth-child(1)",
            "target_selector": ".card:nth-child(2)",
        },
        {
            "action": "assert_text",
            "selector": "[data-testid='timestamp']",
            "match": "nonempty",
        },
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["checks"][0]["actions"] == [
        {
            "action": "drag_and_drop",
            "source_selector": ".card:nth-child(1)",
            "target_selector": ".card:nth-child(2)",
        },
        {
            "action": "assert_text",
            "selector": "[data-testid='timestamp']",
            "match": "nonempty",
            "value": "",
        },
    ]


def test_atomic_plan_completes_bounded_positional_drag_target():
    payload = _atomic_plan()
    payload["checks"][0]["actions"] = [{
        "action": "drag_and_drop",
        "source_selector": ".artifact-card:nth-child(1)",
    }]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["checks"][0]["actions"] == [{
        "action": "drag_and_drop",
        "source_selector": ".artifact-card:nth-child(1)",
        "target_selector": ".artifact-card:nth-child(2)",
    }]


def test_atomic_plan_maps_empty_url_placeholder_before_check_splitting():
    payload = _atomic_plan()
    payload["checks"][0]["actions"] = [
        {"action": "assert_text", "selector": "#one", "value": "one"},
        {"action": "assert_text", "selector": "#two", "value": "two"},
        {"action": "assert_text", "selector": "#three", "value": "three"},
        {"action": "assert_text", "selector": "#four", "value": "four"},
        {"action": "assert_url", "value": "", "match": "contains"},
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert all(check["route"] == "/catalog.html" for check in normalized["checks"])
    assert normalized["checks"][-1]["actions"][-1] == {
        "action": "assert_hash",
        "value": "",
        "match": "nonempty",
    }


def test_atomic_plan_supplies_bounded_default_for_scroll_without_y():
    payload = _atomic_plan()
    payload["checks"][0]["actions"] = [
        {"action": "scroll"},
        {"action": "assert_visible", "selector": "#target"},
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["checks"][0]["actions"][0] == {
        "action": "scroll",
        "y": 600,
    }


def test_atomic_plan_normalizes_doc_api_action_dialect_and_guessed_source_count():
    payload = _atomic_plan()
    payload["checks"][0]["fixtures"] = []
    payload["checks"][0]["actions"] = [
        {"action": "assert_count", "selector": "select option", "count": 4},
        {"action": "select_option", "selector": "select", "option": "Timepiece"},
        {"action": "assert_hash", "selector": "body", "name": "hash", "value": "#type=Timepiece"},
        {
            "action": "reload",
            "selector": "",
            "action_type": "reload",
            "action_detail": None,
        },
        {"action": "assert_value", "selector": "select", "value": "Timepiece"},
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["checks"][0]["actions"] == [
        {"action": "assert_visible", "selector": "select option"},
        {"action": "select_option", "selector": "select", "value": "Timepiece"},
        {"action": "assert_hash", "value": "#type=Timepiece"},
        {"action": "reload"},
        {"action": "assert_value", "selector": "select", "value": "Timepiece"},
    ]


def test_atomic_plan_converts_hash_route_filter_restore_to_action_flow():
    payload = _atomic_plan()
    payload["checks"][0]["route"] = "/#type=Document"
    payload["checks"][0]["actions"] = [
        {"action": "click", "selector": "a[href='#gallery']"},
        {
            "action": "assert_value",
            "selector": "[data-testid='type-filter']",
            "value": "Document",
        },
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["checks"][0]["route"] == "/"
    assert normalized["checks"][0]["category"] == "persistence"
    assert normalized["checks"][0]["actions"] == [
        {"action": "click", "selector": "a[href='#gallery']"},
        {
            "action": "select_option",
            "selector": "[data-testid='type-filter']",
            "value": "Document",
        },
        {"action": "assert_hash", "value": "#type=Document"},
        {"action": "reload"},
        {
            "action": "assert_value",
            "selector": "[data-testid='type-filter']",
            "value": "Document",
        },
    ]


def test_atomic_plan_keeps_each_reload_check_self_contained():
    payload = _atomic_plan()
    payload["checks"] = [
        {
            "id": "create-history",
            "route": "/",
            "actions": [
                {"action": "click", "selector": "button"},
                {"action": "assert_count", "selector": "[data-testid='download-history-item']", "count": 1},
            ],
        },
        {
            "id": "persist-history",
            "route": "/",
            "actions": [
                {"action": "click", "selector": "button"},
                {"action": "reload"},
                {"action": "assert_count", "selector": "[data-testid='download-history-item']", "count": 1},
            ],
        },
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["checks"][1]["category"] == "persistence"
    assert normalized["checks"][1]["actions"] == [
        {"action": "click", "selector": "button"},
        {"action": "reload"},
        {"action": "assert_visible", "selector": "[data-testid='download-history-item']"},
    ]


def test_navigation_presence_check_does_not_erase_later_filter_setup():
    payload = _atomic_plan()
    payload["checks"] = [
        {
            "id": "presence",
            "route": "/",
            "actions": [
                {"action": "click", "selector": "a[href='#gallery']"},
                {"action": "assert_visible", "selector": "[data-testid='type-filter']"},
            ],
        },
        {
            "id": "persist-filter",
            "route": "/",
            "actions": [
                {"action": "click", "selector": "a[href='#gallery']"},
                {
                    "action": "select_option",
                    "selector": "[data-testid='type-filter']",
                    "value": "Timepiece",
                },
                {"action": "assert_hash", "value": "#type=Timepiece"},
                {"action": "reload"},
                {
                    "action": "assert_value",
                    "selector": "[data-testid='type-filter']",
                    "value": "Timepiece",
                },
            ],
        },
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["checks"][1]["actions"][1]["action"] == "select_option"
    assert normalized["checks"][1]["actions"][2] == {
        "action": "assert_hash",
        "value": "#type=Timepiece",
    }


def test_atomic_plan_strips_hash_state_from_check_route_and_unbacked_counts():
    payload = _atomic_plan()
    payload["checks"][0]["route"] = "/#type=Timepiece"
    payload["checks"][0]["fixtures"] = []
    payload["checks"][0]["actions"] = [
        {"action": "select_option", "selector": "select", "value": "All"},
        {"action": "assert_count", "selector": ".artifact-card", "count": 10},
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert normalized["checks"][0]["route"] == "/"
    assert normalized["checks"][0]["actions"][-1] == {
        "action": "assert_visible",
        "selector": ".artifact-card",
    }


def test_atomic_plan_preserves_zero_and_instruction_grounded_exact_counts():
    payload = _atomic_plan()
    payload["checks"][0]["fixtures"] = []
    payload["checks"][0]["actions"] = [
        {"action": "click", "selector": "#mark-all-read"},
        {"action": "assert_count", "selector": ".notification.unread", "count": 0},
        {"action": "assert_count", "selector": ".notification.read", "count": 3},
    ]

    normalized = normalize_atomic_edit_plan_payload(
        payload,
        instruction_delta="Mark all three notifications as read.",
    )

    actions = normalized["checks"][0]["actions"]
    assert actions[-2:] == [
        {"action": "assert_count", "selector": ".notification.unread", "count": 0},
        {"action": "assert_count", "selector": ".notification.read", "count": 3},
    ]


def test_atomic_plan_normalizes_storage_and_head_checks_without_splitting():
    payload = _atomic_plan()
    payload["checks"][0]["id"] = "physical-pages"
    payload["checks"][0]["route"] = "/"
    payload["checks"][0]["actions"] = [
        {"action": "assert_visible", "selector": "a[href='/']"},
        {"action": "assert_visible", "selector": "a[href='/settings.html']"},
        {"action": "click", "selector": "a[href='/settings.html']"},
        {"action": "assert_url", "value": "/settings.html"},
        {"action": "assert_visible", "selector": "link[href='styles.css']"},
        {"action": "assert_visible", "selector": "script[src='app.js']"},
        {
            "action": "set_storage_value",
            "name": "selectedCategory",
            "value": "Renewable",
        },
        {
            "action": "assert_storage_value",
            "name": "selectedCategory",
            "value": "Renewable",
        },
    ]

    normalized = normalize_atomic_edit_plan_payload(payload)

    assert [item["id"] for item in normalized["checks"]] == ["physical-pages"]
    assert [item["route"] for item in normalized["checks"]] == ["/"]
    assert normalized["checks"][0]["actions"][-3:] == [
        {
            "action": "assert_attribute",
            "selector": "script[src='app.js']",
            "name": "src",
            "value": "app.js",
        },
        {
            "action": "set_storage_value",
            "storage": "local",
            "key": "selectedCategory",
            "value": "Renewable",
        },
        {
            "action": "assert_storage_value",
            "storage": "local",
            "key": "selectedCategory",
            "value": "Renewable",
            "match": "contains",
        },
    ]
    assert len(normalized["checks"]) == 1


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


def test_source_hidden_view_uses_only_unique_native_opener():
    parser = _AcceptedSourceControlParser()
    parser.feed('<button id="openTools" aria-controls="tools">Tools</button><section id="tools" hidden>Tools workspace</section>')
    contract = {"pages": [{"route":"/", "controls":parser.controls, "surfaces":parser.surfaces}]}
    plan = {"checks":[{"id":"open__part1", "route":"/index.html", "actions":[
        {"action":"scroll", "y":600}, {"action":"assert_visible", "selector":"#tools"}]}]}
    updated = _ensure_source_surface_entry(plan, contract)
    assert updated["checks"][0]["actions"][1] == {"action":"click", "selector":"#openTools"}
    assert _ensure_source_surface_entry(updated, contract) == updated
    parser.controls.append({"selector":"#otherOpener","aria_controls":"tools"})
    assert _ensure_source_surface_entry(plan, contract) == plan
    parser.controls.pop()
    plan["checks"][0]["actions"].insert(0, {"action":"click", "selector":"#closeTools"})
    assert _ensure_source_surface_entry(plan, contract) == plan


@pytest.mark.anyio
@pytest.mark.parametrize('correction_fails', [False, True])
async def test_planner_corrects_invalid_action_contract_once(tmp_path, monkeypatch, correction_fails):
    from src.agents import edit_planner as module
    from src.agents.planner import PlannerValidationError
    from src.agents.sdk_runner import AgentRunStats
    file_comm = FileComm(tmp_path / '.harness')
    (file_comm.dir/'traces').mkdir()
    trace = file_comm.dir/'traces/planner.jsonl'
    (file_comm.dir/'atomic_edit_plan.json').write_text(json.dumps({'invalid':'capture-only'}))
    calls=[]
    async def run(config, prompt, comm, workdir, *, validation_feedback=None):
        calls.append(validation_feedback)
        with trace.open('a') as handle:
            handle.write(json.dumps({'event':'usage','estimated_cost_usd':0.2,
                                     'attempt_usage':{'input_tokens':100,'output_tokens':10}})+'\n')
        if validation_feedback is None or correction_fails:
            raise PlannerValidationError('assert_value requires scalar value')
        assert validation_feedback['rejected_plan']=={'invalid':'capture-only'}
        return AgentRunStats(0.2,1,1,{'input_tokens':100,'output_tokens':10},{},{})
    monkeypatch.setattr(module,'_run_atomic_edit_planner',run)
    if correction_fails:
        with pytest.raises(PlannerValidationError):
            await module.run_atomic_edit_planner(HarnessConfig(agent_runtime='openai'),'request',file_comm,tmp_path)
    else:
        result=await module.run_atomic_edit_planner(HarnessConfig(agent_runtime='openai'),'request',file_comm,tmp_path)
        assert result.cost_usd==0.4
        assert result.token_usage=={'input_tokens':200,'output_tokens':20}
    assert len(calls)==2
    assert len(list(file_comm.dir.glob('rejected_atomic_plan_*.json')))==1


def test_split_completion_flow_is_not_limited_to_ten_segments(tmp_path):
    payload = _atomic_plan()
    original = payload['checks'][0]
    payload['checks'] = [{**original, 'id': f'UI-FLOW-{i}'} for i in range(12)]
    path = write_atomic_edit_plan(tmp_path, payload)
    assert len(json.loads(path.read_text())['checks']) == 12
