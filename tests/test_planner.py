from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk.types import ResultMessage

from src.agents.planner import (
    PlannerValidationError,
    _check_edit_transaction,
    _make_planner_stop_hook,
    _validate_planning_bundle,
    recover_trace_proven_planner_checkpoint,
    run_planner,
)
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.prompts.planner import PLANNER_SYSTEM_PROMPT


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _valid_spec_text() -> str:
    return (
        "# Counter App - Track Every Tap\n\n"
        "## Product Overview\nA simple spec.\n\n"
        "## Target Users\nPeople who count.\n\n"
        "## Feature Descriptions\n"
        "### 1. Counter\n"
        "**Description:** Count.\n"
        "**User Stories:**\n"
        "- As a user, I want to count.\n"
        "**(Priority: High)**\n\n"
        "## Technical Architecture\nClient-only architecture.\n\n"
        "## Visual Design Direction\nMinimal but distinctive.\n"
    )


def test_planner_requires_valid_form_precondition_for_submit_contracts():
    assert "assert_form_valid" in PLANNER_SYSTEM_PROMPT
    assert "including required select and textarea controls" in PLANNER_SYSTEM_PROMPT
    assert "select_option" in PLANNER_SYSTEM_PROMPT
    assert "do not infer a select value from ArrowDown/Enter" in PLANNER_SYSTEM_PROMPT
    assert "exact same-origin browser pathname" in PLANNER_SYSTEM_PROMPT
    assert "Consecutive checks on the same" in PLANNER_SYSTEM_PROMPT


def _write_valid_planning_bundle(file_comm: FileComm) -> None:
    file_comm.write_spec(_valid_spec_text())
    file_comm.write_design_tokens(
        {
            "theme_name": "editorial counter",
            "color": {"bg": "#111111"},
            "typography": {"display": "Space Grotesk"},
            "spacing": {"base": 8},
            "radius": {"card": 16},
            "motion": {"duration_fast": 160},
            "style_rules": ["bold hierarchy"],
            "anti_patterns": ["generic cards"],
            "visual_experiment": {
                "design_hypothesis": "Use poster-like asymmetry.",
                "reason_for_image_first": "Text-only outputs stay too templated.",
                "desired_break_from_web_templates": ["poster-like asymmetry"],
                "visual_opportunities_beyond_css": ["ink texture"],
                "forbidden_generic_patterns": ["centered card grid"],
            },
        }
    )
    file_comm.write_feature_list(
        {
            "features": [
                {
                    "id": "F001",
                    "name": "Counter",
                    "priority": "high",
                    "depends_on": [],
                    "description": "Count values.",
                    "acceptance_criteria": ["Counter increments correctly."],
                    "status": "planned",
                    "sprint": 1,
                }
            ]
        }
    )
    file_comm.write_sprint_plan(
        {
            "total_sprints": 1,
            "sprints": [
                {
                    "number": 1,
                    "title": "Core counter",
                    "goal": "Ship the primary counter flow.",
                    "feature_ids": ["F001"],
                    "deliverables": ["Visible counter UI."],
                    "exit_criteria": ["Counter increments correctly."],
                }
            ],
        }
    )
    file_comm.write_ui_verification_plan(
        {
            "sprints": [
                {
                    "sprint": 1,
                    "checks": [
                        {
                            "id": "UI-001",
                            "feature_id": "F001",
                            "task": "Click increment once.",
                            "expected_result": "Counter changes by one step.",
                            "critical": True,
                            "category": "core_interaction",
                        }
                    ],
                }
            ]
        }
    )
    file_comm.write_progress("# Progress Log\n\n## planning\n- status: complete")


def test_explicit_edit_is_one_multi_page_transaction(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "edit_task_contract.json").write_text(
        '{"schema_version":"edit-task-contract-v1"}'
    )
    sprint = {
        "number": 1,
        "requirement_changes": [{"requirement_id": "REQ-1", "relation": "add"}],
        "impact_tags": ["shared-store"],
        "visual_evidence_reason": "State-only change uses DOM/property evidence.",
    }
    verification = {"sprints": [{"sprint": 1, "checks": [
        {"id": "UI-a", "requirement_id": "REQ-1", "impact_tags": ["shared-store"]},
        {"id": "UI-b", "requirement_id": "REQ-1", "impact_tags": ["shared-store"]},
    ]}]}

    _check_edit_transaction(
        file_comm,
        {"total_sprints": 1, "sprints": [sprint]},
        verification,
    )

    with pytest.raises(PlannerValidationError, match="exactly one Sprint"):
        _check_edit_transaction(
            file_comm,
            {"total_sprints": 2, "sprints": [sprint, {**sprint, "number": 2}]},
            verification,
        )


