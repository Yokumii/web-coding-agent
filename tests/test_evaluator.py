from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import ResultMessage

from src.agents.evaluator import (
    _bounded_edit_diff,
    _determine_passed,
    _extract_grades_from_response,
    _contract_only_route_eligible,
    _normalize_contract_grades,
    _run_contract_only_evaluator,
    _typed_pass_route_eligible,
    build_deterministic_pass_grades,
    build_deterministic_failure_grades,
    run_evaluator,
)
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.sprint_state import SprintState


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _write_evaluator_context(file_comm: FileComm) -> None:
    file_comm.write_spec("# Product Spec\n\n## Product Overview\nBrowser app.")
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
                    "status": "in_progress",
                    "sprint": 1,
                }
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
                    "title": "Polish counter",
                    "goal": "Improve presentation quality.",
                    "feature_ids": ["F002"],
                    "deliverables": ["Improve layout and polish."],
                    "exit_criteria": ["Layout is stable across breakpoints."],
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
                }
            ]
        }
    )
    file_comm.write_accepted_sprints(
        {
            "accepted": [],
            "current_target": 1,
            "last_evaluated_round": 0,
        }
    )


def _passing_grades(round_num: int = 1) -> dict:
    return {
        "round": round_num,
        "sprint": 1,
        "mode_recommendation": "generate_next_sprint",
        "phase_results": {
            "render_gate": "pass",
            "ui_functionality": "pass",
            "appearance": "pass",
            "source_inspection": "skipped",
        },
        "sprint_passed": True,
        "regression_passed": True,
        "overall_passed": True,
        "criteria": {
            "design_quality": {"score": 7.0, "passed": True, "notes": "Distinct visual identity."},
            "functionality": {"score": 8.0, "passed": True, "notes": "Core task works."},
            "originality": {"score": 6.0, "passed": True, "notes": "Intentional choices."},
            "craft": {"score": 7.0, "passed": True, "notes": "Polished layout."},
        },
        "target_exit_criteria_results": [
            {
                "criterion_id": "EXIT-01-01",
                "feature_id": "F001",
                "critical": True,
                "criterion": "Counter increments correctly.",
                "passed": True,
                "notes": "Verified in browser.",
            }
        ],
        "ui_checks": [
            {
                "check_id": "UI-001",
                "feature_id": "F001",
                "critical": True,
                "task": "Click increment once.",
                "expected_result": "Counter changes by one step.",
                "status": "pass",
                "notes": "Worked in browser.",
            }
        ],
        "appearance_review": {
            "screenshots": [f"round_{round_num}_home.png"],
            "render_stability": 4,
            "content_relevance": 4,
            "layout_harmony": 4,
            "modernness_memorability": 4,
            "token_adherence": 4,
            "notes": "Solid visual consistency.",
        },
        "bugs_found": [],
        "regressions_found": [],
        "missing_features": [],
        "repair_instructions": [],
        "edit_scope_audit": "pass",
    }


def test_contract_grade_normalization_repairs_provider_schema_drift():
    raw = {
        "round": 1,
        "sprint": "Back to Top Button",
        "phase_results": {"phase_1": {"passed": False, "notes": "bad"}},
        "criteria": {},
        "ui_checks": [],
    }
    evidence = {"checks": [{"check_id": "UI-001", "status": "action_failed"}]}
    checks = [{
        "id": "UI-001", "feature_id": "F001", "critical": True,
        "task": "Click it", "expected_result": "It works",
    }]

    grades = _normalize_contract_grades(
        raw, round_num=1, sprint_num=1,
        sprint_context={"exit_criteria": ["It works"]}, ui_checks=checks,
        evidence=evidence, edit_guard={"passed": True},
    )

    assert grades["sprint"] == 1
    assert grades["phase_results"]["ui_functionality"] == "fail"
    assert grades["ui_checks"][0]["status"] == "fail"
    assert grades["overall_passed"] is False


