from __future__ import annotations

import json
from pathlib import Path

from src.orchestration.atomic_edit_plan import write_atomic_edit_plan
from src.orchestration.edit_risk_tests import (
    materialize_edit_risk_tests, distinguish_preexisting_risks,
    _restore_split_flow_prefixes, _setup_prefix, _target_selector, _ranked_candidates,
)
from src.orchestration.edit_task_contract import prepare_edit_task_contract
from src.orchestration.hidden_oracle_checks import (
    read_hidden_oracle_checks,
    write_hidden_oracle_checks,
)
from src.orchestration.webcompass_protocol import REPAIR_TYPES


INSTRUCTION = (
    "Add a responsive dark-theme modal with a form, image preview, and Save button "
    "on both the dashboard and settings pages."
)


def test_risk_setup_restores_split_states_and_checks_controls_before_click():
    opened = [
        {"action": "click", "selector": "#workspace-open"},
        {"action": "assert_visible", "selector": "#workspace"},
        {"action": "click", "selector": "#card"},
        {"action": "assert_visible", "selector": "#detail"},
    ]
    checks = [
        {"id": "detail__part1", "route": "/", "actions": opened},
        {"id": "detail__part2", "route": "/", "actions": [{"action": "assert_text", "selector": "#internal", "value": "Ready"}]},
        {"id": "detail__part3", "route": "/", "actions": [{"action": "click", "selector": "#close"}, {"action": "assert_hidden", "selector": "#detail"}, {"action": "assert_visible", "selector": "#workspace"}]},
        {"id": "independent", "route": "/", "actions": [{"action": "assert_visible", "selector": "#public"}]},
    ]
    restored = _restore_split_flow_prefixes(checks)
    assert _setup_prefix(restored[1]) == opened
    assert _target_selector(restored[2]) == "#workspace"
    assert _setup_prefix(restored[3]) == []
    candidates = _ranked_candidates(instruction_delta="Open an event detail", plan={"checks": checks}, source={}, routes=["/"])
    card = next(item for item in candidates if item["repair_type"] == "Loss of Interactivity" and item["selector"] == "#card")
    assert card["setup_actions"] == opened[:2]
    close = next(item for item in candidates if item["repair_type"] == "Loss of Interactivity" and item["selector"] == "#close")
    assert close["setup_actions"][-1]["selector"] == "#internal"
    internal = next(item for item in candidates if item["selector"] == "#internal")
    assert internal["setup_actions"] == opened


def test_hidden_target_is_audited_before_close_and_other_routes_are_isolated():
    checks = [{"id": "flow__part1", "route": "/", "actions": [
        {"action": "click", "selector": "#open"},
        {"action": "assert_visible", "selector": "#detail"},
        {"action": "click", "selector": "#close"},
        {"action": "assert_hidden", "selector": "#detail"},
    ]}, {"id": "flow__part2", "route": "/other.html", "actions": [{"action": "assert_visible", "selector": "#other"}]}]
    restored = _restore_split_flow_prefixes(checks)
    assert _setup_prefix(restored[0]) == [{"action": "click", "selector": "#open"}]
    assert _setup_prefix(restored[1]) == []


def test_static_assertion_is_not_a_loss_of_interactivity_candidate():
    candidates = _ranked_candidates(
        instruction_delta="Add a page and link it from the header.",
        plan={"checks": [{
            "id": "title",
            "route": "/new.html",
            "actions": [{"action": "assert_text", "selector": "#title", "value": "New"}],
        }]},
        source={},
        routes=["/new.html"],
    )
    assert not any(item["repair_type"] == "Loss of Interactivity" for item in candidates)


def test_link_assertion_can_be_a_loss_of_interactivity_candidate():
    candidates = _ranked_candidates(
        instruction_delta="Add a page and link it from the header.",
        plan={"checks": [{
            "id": "link",
            "route": "/",
            "actions": [{"action": "assert_visible", "selector": "a[href='new.html']"}],
        }]},
        source={},
        routes=["/"],
    )
    assert any(item["repair_type"] == "Loss of Interactivity" for item in candidates)


def test_risk_groups_reuse_only_identical_route_and_setup(tmp_path: Path):
    _prepared(tmp_path)
    materialize_edit_risk_tests(workdir=tmp_path, instruction_delta=INSTRUCTION, max_checks=11)
    checks = read_hidden_oracle_checks(tmp_path / ".harness")
    groups = {}
    for check in checks:
        base, _part = check["id"].rsplit("__part", 1)
        groups.setdefault(base, []).append(check)
    for group in groups.values():
        assert len({check["route"] for check in group}) == 1
        assert group[0]["id"].endswith("__part1")
        prefixes = [check["actions"][:-1] for check in group]
        assert all(prefix == prefixes[0] for prefix in prefixes)
    assert len(checks) == 11
    assert {check["repair_type"] for check in checks} <= REPAIR_TYPES
    assert all(check["actions"][-1]["action"] == "assert_webcompass_risk" for check in checks)