def _write_planner_trace(file_comm: FileComm, artifact_names: list[str]) -> None:
    trace_dir = file_comm.dir / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    events: list[dict] = []
    for index, name in enumerate(artifact_names):
        events.extend(
            [
                {
                    "event": "assistant",
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(
                                        {"path": f".harness/{name}", "content": "model output"}
                                    ),
                                },
                                "id": f"call-{index}",
                            }
                        ]
                    },
                },
                {"event": "tool", "name": "write_file", "ok": True},
            ]
        )
    events.append(
        {
            "event": "run_error",
            "cumulative_usage": {"input_tokens": 100, "output_tokens": 20},
            "estimated_cost_usd": 0.001,
        }
    )
    (trace_dir / "planner.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def test_recover_trace_proven_planner_checkpoint_without_second_model_call(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    _write_planner_trace(
        file_comm,
        [
            "spec.md",
            "design_tokens.json",
            "feature_list.json",
            "sprint_plan.json",
            "ui_verification_plan.json",
        ],
    )

    stats = recover_trace_proven_planner_checkpoint(file_comm, HarnessConfig())

    assert stats is not None
    assert stats.cost_usd == 0.001
    assert stats.token_usage == {"input_tokens": 100, "output_tokens": 20}
    assert stats.usage["recovery"] == "trace_proven_planner_checkpoint"
    assert file_comm.read_accepted_sprints() == {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 0,
    }


def test_planner_checkpoint_recovery_requires_every_semantic_artifact_in_trace(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    _write_planner_trace(
        file_comm,
        ["spec.md", "design_tokens.json", "feature_list.json", "sprint_plan.json"],
    )

    assert recover_trace_proven_planner_checkpoint(file_comm, HarnessConfig()) is None


def test_atomic_edit_planner_checkpoint_recovers_from_single_semantic_artifact(
    tmp_path: Path,
):
    from src.orchestration.atomic_edit_plan import write_atomic_edit_plan

    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "edit_task_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "edit-task-contract-v1",
                "task_mode": "edit",
                "requested_target_routes": ["/"],
            }
        ),
        encoding="utf-8",
    )
    write_atomic_edit_plan(
        file_comm.dir,
        {
            "schema_version": "atomic-edit-plan-v1",
            "title": "Add a status toggle",
            "goal": "Toggle the visible status panel.",
            "deliverables": ["A working status toggle."],
            "exit_criteria": ["Clicking the toggle reveals the status panel."],
            "requirement_changes": [
                {
                    "requirement_id": "REQ-STATUS",
                    "relation": "add",
                    "prior_requirement_ids": [],
                    "rationale": "New requested behavior.",
                }
            ],
            "impact_tags": ["route:/", "status-toggle"],
            "unresolved_conflicts": [],
            "visual_evidence": "not_required",
            "visual_evidence_reason": "DOM visibility and click behavior are sufficient.",
            "checks": [
                {
                    "id": "UI-STATUS",
                    "task": "Click the status toggle.",
                    "expected_result": "The status panel is visible.",
                    "critical": True,
                    "category": "interaction",
                    "requirement_id": "REQ-STATUS",
                    "impact_tags": ["route:/", "status-toggle"],
                    "route": "/",
                    "fixtures": [],
                    "actions": [
                        {"action": "click", "selector": "#status-toggle"},
                        {"action": "assert_visible", "selector": "#status-panel"},
                    ],
                }
            ],
        },
    )
    trace = file_comm.dir / "traces" / "planner.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(
        '{"event":"atomic_edit_plan","artifact":".harness/atomic_edit_plan.json"}\n'
        '{"event":"usage","cumulative_usage":{"input_tokens":30,"output_tokens":10}}\n',
        encoding="utf-8",
    )

    stats = recover_trace_proven_planner_checkpoint(file_comm, HarnessConfig())

    assert stats is not None
    assert stats.token_usage == {"input_tokens": 30, "output_tokens": 10}
    assert file_comm.read_sprint_plan()["total_sprints"] == 1


def test_final_project_mode_instruction_requests_natural_complete_roadmap():
    from src.agents.planner import _build_planner_prompt

    prompt = _build_planner_prompt(
        HarnessConfig(final_project_mode=True), "build everything", Path("/tmp/work")
    )
    assert "natural number of Sprints" in prompt
    assert "complete requested product" in prompt
    assert "exactly one Sprint" not in prompt