def test_contract_grade_normalization_preserves_semantic_failure():
    grades = _normalize_contract_grades(
        {
            "overall_passed": False,
            "criteria": {
                "functionality": {
                    "score": 4,
                    "passed": False,
                    "notes": "The filter clause is not evidenced.",
                }
            },
        },
        round_num=1,
        sprint_num=1,
        sprint_context={"exit_criteria": ["Filter totals by route"]},
        ui_checks=[{"id": "UI-001", "critical": True}],
        evidence={"checks": [{"check_id": "UI-001", "status": "ok"}]},
        edit_guard={"passed": True},
    )

    assert grades["ui_checks"][0]["status"] == "pass"
    assert grades["criteria"]["functionality"]["passed"] is False
    assert grades["target_exit_criteria_results"][0]["passed"] is False
    assert grades["overall_passed"] is False


def test_contract_grade_normalization_keeps_only_observed_post_hoc_repair_types():
    grades = _normalize_contract_grades(
        {
            "overall_passed": False,
            "criteria": {"functionality": {"passed": False, "score": 4}},
            "repair_task_descriptions": [
                {
                    "task_type": "Loss of Interactivity",
                    "description": "The save button does not respond.",
                    "evidence_ids": ["UI-001"],
                },
                {
                    "task_type": "Invented Category",
                    "description": "Unsupported label.",
                    "evidence_ids": ["UI-001"],
                },
                {
                    "task_type": "Overflow",
                    "description": "No failed evidence supports this.",
                    "evidence_ids": ["UI-999"],
                },
            ],
        },
        round_num=1,
        sprint_num=1,
        sprint_context={"exit_criteria": ["Save works"]},
        ui_checks=[{"id": "UI-001", "critical": True}],
        evidence={"checks": [{"check_id": "UI-001", "status": "action_failed"}]},
        edit_guard={"passed": True},
    )

    assert grades["repair_task_descriptions"] == [{
        "task_type": "Loss of Interactivity",
        "description": "The save button does not respond.",
        "evidence_ids": ["UI-001"],
    }]


def test_contract_grade_normalization_treats_subthreshold_functionality_as_failure():
    grades = _normalize_contract_grades(
        {
            "overall_passed": True,
            "criteria": {
                "functionality": {
                    "score": 5,
                    "passed": True,
                    "notes": "Some semantic evidence is incomplete.",
                }
            },
        },
        round_num=1,
        sprint_num=1,
        sprint_context={"exit_criteria": ["Filter totals by route"]},
        ui_checks=[{"id": "UI-001", "critical": True}],
        evidence={"checks": [{"check_id": "UI-001", "status": "ok"}]},
        edit_guard={"passed": True},
    )

    assert grades["criteria"]["functionality"]["passed"] is False
    assert grades["overall_passed"] is False


def test_reproduced_browser_failure_builds_zero_cost_grades(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "browser_evidence_round_1.json").write_text(
        '{"checks":[{"check_id":"UI-001","status":"action_failed"}]}'
    )

    passed, grades, stats = build_deterministic_failure_grades(
        file_comm=file_comm,
        round_num=1,
        sprint_num=1,
        sprint_context={"exit_criteria": ["Archive state changes"]},
        ui_checks=[{
            "id": "UI-001",
            "feature_id": "F001",
            "critical": True,
            "task": "Archive item",
            "expected_result": "Item leaves active list",
        }],
        edit_guard={"passed": True},
    )

    assert passed is False
    assert grades["evidence_route"]["llm_evaluator_called"] is False
    assert grades["bugs_found"] == ["Deterministic browser contract failed: UI-001"]
    assert stats.cost_usd == 0