def test_risk_group_collision_keeps_split_suffix_and_shared_base(tmp_path: Path):
    _prepared(tmp_path)
    materialize_edit_risk_tests(workdir=tmp_path, instruction_delta=INSTRUCTION, max_checks=11)
    base = next(c["id"].rsplit("__part", 1)[0] for c in read_hidden_oracle_checks(tmp_path / ".harness") if c["id"].endswith("__part2"))
    write_hidden_oracle_checks(tmp_path / ".harness", [{"id": base + "__part1", "route": "/", "actions": [{"action": "assert_visible", "selector": "body"}]}], target_routes=["/", "/settings.html"])
    materialize_edit_risk_tests(workdir=tmp_path, instruction_delta=INSTRUCTION, max_checks=11)
    generated = [c for c in read_hidden_oracle_checks(tmp_path / ".harness") if c.get("origin") == "source_edit_risk_analysis"]
    assert any(c["id"] == base + "-AUTO__part1" for c in generated)
    assert any(c["id"] == base + "-AUTO__part2" for c in generated)


def test_default_risk_checks_are_bounded_globally(tmp_path: Path):
    _prepared(tmp_path)
    artifact = materialize_edit_risk_tests(
        workdir=tmp_path, instruction_delta=INSTRUCTION
    )
    checks = [
        check for check in read_hidden_oracle_checks(tmp_path / ".harness")
        if check.get("origin") == "source_edit_risk_analysis"
    ]
    assert len(artifact["generated_check_ids"]) == 0
    assert {check["route"] for check in checks} <= {"/", "/settings.html"}


def _plan() -> dict:
    checks = []
    for index, route in enumerate(("/", "/settings.html"), start=1):
        checks.append({
            "id": f"UI-MODAL-{index}",
            "task": "Open and inspect the responsive modal form.",
            "expected_result": "The dialog is usable without changing the surrounding layout.",
            "critical": True,
            "category": "responsive interaction layout",
            "requirement_id": "REQ-MODAL",
            "impact_tags": ["modal", "form", "dark-theme", f"route:{route}"],
            "route": route,
            "fixtures": [],
            "actions": [
                {"action": "set_viewport", "width": 390, "height": 844},
                {"action": "click", "selector": "[data-testid='open-modal']"},
                {"action": "assert_visible", "selector": "[data-testid='edit-modal']"},
            ],
        })
    return {
        "schema_version": "atomic-edit-plan-v1",
        "title": "Add responsive modal",
        "goal": INSTRUCTION,
        "source_anchors": ["dashboard", "settings"],
        "deliverables": ["A responsive modal form with image preview."],
        "exit_criteria": ["The modal is operable on both routes."],
        "requirement_changes": [{
            "requirement_id": "REQ-MODAL",
            "relation": "add",
            "prior_requirement_ids": [],
            "rationale": INSTRUCTION,
        }],
        "impact_tags": ["modal", "responsive", "form", "dark-theme"],
        "unresolved_conflicts": [],
        "visual_evidence": "required",
        "visual_evidence_reason": "The modal changes responsive layout and theme styling.",
        "checks": checks,
    }


def _prepared(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        "<main><button data-testid='open-modal'>Open</button></main>", encoding="utf-8"
    )
    (frontend / "settings.html").write_text(
        "<main><form><button data-testid='open-modal'>Open</button></form></main>",
        encoding="utf-8",
    )
    prepare_edit_task_contract(
        tmp_path, requested_target_routes=["/", "/settings.html"]
    )
    write_atomic_edit_plan(
        tmp_path / ".harness", _plan(), instruction_delta=INSTRUCTION
    )


def test_source_edit_risk_analysis_selects_relevant_subset_and_covers_routes(
    tmp_path: Path,
):
    _prepared(tmp_path)

    profile = materialize_edit_risk_tests(
        workdir=tmp_path, instruction_delta=INSTRUCTION, max_checks=6
    )
    checks = read_hidden_oracle_checks(tmp_path / ".harness")

    assert 2 <= len(checks) <= 6
    assert {check["route"] for check in checks} == {"/", "/settings.html"}
    assert {check["repair_type"] for check in checks} <= REPAIR_TYPES
    assert "Loss of Interactivity" in {check["repair_type"] for check in checks}
    assert all(check["origin"] == "source_edit_risk_analysis" for check in checks)
    assert all(
        check["actions"][-1]["action"] == "assert_webcompass_risk"
        for check in checks
    )
    assert len(profile["risk_decisions"]) == 2 * len(REPAIR_TYPES)
    assert sum(item["selected"] for item in profile["risk_decisions"]) == len(checks)