def test_planner_prompt_preserves_existing_frontend_stack(tmp_path: Path):
    from src.agents.planner import _build_planner_prompt

    (tmp_path / "frontend").mkdir()
    prompt = _build_planner_prompt(HarnessConfig(), "add one interaction", tmp_path)

    assert "existing runnable frontend" in prompt
    assert "do not propose a stack migration" in prompt


def test_planner_system_prompt_requires_economical_stack_preserving_plan():
    from src.prompts.planner import PLANNER_SYSTEM_PROMPT

    assert "preserve that stack" in PLANNER_SYSTEM_PROMPT
    assert "no more than 700 words" in PLANNER_SYSTEM_PROMPT
    assert "Do not spend tool calls rereading" in PLANNER_SYSTEM_PROMPT


def test_planner_rejects_malformed_browser_action_contract(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Scroll and check.",
        "expected_result": "Control is visible.", "critical": True, "category": "scroll",
        "actions": [
            {"action": "scroll", "count": 0},
            {"action": "assert_visible", "selector": "#control"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="scroll action requires integer y"):
        _validate_planning_bundle(file_comm)


@pytest.mark.parametrize("route", ["https://example.com/catalog", "//example.com", "../catalog", "/a/../catalog", "/catalog?q=x", "/#/catalog"])
def test_planner_rejects_unsafe_browser_route(tmp_path: Path, route: str):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    plan = file_comm.read_ui_verification_plan()
    assert plan is not None
    plan["sprints"][0]["checks"][0]["route"] = route
    file_comm.write_ui_verification_plan(plan)

    with pytest.raises(PlannerValidationError, match="safe same-origin path"):
        _validate_planning_bundle(file_comm)


def test_planner_requires_final_typed_assertion_for_authored_contract(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Activate control.",
        "expected_result": "State changes.", "critical": True, "category": "interaction",
        "actions": [{"action": "click", "selector": "#control"}],
    }]}]})

    with pytest.raises(PlannerValidationError, match="must end with a typed assertion"):
        _validate_planning_bundle(file_comm)


def test_planner_accepts_related_typed_assertions_in_one_action_contract(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Activate control.",
        "expected_result": "State changes.", "critical": True, "category": "interaction",
        "actions": [
            {"action": "click", "selector": "#control"},
            {"action": "assert_visible", "selector": "#control"},
            {"action": "assert_attribute", "selector": "#control", "name": "data-state", "value": "active"},
        ],
    }]}]})

    _validate_planning_bundle(file_comm)


@pytest.mark.parametrize("attribute", ["class", "style"])
def test_planner_rejects_exact_presentation_attribute_as_state_proxy(
    tmp_path: Path, attribute: str
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Focus the control.",
        "expected_result": "Focus is observable.", "critical": True,
        "category": "accessibility", "route": "/",
        "actions": [
            {"action": "key_press", "selector": "#name", "key": "Tab"},
            {"action": "assert_focus", "selector": "#save"},
            {"action": "assert_attribute", "selector": "#save", "name": attribute, "value": "focused"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="exact class/style assertions are forbidden"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_observational_exact_count_without_declared_fixtures(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Inspect catalog.",
        "expected_result": "Catalog has suggestions.", "critical": True,
        "category": "functionality", "route": "/catalog",
        "actions": [
            {"action": "reload"},
            {"action": "wait_for", "selector": ".item", "state": "visible"},
            {"action": "assert_count", "selector": ".item", "count": 1},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="exact count has no state-producing setup or declared fixtures"):
        _validate_planning_bundle(file_comm)


def test_planner_accepts_filter_literal_backed_by_declared_fixture(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Filter catalog.",
        "expected_result": "Matching fixture remains.", "critical": True,
        "category": "functionality", "route": "/catalog",
        "fixtures": ["Dune"],
        "actions": [
            {"action": "fill", "selector": "#filter", "value": "Dune"},
            {"action": "assert_text", "selector": ".item", "value": "Dune", "match": "contains"},
        ],
    }]}]})

    _validate_planning_bundle(file_comm)