def test_complete_behavior_only_contract_can_take_zero_cost_pass_route(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "edit_card.json").write_text(json.dumps({
        "schema_version": "edit-card-v1",
        "visual_evidence": "not_required",
    }))
    checks = [{
        "id": "UI-001", "feature_id": "F001", "critical": True,
        "task": "Use the control", "expected_result": "Status changes",
        "actions": [
            {"action": "click", "selector": "#control"},
            {"action": "assert_text", "selector": "#status", "value": "Done"},
        ],
    }]
    evidence = {"checks": [{"check_id": "UI-001", "status": "ok"}]}
    config = HarnessConfig(evaluator_evidence_route="auto")

    assert _typed_pass_route_eligible(
        config=config, file_comm=file_comm, ui_checks=checks,
        evidence=evidence, edit_guard={"passed": True},
    )
    passed, grades, stats = build_deterministic_pass_grades(
        file_comm=file_comm, round_num=1, sprint_num=1,
        sprint_context={"exit_criteria": ["Status changes"]},
        ui_checks=checks, evidence=evidence, edit_guard={"passed": True},
    )

    assert passed is True
    assert grades["evidence_route"]["llm_evaluator_called"] is False
    assert grades["evidence_route"]["formal_full_evaluator"] is False
    assert stats.cost_usd == 0


def test_complete_chain_edit_contract_can_take_zero_cost_pass_route(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "edit_card.json").write_text(json.dumps({
        "schema_version": "edit-card-v1",
        "visual_evidence": "not_required",
        "chain_obligations": {
            "edit_id": "q2", "depends_on": ["q1"],
            "produces": [{"state": "A saved item persists after reload."}],
        },
    }))
    checks = [{
        "id": "UI-001",
        "actions": [
            {"action": "click", "selector": "#save"},
            {"action": "reload"},
            {"action": "assert_text", "selector": "#item", "value": "Saved"},
        ],
    }]

    assert _typed_pass_route_eligible(
        config=HarnessConfig(evaluator_evidence_route="auto"),
        file_comm=file_comm,
        ui_checks=checks,
        evidence={"checks": [{"check_id": "UI-001", "status": "ok"}]},
        edit_guard=None,
    )


def test_redundant_presence_after_action_requires_full_evaluator(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "edit_card.json").write_text(json.dumps({
        "schema_version": "edit-card-v1",
        "visual_evidence": "not_required",
    }))
    checks = [
        {
            "id": "UI-001",
            "actions": [
                {"action": "assert_visible", "selector": "#total"},
            ],
        },
        {
            "id": "UI-002",
            "actions": [
                {"action": "click", "selector": "#download"},
                {"action": "assert_visible", "selector": "#total"},
            ],
        },
    ]
    evidence = {"checks": [
        {"check_id": "UI-001", "status": "ok"},
        {"check_id": "UI-002", "status": "ok"},
    ]}

    assert not _typed_pass_route_eligible(
        config=HarnessConfig(evaluator_evidence_route="auto"),
        file_comm=file_comm,
        ui_checks=checks,
        evidence=evidence,
        edit_guard={"passed": True},
    )


def test_presence_only_contract_requires_source_capable_evaluator(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "edit_card.json").write_text(json.dumps({
        "schema_version": "edit-card-v1",
        "visual_evidence": "not_required",
    }))
    checks = [{
        "id": "UI-001",
        "actions": [
            {"action": "click", "selector": "#download"},
            {"action": "assert_visible", "selector": "#history"},
            {"action": "reload"},
            {"action": "assert_visible", "selector": "#history-item"},
        ],
    }]

    assert not _typed_pass_route_eligible(
        config=HarnessConfig(evaluator_evidence_route="auto"),
        file_comm=file_comm,
        ui_checks=checks,
        evidence={"checks": [{"check_id": "UI-001", "status": "ok"}]},
        edit_guard={"passed": True},
    )
    assert _contract_only_route_eligible(
        config=HarnessConfig(evaluator_evidence_route="auto"),
        file_comm=file_comm,
        ui_checks=checks,
    )


def test_bounded_edit_diff_exposes_only_baseline_to_candidate_patch(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    (frontend / "app.js").write_text("const value = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.js"], cwd=frontend, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Harness", "-c", "user.email=harness@example.com", "commit", "-m", "seed"],
        cwd=frontend,
        check=True,
        capture_output=True,
    )
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    (tmp_path / "seed_manifest.json").write_text(
        json.dumps({"baseline_commit": baseline}), encoding="utf-8"
    )
    (frontend / "app.js").write_text("const value = 2;\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.js"], cwd=frontend, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Harness", "-c", "user.email=harness@example.com", "commit", "-m", "edit"],
        cwd=frontend,
        check=True,
        capture_output=True,
    )

    diff = _bounded_edit_diff(tmp_path)

    assert "-const value = 1;" in diff
    assert "+const value = 2;" in diff