def test_source_edit_risk_analysis_preserves_user_hidden_oracle(tmp_path: Path):
    _prepared(tmp_path)
    write_hidden_oracle_checks(
        tmp_path / ".harness",
        [{
            "id": "USER-ORACLE",
            "route": "/",
            "actions": [{"action": "assert_hash", "value": ""}],
        }],
        target_routes=["/", "/settings.html"],
    )

    materialize_edit_risk_tests(
        workdir=tmp_path, instruction_delta=INSTRUCTION, max_checks=4
    )
    checks = read_hidden_oracle_checks(tmp_path / ".harness")

    assert checks[0]["id"] == "USER-ORACLE"
    assert len(checks) == 5


def test_risk_profile_contains_no_candidate_or_target_implementation(tmp_path: Path):
    _prepared(tmp_path)

    profile = materialize_edit_risk_tests(
        workdir=tmp_path, instruction_delta=INSTRUCTION
    )
    raw = json.dumps(profile, ensure_ascii=False).casefold()

    assert profile["status"] == "authored_before_build"
    assert profile["baseline_commit"]
    assert "candidate_implementation" not in raw
    assert "target_screenshot" not in raw


def test_default_defect_audit_is_not_multiplied_across_target_scenes(tmp_path: Path):
    _prepared(tmp_path)
    profile = materialize_edit_risk_tests(workdir=tmp_path, instruction_delta="Update this surface")
    checks = read_hidden_oracle_checks(tmp_path / ".harness")
    assert profile["test_policy"] == "webcompass_defects_first"
    assert len(checks) == 0
    assert {item["route"] for item in checks} <= {"/", "/settings.html"}


def test_multiple_target_states_are_not_collapsed_to_first_route_check(tmp_path: Path):
    _prepared(tmp_path)
    plan = _plan()
    additional = json.loads(json.dumps(plan["checks"][0]))
    additional["id"] = "UI-SECOND-STATE"
    additional["actions"][-1]["selector"] = "[data-testid='second-modal']"
    plan["checks"].append(additional)
    write_atomic_edit_plan(tmp_path / ".harness", plan, instruction_delta=INSTRUCTION)
    materialize_edit_risk_tests(
        workdir=tmp_path, instruction_delta=INSTRUCTION, max_checks=11
    )
    checks = read_hidden_oracle_checks(tmp_path / ".harness")
    assert any(item["actions"][-1]["selector"] == "[data-testid='second-modal']" for item in checks)


def test_source_risks_preserve_new_worsened_duplicate_and_invalid_findings():
    old = {"kind": "wcag-text-contrast", "element_path": "#label", "ratio": 3.2}
    def record(issues):
        return {"check_id": "risk", "status": "action_failed", "steps": [{
            "action": "assert_webcompass_risk", "ok": False,
            "output": {"actual": {"issues": issues, "passed": False}}}]}
    baseline = {"checks": [record([old])], "status": "observed_source_initial_state"}
    for issues, expected_remaining in [([old], []),
            ([old, old], [old]),
            ([dict(old, ratio=2.5)], [dict(old, ratio=2.5)]),
            ([dict(old, element_path="#new")], [dict(old, element_path="#new")])]:
        evidence = {"checks": [record(issues)]}
        distinguish_preexisting_risks(evidence, baseline)
        item = evidence["checks"][0]
        assert item["steps"][0]["output"]["actual"]["issues"] == expected_remaining
        assert (item["status"] == "ok") == (not expected_remaining)
    invalid = {"checks": [{"check_id": "risk", "status": "invalid_test_contract",
                           "steps": [{"action": "assert_webcompass_risk", "ok": False, "error": "bad selector"}]}]}
    distinguish_preexisting_risks(invalid, baseline)
    assert invalid["checks"][0]["status"] == "invalid_test_contract"


def test_applicable_states_cover_classes_and_intermediate_targets(tmp_path):
    _prepared(tmp_path)
    path = tmp_path / '.harness/atomic_edit_plan.json'
    plan = __import__('json').loads(path.read_text())
    plan['checks'][0]['actions'] += [
        {'action': 'click', 'selector': '#next'},
        {'action': 'assert_text', 'selector': '#result', 'value': 'Saved'},
    ]
    path.write_text(__import__('json').dumps(plan))
    materialize_edit_risk_tests(workdir=tmp_path, instruction_delta=INSTRUCTION, applicable_states=True)
    checks = read_hidden_oracle_checks(tmp_path / '.harness')
    assert {c['repair_type'] for c in checks} == REPAIR_TYPES
    targets = {c['actions'][-1]['selector'] for c in checks}
    assert "[data-testid='edit-modal']" in targets
    assert '#result' in targets
    assert all(not any(a['action'].startswith('assert_') for a in c['actions'][:-1]) for c in checks)