def test_planner_rejects_filter_literal_without_declared_fixture(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Filter catalog.",
        "expected_result": "Matching item remains.", "critical": True,
        "category": "functionality", "route": "/catalog",
        "actions": [
            {"action": "fill", "selector": "#filter", "value": "Dune"},
            {"action": "assert_text", "selector": ".item", "value": "Dune", "match": "contains"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="filter assertion literal 'Dune' is not declared in fixtures"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_more_than_four_typed_assertions(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    actions = [
        {"action": "assert_visible", "selector": f"#control-{index}"}
        for index in range(5)
    ]
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Inspect one journey.",
        "expected_result": "Related states are visible.", "critical": True,
        "category": "interaction", "route": "/", "actions": actions,
    }]}]})

    with pytest.raises(PlannerValidationError, match="1 to 4 related typed assertions"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_tab_asserting_focus_remains_on_start(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Tab from save.",
        "expected_result": "Focus advances.", "critical": True, "category": "accessibility",
        "route": "/",
        "actions": [
            {"action": "key_press", "selector": "#save", "key": "Tab"},
            {"action": "assert_focus", "selector": "#save"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="destination selector"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_tab_starting_from_global_container(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Tab into the form.",
        "expected_result": "Title receives focus.", "critical": True,
        "category": "accessibility", "route": "/",
        "actions": [
            {"action": "key_press", "selector": "body", "key": "Tab"},
            {"action": "assert_focus", "selector": "#title"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="Tab start must name a focusable control"):
        _validate_planning_bundle(file_comm)


def test_planner_requires_explicit_storage_string_match_mode(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Check saved item.",
        "expected_result": "Storage contains the title.", "critical": True,
        "category": "persistence", "route": "/",
        "actions": [
            {
                "action": "assert_storage_value",
                "storage": "local",
                "key": "items",
                "value": "Dune",
            },
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="string storage assertions require explicit match"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_initial_empty_state_after_stateful_route_checks(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [
        {
            "id": "UI-001", "feature_id": "F001", "task": "Create item.",
            "expected_result": "Item exists.", "critical": True,
            "category": "functionality", "route": "/",
            "actions": [
                {"action": "fill", "selector": "#name", "value": "Atlas"},
                {"action": "click", "selector": "#create"},
                {"action": "assert_count", "selector": ".item", "count": 1},
            ],
        },
        {
            "id": "UI-002", "feature_id": "F001", "task": "Show initial empty state.",
            "expected_result": "Empty guidance is visible.", "critical": True,
            "category": "empty_state", "route": "/",
            "actions": [
                {"action": "reload"},
                {"action": "assert_visible", "selector": ".empty-state"},
            ],
        },
    ]}]})

    with pytest.raises(PlannerValidationError, match="initial empty-state reload"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_persistence_count_not_established_in_current_sprint(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [
        {
            "id": "UI-001", "feature_id": "F001", "task": "Save from catalog.",
            "expected_result": "One item is visible.", "critical": True,
            "category": "functionality", "route": "/library.html",
            "actions": [
                {"action": "click", "selector": ".save-btn"},
                {"action": "click", "selector": "a[href='/']"},
                {"action": "assert_url", "value": "/"},
                {"action": "assert_visible", "selector": ".item"},
            ],
        },
        {
            "id": "UI-002", "feature_id": "F001", "task": "Reload saved items.",
            "expected_result": "Two items persist.", "critical": True,
            "category": "persistence", "route": "/",
            "actions": [
                {"action": "reload"},
                {"action": "assert_count", "selector": ".item", "count": 2},
            ],
        },
    ]}]})

    with pytest.raises(PlannerValidationError, match="current-sprint setup establishes only 1"):
        _validate_planning_bundle(file_comm)


def test_planner_accepts_persistence_count_established_after_route_transition(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [
        {
            "id": "UI-001", "feature_id": "F001", "task": "Save from catalog.",
            "expected_result": "One item is visible.", "critical": True,
            "category": "functionality", "route": "/library.html",
            "actions": [
                {"action": "click", "selector": ".save-btn"},
                {"action": "click", "selector": "a[href='/']"},
                {"action": "assert_url", "value": "/"},
                {"action": "assert_visible", "selector": ".item"},
            ],
        },
        {
            "id": "UI-002", "feature_id": "F001", "task": "Reload saved item.",
            "expected_result": "One item persists.", "critical": True,
            "category": "persistence", "route": "/",
            "actions": [
                {"action": "reload"},
                {"action": "assert_count", "selector": ".item", "count": 1},
            ],
        },
    ]}]})

    _validate_planning_bundle(file_comm)


def test_planner_rejects_legacy_model_authored_evaluate(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Activate control.",
        "expected_result": "State changes.", "critical": True, "category": "interaction",
        "actions": [{"action": "evaluate", "expression": "true"}],
    }]}]})

    with pytest.raises(PlannerValidationError, match="must end with a typed assertion"):
        _validate_planning_bundle(file_comm)