def test_visual_required_typed_contract_uses_compact_semantic_route(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "edit_card.json").write_text(json.dumps({
        "schema_version": "edit-card-v1",
        "visual_evidence": "required",
    }))
    checks = [{
        "id": "UI-001",
        "actions": [
            {"action": "click", "selector": "#save"},
            {"action": "assert_text", "selector": "#status", "value": "Saved"},
        ],
    }]

    assert _contract_only_route_eligible(
        config=HarnessConfig(evaluator_evidence_route="typed"),
        file_comm=file_comm,
        ui_checks=checks,
    ) is True
    assert _typed_pass_route_eligible(
        config=HarnessConfig(evaluator_evidence_route="typed"),
        file_comm=file_comm,
        ui_checks=checks,
        evidence={"checks": [{"check_id": "UI-001", "status": "ok"}]},
        edit_guard={"passed": True},
    ) is False


@pytest.mark.anyio
async def test_contract_evaluator_does_not_pay_for_format_retry(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "browser_evidence_round_1.json").write_text(
        '{"checks":[{"check_id":"UI-001","status":"ok"}]}'
    )
    calls = 0

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def complete(self, **_kwargs):
            nonlocal calls
            calls += 1
            return {"choices": [{"message": {"content": "not-json"}}], "usage": {}}

    monkeypatch.setattr("src.agents.evaluator.OpenAIHTTPClient", FakeClient)

    with pytest.raises(RuntimeError, match="automatic paid format retries are disabled"):
        await _run_contract_only_evaluator(
            HarnessConfig(),
            file_comm,
            1,
            1,
            {"exit_criteria": ["Works"]},
            [{
                "id": "UI-001", "feature_id": "F001", "critical": True,
                "task": "Use control", "expected_result": "It works",
            }],
            {"passed": True},
        )

    assert calls == 1


@pytest.mark.anyio
async def test_evaluator_builds_staged_prompt_with_sprint_context(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)
    file_comm.write_feedback(1, "# Round 1 Feedback")
    file_comm.write_grades(1, _passing_grades(1))
    captured: dict = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        file_comm.write_grades(2, _passing_grades(2))
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.3,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.3,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.evaluator.run_sdk_agent", fake_run_sdk_agent)

    passed, grades, stats = await run_evaluator(
        HarnessConfig(evaluator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=2,
        app_url="http://127.0.0.1:4173",
    )

    assert passed is True
    assert grades["mode_recommendation"] == "generate_next_sprint"
    # claude-sonnet-4-6 at $3 per 1M input tokens → 100_000 * 3 / 1e6 = $0.30.
    # Matches the legacy 0.3 by coincidence but is now derived from the
    # local pricing table.
    assert stats.cost_usd == 0.3
    assert stats.duration_ms == 1
    assert "Application URL: http://127.0.0.1:4173" in captured["prompt"]
    assert "Round: 2" in captured["prompt"]
    assert "Sprint: 1" in captured["prompt"]
    assert "Sprint Title: Core counter" in captured["prompt"]
    assert "Sprint Goal: Ship the primary counter flow." in captured["prompt"]
    assert "Target Feature IDs: F001" in captured["prompt"]
    assert "Exit Criterion Feature Mapping:" in captured["prompt"]
    assert "criterion_id=EXIT-01-01 | feature_id=F001 | critical=True | criterion=Counter increments correctly." in captured["prompt"]
    assert "Current Sprint UI Verification Checks:" in captured["prompt"]
    assert "check_id=UI-001 | feature_id=F001 | critical=True" in captured["prompt"]
    assert "task=Click increment once." in captured["prompt"]
    assert "expected=Counter changes by one step." in captured["prompt"]
    assert "Required Reads:" in captured["prompt"]
    assert "- .harness/spec.md" in captured["prompt"]
    assert "- .harness/design_tokens.json" in captured["prompt"]
    assert "- .harness/feature_list.json" in captured["prompt"]
    assert "- .harness/sprint_plan.json" in captured["prompt"]
    assert "- .harness/ui_verification_plan.json" in captured["prompt"]
    assert "- .harness/accepted_sprints.json" in captured["prompt"]
    assert "- .harness/feedback_round_1.md" in captured["prompt"]
    assert "- .harness/grade_round_1.json" in captured["prompt"]
    assert ".claude/skills/webapp-testing/SKILL.md" in captured["prompt"]
    assert "Phase A: Render Gate" in captured["prompt"]
    assert "Phase B: UI Functionality Verification" in captured["prompt"]
    assert "Phase C: Deferred Visual Review Capture" in captured["prompt"]
    assert "Phase E: Score Aggregation And Verdict" in captured["prompt"]
    assert "3. .harness/visual_manifest_round_2.json" not in captured["prompt"]
    assert ".harness/visual_round_2_home.png" not in captured["prompt"]
    assert "downstream VLM review" not in captured["prompt"]


@pytest.mark.anyio
async def test_evaluator_prompt_prioritizes_harness_browser_evidence(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)
    (file_comm.dir / "browser_evidence_round_1.json").write_text('{"checks": []}\n')
    captured: dict = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        file_comm.write_grades(1, _passing_grades(1))
        return (
            SimpleNamespace(duration_ms=1, duration_api_ms=1, usage={}, model_usage={}, result="done"),
            0.0,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.evaluator.run_sdk_agent", fake_run_sdk_agent)
    await run_evaluator(
        HarnessConfig(evaluator_model="qwen3-coder-plus"), file_comm, tmp_path,
        round_num=1, app_url="http://127.0.0.1:4173",
    )

    assert ".harness/browser_evidence_round_1.json" in captured["prompt"]
    assert "Treat `action_failed`, or an `evaluate` step with `ok: false`, as an observed valid-test failure" in captured["prompt"]


def test_evaluator_prompt_requires_independent_edit_scope_audit(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)
    from src.agents.evaluator import _build_evaluator_prompt

    prompt = _build_evaluator_prompt(
        file_comm=file_comm, workdir=tmp_path, round_num=1, sprint_num=1,
        sprint_run_context=SprintState.load(file_comm).current_run_context(),
        app_url="http://127.0.0.1:4173",
        edit_guard={"passed": True, "allowed_root_keys": ["main"]},
    )
    assert ".harness/edit_dom_baseline.json" in prompt
    assert ".harness/edit_scope_round_1.json" in prompt
    assert "Edit Scope Contract (independent audit required)" in prompt


def test_evaluator_prompt_makes_mobile_check_a_preflight(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-mobile", "feature_id": "F001", "critical": True,
        "category": "responsive",
        "task": "Resize browser to mobile width and tap the control.",
        "expected_result": "Touch target remains reachable.",
    }]}]})
    from src.agents.evaluator import _build_evaluator_prompt

    prompt = _build_evaluator_prompt(
        file_comm=file_comm, workdir=tmp_path, round_num=1, sprint_num=1,
        sprint_run_context=SprintState.load(file_comm).current_run_context(),
        app_url="http://127.0.0.1:4173",
    )

    assert "MANDATORY RESPONSIVE PREFLIGHT" in prompt
    assert "width=375 and height=812" in prompt
    assert "MANDATORY KEYBOARD PRIORITY" not in prompt


def test_evaluator_system_prompt_requires_normal_click_before_force():
    from src.prompts.evaluator import EVALUATOR_SYSTEM_PROMPT

    assert "first call `browser_click` without\n`force`" in EVALUATOR_SYSTEM_PROMPT
    assert "successful non-forced `browser_click`" in EVALUATOR_SYSTEM_PROMPT
    assert "browser_set_viewport" in EVALUATOR_SYSTEM_PROMPT
    assert "Mobile evidence is\notherwise often lost" in EVALUATOR_SYSTEM_PROMPT