def test_planner_reports_all_typed_contract_errors_in_one_retry(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [
        {
            "id": "UI-001", "feature_id": "F001", "task": "Check class.",
            "expected_result": "Changed.", "critical": True, "category": "interaction",
            "actions": [{
                "action": "assert_attribute", "selector": "#control",
                "attribute": "class", "match": "contains", "value": "active",
            }],
        },
        {
            "id": "UI-002", "feature_id": "F001", "task": "Check URL.",
            "expected_result": "Changed.", "critical": True, "category": "interaction",
            "actions": [{"action": "assert_url", "url": "/done"}],
        },
    ]}]})

    with pytest.raises(PlannerValidationError) as caught:
        _validate_planning_bundle(file_comm)

    message = str(caught.value)
    assert "UI-001" in message and "unsupported fields: attribute, match" in message
    assert "UI-002" in message and "unsupported fields: url" in message


def test_planner_rejects_invalid_action_settle_time(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Type then check.",
        "expected_result": "State changes.", "critical": True, "category": "interaction",
        "actions": [
            {"action": "fill", "selector": "#control", "value": "x", "settle_ms": 9000},
            {"action": "assert_visible", "selector": "#control"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="settle_ms must be an integer from 0 to 5000"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_unsupported_browser_action_before_evaluation(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Use unsupported action.",
        "expected_result": "State changes.", "critical": True, "category": "interaction",
        "actions": [
            {"action": "teleport", "selector": "#control"},
            {"action": "assert_visible", "selector": "#control"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="unsupported action 'teleport'"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_unsafe_upload_fixture_contract(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Upload fixture.",
        "expected_result": "File is accepted.", "critical": True, "category": "interaction",
        "actions": [
            {
                "action": "set_input_files",
                "selector": "#upload",
                "files": [{"name": "../secret.txt", "mime_type": "text/plain", "content": "x"}],
            },
            {"action": "assert_visible", "selector": "#upload"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="safe base name"):
        _validate_planning_bundle(file_comm)


@pytest.mark.anyio
async def test_planner_initializes_accepted_sprints_after_successful_run(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        _write_valid_planning_bundle(file_comm)
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                usage={"input_tokens": 100_000},
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    stats = await run_planner(
        HarnessConfig(planner_model="claude-sonnet-4-6"),
        "build a counter app",
        file_comm,
        tmp_path,
    )

    # claude-sonnet-4-6 at $3 per 1M input tokens
    # → 100_000 * 3 / 1e6 = $0.30.
    assert stats.cost_usd == 0.3
    assert stats.duration_ms == 1
    assert file_comm.read_spec().startswith("# Counter App - Track Every Tap")
    assert file_comm.read_accepted_sprints() == {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 0,
    }


@pytest.mark.anyio
async def test_planner_raises_when_planning_bundle_is_missing_required_artifact(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_spec(_valid_spec_text())
        file_comm.write_progress("# Progress")
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(PlannerValidationError, match="design_tokens.json"):
        await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)


@pytest.mark.anyio
async def test_planner_raises_when_planning_bundle_is_malformed(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_spec(_valid_spec_text())
        file_comm.write_design_tokens(
            {
                "theme_name": "editorial counter",
                "color": {"bg": "#111111"},
                "typography": {"display": "Space Grotesk"},
                "spacing": {"base": 8},
                "radius": {"card": 16},
                "motion": {"duration_fast": 160},
                "style_rules": ["bold hierarchy"],
                "anti_patterns": ["generic cards"],
                "visual_experiment": {
                    "design_hypothesis": "Use poster-like asymmetry.",
                    "reason_for_image_first": "Text-only outputs stay too templated.",
                    "desired_break_from_web_templates": ["poster-like asymmetry"],
                    "visual_opportunities_beyond_css": ["ink texture"],
                    "forbidden_generic_patterns": ["centered card grid"],
                },
            }
        )
        file_comm.write_feature_list({"features": []})
        file_comm.write_sprint_plan(
            {
                "total_sprints": 1,
                "sprints": [
                    {
                        "number": 1,
                        "title": "Core counter",
                        "goal": "Ship the primary counter flow.",
                        "feature_ids": ["F001"],
                        "deliverables": ["Visible counter UI."],
                        "exit_criteria": ["Counter increments correctly."],
                    }
                ],
            }
        )
        file_comm.write_ui_verification_plan(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-001",
                                "feature_id": "F001",
                                "task": "Click increment once.",
                                "expected_result": "Counter changes by one step.",
                                "critical": True,
                                "category": "core_interaction",
                            }
                        ],
                    }
                ]
            }
        )
        file_comm.write_progress("# Progress")
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(PlannerValidationError, match="F001"):
        await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)


@pytest.mark.anyio
async def test_planner_rejects_missing_visual_experiment(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_spec(_valid_spec_text())
        (file_comm.dir / "design_tokens.json").write_text(
            json.dumps(
                {
                    "theme_name": "editorial counter",
                    "color": {"bg": "#111111"},
                    "typography": {"display": "Space Grotesk"},
                    "spacing": {"base": 8},
                    "radius": {"card": 16},
                    "motion": {"duration_fast": 160},
                    "style_rules": ["bold hierarchy"],
                    "anti_patterns": ["generic cards"],
                    "visual_experiment": {},
                }
            ),
            encoding="utf-8",
        )
        file_comm.write_feature_list(
            {
                "features": [
                    {
                        "id": "F001",
                        "name": "Counter",
                        "priority": "high",
                        "depends_on": [],
                        "description": "Count values.",
                        "acceptance_criteria": ["Counter increments correctly."],
                        "status": "planned",
                        "sprint": 1,
                    }
                ]
            }
        )
        file_comm.write_sprint_plan(
            {
                "total_sprints": 1,
                "sprints": [
                    {
                        "number": 1,
                        "title": "Core counter",
                        "goal": "Ship the primary counter flow.",
                        "feature_ids": ["F001"],
                        "deliverables": ["Visible counter UI."],
                        "exit_criteria": ["Counter increments correctly."],
                    }
                ],
            }
        )
        file_comm.write_ui_verification_plan(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-001",
                                "feature_id": "F001",
                                "task": "Click increment once.",
                                "expected_result": "Counter changes by one step.",
                                "critical": True,
                                "category": "core_interaction",
                            }
                        ],
                    }
                ]
            }
        )
        file_comm.write_progress("# Progress")
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(PlannerValidationError, match="schema validation"):
        await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)


@pytest.mark.anyio
async def test_planner_prompt_explicitly_forbids_bash_and_uses_precreated_artifacts(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    captured: dict[str, str] = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        captured["system_prompt"] = kwargs["system_prompt"]
        captured["stop_hooks"] = kwargs["stop_hooks"]
        assert file_comm.read_spec().startswith("# Draft Product - Working Title")
        assert file_comm.read_progress() == "# Progress Log\n"
        assert (file_comm.dir / "design_tokens.json").exists() is True
        assert (file_comm.dir / "feature_list.json").exists() is True
        assert (file_comm.dir / "sprint_plan.json").exists() is True
        assert (file_comm.dir / "ui_verification_plan.json").exists() is True
        _write_valid_planning_bundle(file_comm)
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)

    assert "Bash is unavailable for this task." in captured["prompt"]
    assert "The Harness has already prepared the workdir, the .harness directory, and the required artifact files." in captured["prompt"]
    assert "Replace the scaffold content in those files" in captured["prompt"]
    assert "`Bash` is unavailable for this task." in captured["system_prompt"]
    assert "The Harness prepares the workdir, the `.harness/` directory, and the required artifact" in captured["system_prompt"]
    assert "Use `total_sprints` exactly as written, never `total_sprint`." in captured["system_prompt"]
    assert "Every sprint entry must include at least one item in `feature_ids`" in captured["system_prompt"]
    assert "ensure all six required artifacts were written" in captured["system_prompt"]
    assert len(captured["stop_hooks"]) == 1


@pytest.mark.anyio
async def test_planner_prepares_missing_workdir_and_harness_dir(monkeypatch, tmp_path: Path):
    workdir = tmp_path / "missing-workdir"
    file_comm = FileComm(workdir / ".harness")
    harness_dir = workdir / ".harness"
    if harness_dir.exists():
        harness_dir.rmdir()
    if workdir.exists():
        workdir.rmdir()

    async def fake_run_sdk_agent(**kwargs):
        assert workdir.exists() is True
        assert harness_dir.exists() is True
        assert (harness_dir / "spec.md").exists() is True
        assert (harness_dir / "design_tokens.json").exists() is True
        assert (harness_dir / "feature_list.json").exists() is True
        assert (harness_dir / "sprint_plan.json").exists() is True
        assert (harness_dir / "ui_verification_plan.json").exists() is True
        assert (harness_dir / "progress.md").exists() is True
        _write_valid_planning_bundle(file_comm)
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    await run_planner(HarnessConfig(), "build a counter app", file_comm, workdir)

    assert workdir.exists() is True
    assert harness_dir.exists() is True