@pytest.mark.anyio
async def test_evaluator_prompt_includes_design_contract_reads_when_present(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)
    file_comm.write_design_brief(
        {
            "requested_mode": "image-first",
            "visual_strategy": "image_backed_ui",
            "reference_files": {"background_ui": ".harness/design/background_ui.png"},
            "aesthetic_intent": {"design_hypothesis": "Use asymmetry."},
            "responsive_strategy": {"desktop": "Layered", "mobile": "Stacked"},
            "overlay_regions": [{"id": "hero"}],
            "visual_success_criteria": ["Preserve hierarchy."],
            "implementation_rules": ["Keep text in HTML."],
        }
    )
    file_comm.write_layout_contract(
        {
            "viewport_targets": ["1440x900"],
            "regions": [{"id": "hero"}],
            "safe_zones": [],
            "forbidden_overlay_zones": [],
            "asset_fit": {"background_ui": "cover"},
            "responsive_rules": ["Keep controls visible."],
        }
    )
    file_comm.write_asset_manifest(
        {
            "assets": [{"id": "background_ui"}],
            "generation_records": [],
            "implementation_notes": ["Copy production assets."],
        }
    )
    captured: dict[str, str] = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        file_comm.write_grades(1, _passing_grades(1))
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.3,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.3,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.evaluator.run_sdk_agent", fake_run_sdk_agent)

    await run_evaluator(
        HarnessConfig(evaluator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=1,
        app_url="http://127.0.0.1:4173",
    )

    assert "- .harness/design/design_brief.json" in captured["prompt"]
    assert "- .harness/design/layout_contract.json" in captured["prompt"]
    assert "- .harness/design/asset_manifest.json" in captured["prompt"]
    assert "Design Contract Assessment:" in captured["prompt"]


@pytest.mark.anyio
async def test_evaluator_reads_written_grade_file_and_uses_overall_verdict(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)

    failing_grades = _passing_grades(1)
    failing_grades["overall_passed"] = False
    failing_grades["mode_recommendation"] = "repair"
    failing_grades["criteria"]["functionality"] = {
        "score": 5.0,
        "passed": False,
        "notes": "Critical interaction is broken.",
    }

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_grades(1, failing_grades)
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.3,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.3,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.evaluator.run_sdk_agent", fake_run_sdk_agent)

    passed, grades, stats = await run_evaluator(
        HarnessConfig(evaluator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=1,
        app_url="http://127.0.0.1:4173",
    )

    assert passed is False
    assert grades["overall_passed"] is False
    assert grades["mode_recommendation"] == "repair"
    assert grades["phase_results"]["ui_functionality"] == "pass"
    # Local pricing: claude-sonnet-4-6 input @ $3 / 1M → 100_000 * 3 / 1e6 = $0.30.
    assert stats.cost_usd == 0.3


@pytest.mark.anyio
async def test_evaluator_exposes_repo_local_claude_skills_to_workdir(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)

    source_skills = tmp_path / "source-skills"
    (source_skills / "webapp-testing").mkdir(parents=True)
    (source_skills / "webapp-testing" / "SKILL.md").write_text("# webapp testing skill\n")
    monkeypatch.setattr("src.agents.evaluator._LOCAL_CLAUDE_SKILLS_DIR", source_skills)

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_grades(1, _passing_grades(1))
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.3,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.3,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.evaluator.run_sdk_agent", fake_run_sdk_agent)

    await run_evaluator(
        HarnessConfig(evaluator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=1,
        app_url="http://127.0.0.1:4173",
    )

    exposed = tmp_path / ".claude" / "skills"
    assert exposed.exists()
    if exposed.is_symlink():
        assert exposed.resolve() == source_skills.resolve()
    else:
        assert (exposed / "webapp-testing" / "SKILL.md").read_text() == "# webapp testing skill\n"


def test_extract_grades_from_response_parses_embedded_json():
    grades = _passing_grades(3)
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="text",
                text=(
                    "Wrote .harness/grade_round_3.json\n"
                    f"{grades}"
                ).replace("'", '"').replace("True", "true").replace("False", "false")
            )
        ],
    )

    extracted = _extract_grades_from_response(response)

    assert extracted is not None
    assert extracted["round"] == 3
    assert extracted["criteria"]["functionality"]["score"] == 8.0
    assert extracted["target_exit_criteria_results"][0]["feature_id"] == "F001"
    assert extracted["target_exit_criteria_results"][0]["criterion_id"] == "EXIT-01-01"
    assert extracted["ui_checks"][0]["check_id"] == "UI-001"


def test_determine_passed_rejects_failed_critical_ui_check_without_overall_flag():
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["ui_checks"][0]["status"] = "fail"

    assert _determine_passed(grades) is False


def test_determine_passed_rejects_failed_critical_exit_criterion_without_overall_flag():
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["target_exit_criteria_results"][0]["passed"] = False

    assert _determine_passed(grades) is False


# --- tri-state robustness against agent-written strings ---


@pytest.mark.parametrize("critical_value", [True, "true", "True", "TRUE", "yes", 1])
def test_determine_passed_rejects_truthy_string_critical_with_failing_ui_check(critical_value):
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["ui_checks"][0]["critical"] = critical_value
    grades["ui_checks"][0]["status"] = "fail"

    assert _determine_passed(grades) is False


@pytest.mark.parametrize("passed_value", [False, "false", "False", "no", 0])
def test_determine_passed_rejects_falsey_string_passed_on_critical_exit_criterion(passed_value):
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["target_exit_criteria_results"][0]["critical"] = True
    grades["target_exit_criteria_results"][0]["passed"] = passed_value

    assert _determine_passed(grades) is False


@pytest.mark.parametrize("overall_value", ["false", "False", "no", 0])
def test_determine_passed_treats_falsey_string_overall_as_fail(overall_value):
    grades = _passing_grades(1)
    grades["overall_passed"] = overall_value

    assert _determine_passed(grades) is False


@pytest.mark.parametrize("sprint_value", ["false", "False", "no", 0])
def test_determine_passed_treats_falsey_string_sprint_passed_as_fail(sprint_value):
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["sprint_passed"] = sprint_value

    assert _determine_passed(grades) is False


def test_determine_passed_accepts_status_failed_synonym_on_critical_check():
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["ui_checks"][0]["status"] = "FAILED"

    assert _determine_passed(grades) is False


# --- grade extraction picks the right JSON among multiples ---


def test_extract_grades_from_response_picks_grade_among_explanatory_objects():
    text = (
        "I had to retry once. Here is my reasoning:\n"
        '{"explanation": "first attempt failed", "retry": true}\n'
        "Final grade JSON for grade_round_4.json:\n"
        '{"round": 4, "criteria": {"design_quality": {"score": 7.0, "passed": true},'
        ' "functionality": {"score": 8.0, "passed": true},'
        ' "originality": {"score": 6.0, "passed": true},'
        ' "craft": {"score": 7.0, "passed": true}},'
        ' "phase_results": {"render_gate": "pass"}}'
    )
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    extracted = _extract_grades_from_response(response)

    assert extracted is not None
    assert extracted["round"] == 4
    assert extracted["criteria"]["design_quality"]["score"] == 7.0
    # Must NOT have picked the explanatory object.
    assert "explanation" not in extracted


def test_extract_grades_from_response_returns_none_when_only_noise_objects():
    text = '{"explanation": "no grade yet"}\n{"another": "non-grade"}'
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    assert _extract_grades_from_response(response) is None


def test_extract_grades_from_response_handles_truncated_trailing_json():
    # A common LLM failure: max_tokens cuts the JSON mid-write.
    text = (
        '{"round": 1, "criteria": {"design_quality": {"score": 7.0, "passed": true},'
        ' "functionality": {"score": 8.0, "passed": true},'
        ' "originality": {"score": 6.0, "passed": true},'
        ' "craft": {"score": 7.0, "passed": true}}}\n'
        '{"truncated": "missin'  # <- truncated here, no closing
    )
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    extracted = _extract_grades_from_response(response)
    assert extracted is not None
    assert extracted["round"] == 1