@pytest.mark.anyio
async def test_planner_stop_hook_blocks_invalid_bundle(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.initialize_planning_artifacts()
    hook = _make_planner_stop_hook(file_comm, HarnessConfig())

    result = await hook({}, None, None)

    assert result["decision"] == "block"
    assert "Planning artifact validation failed" in result["reason"]
    assert ".harness/spec.md" in result["reason"]


@pytest.mark.anyio
async def test_planner_stop_hook_allows_valid_bundle(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    hook = _make_planner_stop_hook(file_comm, HarnessConfig())

    result = await hook({}, None, None)

    assert result == {"decision": "complete"}


# --- cross-ref consistency between the three plan files ---


def _seed_valid_bundle(file_comm: FileComm) -> None:
    """Write a self-consistent planning bundle with two features and two sprints."""
    file_comm.write_spec(_valid_spec_text())
    file_comm.write_design_tokens(
        {
            "theme_name": "editorial counter",
            "color": {"bg": "#111111"},
            "typography": {"display": "Space Grotesk"},
            "spacing": {"base": 8},
            "radius": {"card": 16},
            "motion": {"duration_fast": 160},
            "style_rules": ["bold hierarchy"],
            "anti_patterns": ["generic cards"],
            "visual_experiment": {
                "design_hypothesis": "Use poster-like asymmetry.",
                "reason_for_image_first": "Text-only outputs stay too templated.",
                "desired_break_from_web_templates": ["poster-like asymmetry"],
                "visual_opportunities_beyond_css": ["ink texture"],
                "forbidden_generic_patterns": ["centered card grid"],
            },
        }
    )
    file_comm.write_feature_list(
        {
            "features": [
                {
                    "id": "F001",
                    "name": "Counter",
                    "priority": "high",
                    "depends_on": [],
                    "description": "Count values.",
                    "acceptance_criteria": ["Counter increments correctly."],
                    "status": "planned",
                    "sprint": 1,
                },
                {
                    "id": "F002",
                    "name": "Polish",
                    "priority": "medium",
                    "depends_on": ["F001"],
                    "description": "Animate.",
                    "acceptance_criteria": ["Animation runs."],
                    "status": "planned",
                    "sprint": 2,
                },
            ]
        }
    )
    file_comm.write_sprint_plan(
        {
            "total_sprints": 2,
            "sprints": [
                {
                    "number": 1,
                    "title": "Core counter",
                    "goal": "Ship the primary counter flow.",
                    "feature_ids": ["F001"],
                    "deliverables": ["Visible counter UI."],
                    "exit_criteria": ["Counter increments correctly."],
                },
                {
                    "number": 2,
                    "title": "Polish",
                    "goal": "Add motion.",
                    "feature_ids": ["F002"],
                    "deliverables": ["Animated counter."],
                    "exit_criteria": ["Animation runs."],
                },
            ],
        }
    )
    file_comm.write_ui_verification_plan(
        {
            "sprints": [
                {
                    "sprint": 1,
                    "checks": [
                        {
                            "id": "UI-001",
                            "feature_id": "F001",
                            "task": "Click increment once.",
                            "expected_result": "Counter changes by one step.",
                            "critical": True,
                            "category": "core_interaction",
                        }
                    ],
                },
                {
                    "sprint": 2,
                    "checks": [
                        {
                            "id": "UI-002",
                            "feature_id": "F002",
                            "task": "Observe animation.",
                            "expected_result": "Counter animates.",
                            "critical": False,
                            "category": "appearance",
                        }
                    ],
                },
            ]
        }
    )
    file_comm.write_progress("# Progress Log\n\n## planning\n- status: complete")


def test_validate_planning_bundle_passes_for_consistent_bundle(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)
    # No exception → bundle is internally consistent.
    _validate_planning_bundle(file_comm)
    assert file_comm.read_sprint_plan()["total_sprints"] == 2


def test_validate_planning_bundle_rejects_dangling_sprint_feature_id(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["feature_ids"] = ["F999"]  # not in feature_list
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match="F999"):
        _validate_planning_bundle(file_comm)


def test_validate_planning_bundle_rejects_dangling_ui_check_feature_id(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    plan = file_comm.read_ui_verification_plan()
    plan["sprints"][0]["checks"][0]["feature_id"] = "F404"
    file_comm.write_ui_verification_plan(plan)

    with pytest.raises(PlannerValidationError, match="F404"):
        _validate_planning_bundle(file_comm)


def test_validate_planning_bundle_rejects_feature_assigned_to_unknown_sprint(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    feature_list = file_comm.read_feature_list()
    feature_list["features"][0]["sprint"] = 99  # outside total_sprints=2
    file_comm.write_feature_list(feature_list)

    with pytest.raises(PlannerValidationError, match="sprint"):
        _validate_planning_bundle(file_comm)


def test_validate_planning_bundle_rejects_sprint_plan_missing_a_sprint_number(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    # total_sprints=2 but sprint_plan only declares sprint 1.
    sprint_plan["sprints"] = [sprint_plan["sprints"][0]]
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match="sprint"):
        _validate_planning_bundle(file_comm)


# --- sprint sizing caps ---


def test_validate_sprint_plan_rejects_too_many_deliverables(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["deliverables"] = [f"Deliverable {i}" for i in range(6)]
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match=r"deliverables.*max allowed is 5"):
        _validate_planning_bundle(
            file_comm, HarnessConfig(max_deliverables_per_sprint=5)
        )


def test_validate_sprint_plan_rejects_too_many_exit_criteria(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["exit_criteria"] = [f"Criterion {i}" for i in range(6)]
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match=r"exit_criteria.*max allowed is 5"):
        _validate_planning_bundle(
            file_comm, HarnessConfig(max_exit_criteria_per_sprint=5)
        )


def test_validate_sprint_plan_accepts_at_cap(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["deliverables"] = [f"D{i}" for i in range(5)]
    sprint_plan["sprints"][0]["exit_criteria"] = [f"C{i}" for i in range(5)]
    file_comm.write_sprint_plan(sprint_plan)

    _validate_planning_bundle(file_comm)


def test_validate_sprint_plan_respects_config_override_for_caps(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["deliverables"] = [f"D{i}" for i in range(8)]
    file_comm.write_sprint_plan(sprint_plan)

    relaxed = HarnessConfig(max_deliverables_per_sprint=8)
    _validate_planning_bundle(file_comm, relaxed)


def test_expansive_data_enforces_three_item_sprint_cap(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)
    sprint_plan = file_comm.read_sprint_plan()
    feature_list = file_comm.read_feature_list()
    feature_id = feature_list["features"][0]["id"]
    sprint_plan["total_sprints"] = 6
    template = sprint_plan["sprints"][0]
    sprint_plan["sprints"] = []
    for number in range(1, 7):
        item = dict(template)
        item["number"] = number
        item["feature_ids"] = [feature_id]
        item["deliverables"] = [f"D{i}" for i in range(5)]
        sprint_plan["sprints"].append(item)
    file_comm.write_sprint_plan(sprint_plan)
    feature_list["features"][0]["sprint"] = 1
    file_comm.write_feature_list(feature_list)
    verification = file_comm.read_ui_verification_plan()
    verification["sprints"] = [verification["sprints"][0]]
    file_comm.write_ui_verification_plan(verification)

    config = HarnessConfig(planner_scope_mode="expansive-data")
    with pytest.raises(PlannerValidationError, match=r"deliverables.*max allowed is 3"):
        _validate_planning_bundle(file_comm, config)


def test_planner_prompt_documents_sprint_size_caps():
    from src.prompts.planner import PLANNER_SYSTEM_PROMPT

    # Bounded caps remain visible while allowing one coherent multi-page slice.
    assert "10 concise deliverables" in PLANNER_SYSTEM_PROMPT
    assert "10 concise exit_criteria" in PLANNER_SYSTEM_PROMPT
    assert "vertical slice" in PLANNER_SYSTEM_PROMPT.lower()


def test_expansive_data_scope_uses_shallow_natural_sprint_expansion():
    from src.prompts.planner import planner_system_prompt

    prompt = planner_system_prompt("expansive-data")
    assert "6-9 dependency-ordered Sprints" in prompt
    assert "2-3 closely related user-visible deliverables" in prompt
    assert 'standalone\n   "polish/refactor/cleanup" Sprint' in prompt
    assert "generate/edit" in prompt


def test_query_aligned_scope_does_not_enable_legacy_expansion():
    from src.prompts.planner import planner_system_prompt

    prompt = planner_system_prompt("query-aligned")
    assert "Scope Profile: Expansive Data Construction" not in prompt