def test_evaluator_prompt_prioritizes_verdict_artifacts_after_reproduced_defect():
    from src.prompts.evaluator import EVALUATOR_SYSTEM_PROMPT

    assert "write `grade_round_N.json` first" in EVALUATOR_SYSTEM_PROMPT
    assert "Do not reread" in EVALUATOR_SYSTEM_PROMPT


def test_completed_evaluation_key_requires_same_clean_source_and_browser_facts(tmp_path):
    import subprocess
    from src.agents.evaluator import _completed_evaluation_key
    frontend=tmp_path/'frontend'; frontend.mkdir()
    subprocess.run(['git','init','-q'],cwd=frontend,check=True)
    (frontend/'main.js').write_text('const value = 1;')
    subprocess.run(['git','add','.'],cwd=frontend,check=True)
    subprocess.run(['git','-c','user.name=Test','-c','user.email=test@example.test',
                    'commit','-qm','source'],cwd=frontend,check=True)
    evidence=tmp_path/'evidence.json'; evidence.write_text('{"checks":[{"status":"ok"}]}')
    config=HarnessConfig()
    key=_completed_evaluation_key(tmp_path,evidence,'request',config)
    assert key and key==_completed_evaluation_key(tmp_path,evidence,'request',config)
    assert key!=_completed_evaluation_key(tmp_path,evidence,'changed request',config)
    evidence.write_text('{"checks":[{"status":"action_failed"}]}')
    assert key!=_completed_evaluation_key(tmp_path,evidence,'request',config)
    (frontend/'main.js').write_text('const value = 2;')
    assert _completed_evaluation_key(tmp_path,evidence,'request',config) is None


@pytest.mark.anyio
async def test_completed_semantic_result_survives_downstream_visual_failure(monkeypatch,tmp_path):
    file_comm=FileComm(tmp_path/'.harness');_write_evaluator_context(file_comm)
    calls=[]
    async def sdk(**kwargs):
        calls.append(kwargs)
        file_comm.write_grades(2,_passing_grades(2))
        return (ResultMessage(subtype='result',duration_ms=1,duration_api_ms=1,is_error=False,
            num_turns=1,session_id='session',total_cost_usd=0.3,
            usage={'input_tokens':100_000},result='done'),0.3,'',[])
    monkeypatch.setattr('src.agents.evaluator.run_sdk_agent',sdk)
    monkeypatch.setattr('src.agents.evaluator._completed_evaluation_key',lambda *args:'identical-verified-evidence')
    config=HarnessConfig(evaluator_model='claude-sonnet-4-6')
    original=await run_evaluator(config,file_comm,tmp_path,round_num=2,app_url='http://127.0.0.1:4173')
    failed=_passing_grades(2);failed['overall_passed']=False
    failed['evaluation_infrastructure_failure']={'phase':'visual_review','reason':'503'}
    file_comm.write_grades(2,failed)
    resumed=await run_evaluator(config,file_comm,tmp_path,round_num=2,app_url='http://127.0.0.1:4173')
    assert len(calls)==1 and resumed[0] is True
    assert not resumed[1].get('evaluation_infrastructure_failure')
    assert resumed[2].cost_usd==original[2].cost_usd


@pytest.mark.anyio
@pytest.mark.parametrize("status, expected", [("ok", True), ("action_failed", False)])
async def test_product_session_uses_only_supplied_browser_evidence(tmp_path, monkeypatch, status, expected):
    comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(comm)
    comm.write_state({"supplied_atomic_plan": True})
    (comm.dir / "browser_evidence_round_1.json").write_text(json.dumps({
        "checks": [{"check_id": "UI-001", "status": status}]}))
    async def forbidden(*args, **kwargs):
        raise AssertionError("Product Session must not call an evaluator LLM")
    monkeypatch.setattr("src.agents.evaluator.OpenAIHTTPClient.complete", forbidden)
    passed, grades, stats = await run_evaluator(
        HarnessConfig(agent_runtime="openai", evaluator_evidence_route="typed"),
        comm, tmp_path, 1, "http://localhost:18931")
    assert passed is expected
    assert grades["evidence_route"]["llm_evaluator_called"] is False
    assert stats.cost_usd == 0
