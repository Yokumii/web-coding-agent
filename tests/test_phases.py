import asyncio
import inspect
import json
from pathlib import Path

import pytest

from src.agents.sdk_runner import AgentRunStats
from src.config import HarnessConfig
from src.orchestration import phases
from src.orchestration.cost_tracker import CostTracker
from src.orchestration.file_comm import FileComm
from src.orchestration.phases import (
    HarnessContext,
    Verdict,
    _apply_browser_click_evidence_gate,
    _checks_are_tape_eligible,
    _contract_functionality_recheck_allowed,
    _reconcile_action_contract_evidence,
    _action_contract_grade_conflicts,
    _edit_guard_requires_repair,
    _merge_visual_evidence_manifest,
    _visual_routes_for_checks,
    _visual_style_recheck_allowed,
    _dedicated_visual_recheck_allowed,
    _non_visual_gates_passed,
    run_build_phase,
    run_evaluate_phase,
    run_planner_phase,
)
from src.orchestration.sprint_state import SprintState


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _stats(cost_usd: float) -> AgentRunStats:
    return AgentRunStats(
        cost_usd=cost_usd,
        duration_ms=1000,
        duration_api_ms=800,
        token_usage={"input_tokens": 100, "output_tokens": 20},
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={},
    )


def test_tape_eligibility_matches_one_to_four_assertion_contract():
    check = {
        "id": "UI-001",
        "actions": [
            {"action": "assert_visible", "selector": "main"},
            {"action": "assert_count", "selector": "main", "count": 1},
        ],
    }
    assert _checks_are_tape_eligible([check]) is True

    check["actions"].extend(
        {"action": "assert_visible", "selector": f"#item-{index}"}
        for index in range(3)
    )
    assert _checks_are_tape_eligible([check]) is False
    assert _checks_are_tape_eligible(["not-a-check"]) is False


def test_visual_capture_targets_current_sprint_routes_in_order():
    assert _visual_routes_for_checks(
        [
            {"route": "/library.html"},
            {"route": "/library.html"},
            {"route": "/details.html"},
        ]
    ) == ["/library.html", "/details.html"]


def test_style_only_counterfactual_failure_allows_zero_mutation_recheck():
    grade = {
        "overall_passed": False,
        "bugs_found": [],
        "ui_checks": [{"check_id": "UI-1", "status": "pass"}],
        "edit_guard": {"passed": True},
    }
    certificate = {
        "status": "non_minimal",
        "redundant_change_ids": ["p001"],
        "atomic_patches": [
            {"change_id": "p001", "path": "catalog.css", "search": "", "replace": ".x{}"}
        ],
    }
    checks = [{"id": "UI-1", "category": "visual", "actions": []}]

    assert _visual_style_recheck_allowed(grade, certificate, checks) is True
    certificate["atomic_patches"][0]["path"] = "catalog.js"
    assert _visual_style_recheck_allowed(grade, certificate, checks) is False


def test_non_visual_pass_can_reach_dedicated_visual_review():
    grade = {
        "overall_passed": False,
        "phase_results": {
            "render_gate": "pass",
            "ui_functionality": "pass",
            "appearance": "skipped",
            "source_inspection": "pass",
        },
        "bugs_found": [],
        "regressions_found": [],
        "repair_instructions": [],
        "edit_guard": {"passed": True},
        "ui_checks": [{"check_id": "UI-1", "status": "pass"}],
    }
    evidence = {"checks": [{"check_id": "UI-1", "status": "ok"}]}

    assert _dedicated_visual_recheck_allowed(grade) is True
    assert _non_visual_gates_passed(
        grade, evidence, {"checks": []}, {"passed": True}
    ) is True
    grade["bugs_found"] = ["real bug"]
    assert _dedicated_visual_recheck_allowed(grade) is False


def test_legacy_positive_grade_without_phase_block_can_reach_visual_review():
    grade = {
        "overall_passed": True,
        "bugs_found": [],
        "regressions_found": [],
    }

    assert _non_visual_gates_passed(
        grade, {"checks": []}, None, None
    ) is True
    grade["overall_passed"] = False
    assert _non_visual_gates_passed(
        grade, {"checks": []}, None, None
    ) is False


def test_edit_guard_requires_both_machine_contract_and_independent_scope_audit():
    assert _edit_guard_requires_repair(
        {"passed": True}, {"edit_scope_audit": "pass"}, evaluator_mode="full"
    ) is False
    assert _edit_guard_requires_repair(
        {"passed": True}, {}, evaluator_mode="full"
    ) is True


def test_browser_click_evidence_gate_overrides_model_pass(tmp_path: Path):
    trace = tmp_path / ".harness" / "traces"
    trace.mkdir(parents=True)
    (trace / "evaluator_round_1.jsonl").write_text(
        '{"event":"tool","name":"browser_click","ok":false,'
        '"output":"chatbot intercepts pointer events"}\n',
        encoding="utf-8",
    )
    grades = {
        "overall_passed": True,
        "sprint_passed": True,
        "regression_passed": True,
        "mode_recommendation": "complete",
        "phase_results": {"ui_functionality": "pass"},
        "bugs_found": [],
        "repair_instructions": [],
    }

    gated = _apply_browser_click_evidence_gate(tmp_path, 1, grades)

    assert gated["overall_passed"] is False
    assert gated["sprint_passed"] is False
    assert gated["regression_passed"] is False
    assert gated["mode_recommendation"] == "repair"
    assert gated["phase_results"]["ui_functionality"] == "fail"
    assert "browser_click evidence failed" in gated["bugs_found"][0]
    assert gated["repair_instructions"]


def test_browser_click_evidence_gate_rejects_force_only_click(tmp_path: Path):
    trace = tmp_path / ".harness" / "traces"
    trace.mkdir(parents=True)
    (trace / "evaluator_round_1.jsonl").write_text(
        '{"event":"assistant","message":{"tool_calls":[{"id":"click-1",'
        '"function":{"name":"browser_click","arguments":"{\\"selector\\":\\"#save\\",\\"force\\":true}"}}]}}\n'
        '{"event":"tool","name":"browser_click","ok":true,"output":"clicked"}\n',
        encoding="utf-8",
    )
    grades = {
        "overall_passed": True,
        "sprint_passed": True,
        "regression_passed": True,
        "phase_results": {"ui_functionality": "pass"},
        "bugs_found": [],
        "repair_instructions": [],
    }

    gated = _apply_browser_click_evidence_gate(tmp_path, 1, grades)

    assert gated["overall_passed"] is False
    assert "forced browser_click" in gated["bugs_found"][0]


def test_merge_visual_evidence_manifest_keeps_agent_refs_and_adds_independent_refs():
    merged = _merge_visual_evidence_manifest(
        {"round": 1, "app_url": "http://example.test", "screenshots": [".harness/agent.png"]},
        round_num=1,
        app_url="http://127.0.0.1:5173",
        evidence_refs=[".harness/visual_round_1_auto_top.png", ".harness/visual_round_1_auto_scrolled.png"],
    )

    assert merged["app_url"] == "http://127.0.0.1:5173"
    assert merged["screenshots"] == [
        ".harness/agent.png",
        ".harness/visual_round_1_auto_top.png",
        ".harness/visual_round_1_auto_scrolled.png",
    ]
    assert "independently captured" in merged["notes"]
    assert _edit_guard_requires_repair(
        {"passed": False}, {"edit_scope_audit": "pass"}, evaluator_mode="simple"
    ) is True


def test_action_contract_evidence_overrides_conflicting_llm_ui_check(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.dir.mkdir(parents=True, exist_ok=True)
    (file_comm.dir / "browser_evidence_round_1.json").write_text(
        '{"checks":[{"check_id":"UI-001","status":"ok"},'
        '{"check_id":"UI-002","status":"action_failed"}]}\n'
    )
    grades = {
        "ui_checks": [
            {"check_id": "UI-001", "feature_id": "F001", "status": "fail", "notes": "LLM mismatch"},
            {"check_id": "UI-002", "feature_id": "F002", "status": "pass", "notes": "LLM mismatch"},
        ],
        "target_exit_criteria_results": [
            {"feature_id": "F001", "passed": False},
            {"feature_id": "F002", "passed": True},
        ],
    }

    reconciled = _reconcile_action_contract_evidence(file_comm, 1, grades)

    assert [item["status"] for item in reconciled["ui_checks"]] == ["pass", "fail"]
    assert [item["passed"] for item in reconciled["target_exit_criteria_results"]] == [True, False]
    assert reconciled["browser_action_contract_reconciliation"]["changed_check_ids"] == ["UI-001", "UI-002"]


def test_complete_passing_action_contracts_calibrate_functionality_to_threshold(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.dir.mkdir(parents=True, exist_ok=True)
    (file_comm.dir / "browser_evidence_round_1.json").write_text(
        '{"checks":[{"check_id":"UI-001","status":"ok"},'
        '{"check_id":"UI-002","status":"ok"}]}\n'
    )
    grades = {
        "criteria": {
            "functionality": {
                "score": 5.0,
                "passed": True,
                "notes": "Exploratory evaluator did not exercise every path.",
            }
        },
        "ui_checks": [
            {"check_id": "UI-001", "feature_id": "F001", "critical": True,
             "status": "pass", "notes": "already agreed"},
            {"check_id": "UI-002", "feature_id": "F001", "critical": True,
             "status": "pass", "notes": "already agreed"},
        ],
        "target_exit_criteria_results": [],
    }

    reconciled = _reconcile_action_contract_evidence(file_comm, 1, grades)

    assert reconciled["criteria"]["functionality"]["score"] == 6.0
    assert reconciled["criteria"]["functionality"]["passed"] is True
    assert "typed browser action contracts" in reconciled["criteria"]["functionality"]["notes"]
    assert reconciled["browser_action_contract_reconciliation"]["functionality_calibrated"] is True


def test_incomplete_action_contracts_do_not_calibrate_functionality(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.dir.mkdir(parents=True, exist_ok=True)
    (file_comm.dir / "browser_evidence_round_1.json").write_text(
        '{"checks":[{"check_id":"UI-001","status":"ok"}]}\n'
    )
    grades = {
        "criteria": {"functionality": {"score": 5.0, "passed": False, "notes": "low"}},
        "ui_checks": [
            {"check_id": "UI-001", "critical": True, "status": "pass"},
            {"check_id": "UI-002", "critical": True, "status": "partial"},
        ],
        "target_exit_criteria_results": [],
    }

    reconciled = _reconcile_action_contract_evidence(file_comm, 1, grades)

    assert reconciled["criteria"]["functionality"]["score"] == 5.0
    assert "browser_action_contract_reconciliation" not in reconciled


def test_contract_functionality_policy_change_allows_zero_mutation_recheck(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.dir.mkdir(parents=True, exist_ok=True)
    (file_comm.dir / "browser_evidence_round_8.json").write_text(
        '{"checks":[{"check_id":"UI-001","status":"ok"},'
        '{"check_id":"UI-002","status":"ok"}]}\n'
    )
    grade = {
        "overall_passed": False,
        "bugs_found": [],
        "regressions_found": [],
        "repair_instructions": [],
        "phase_results": {
            "render_gate": "pass",
            "ui_functionality": "pass",
            "appearance": "pass",
            "source_inspection": "pass",
        },
        "edit_guard": {"passed": True},
        "criteria": {"functionality": {"score": 5.0, "passed": True}},
        "ui_checks": [
            {"check_id": "UI-001", "status": "pass"},
            {"check_id": "UI-002", "status": "pass"},
        ],
    }

    assert _contract_functionality_recheck_allowed(file_comm, 8, grade) is True


def test_action_contract_conflict_gate_only_checks_complete_contracts(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "browser_evidence_round_1.json").write_text(
        '{"checks":[{"check_id":"UI-001","status":"ok"},'
        '{"check_id":"UI-002","status":"action_failed"},'
        '{"check_id":"UI-003","status":"no_action_contract"}]}'
    )
    grades = {"ui_checks": [
        {"check_id": "UI-001", "status": "partial"},
        {"check_id": "UI-002", "status": "pass"},
        {"check_id": "UI-003", "status": "partial"},
    ]}
    assert _action_contract_grade_conflicts(file_comm, 1, grades) == ["UI-001", "UI-002"]


def _stub_sprint(number: int) -> dict:
    return {
        "number": number,
        "title": f"Sprint {number}",
        "goal": f"Ship sprint {number}.",
        "feature_ids": [f"F00{number}"],
        "deliverables": [f"S{number} UI."],
        "exit_criteria": [f"S{number} works."],
    }


def _write_feature_list(file_comm: FileComm, total: int = 1) -> None:
    file_comm.write_feature_list(
        {
            "features": [
                {
                    "id": f"F00{index}",
                    "name": f"Feature {index}",
                    "priority": "high",
                    "depends_on": [],
                    "description": f"Feature {index} description.",
                    "acceptance_criteria": [f"Feature {index} works."],
                    "status": "planned",
                    "sprint": index,
                }
                for index in range(1, total + 1)
            ]
        }
    )


def _make_ctx(tmp_path: Path) -> HarnessContext:
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_sprint_plan({"total_sprints": 1, "sprints": [_stub_sprint(1)]})
    file_comm.write_accepted_sprints(
        {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
    )
    _write_feature_list(file_comm, total=1)
    return HarnessContext(
        workdir=tmp_path,
        config=HarnessConfig(max_rounds=1),
        file_comm=file_comm,
        cost_tracker=CostTracker(10),
        sprint_state=SprintState.load(file_comm),
        user_prompt="demo",
    )


class _DummyAppStack:
    def __init__(self, frontend_url: str = "http://127.0.0.1:5173") -> None:
        self.frontend_url = frontend_url
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_module_imports():
    assert phases.__name__ == "src.orchestration.phases"


def test_verdict_values():
    assert Verdict.completed == "completed"
    assert Verdict.accepted_review == "accepted_review"
    assert Verdict.failed_review == "failed_review"


def test_planner_phase_is_async():
    assert inspect.iscoroutinefunction(run_planner_phase)


def test_build_phase_is_async_and_takes_round_num():
    assert inspect.iscoroutinefunction(run_build_phase)
    sig = inspect.signature(run_build_phase)
    assert "round_num" in sig.parameters
    assert "resume_state" in sig.parameters


def test_evaluate_phase_is_async_and_takes_round_num():
    assert inspect.iscoroutinefunction(run_evaluate_phase)
    sig = inspect.signature(run_evaluate_phase)
    assert "round_num" in sig.parameters


def test_harness_context_dataclass_fields():
    fields = {f.name for f in HarnessContext.__dataclass_fields__.values()}
    assert {
        "workdir", "config", "file_comm", "cost_tracker", "sprint_state",
        "phase_metrics", "user_prompt",
    } <= fields


@pytest.mark.anyio
async def test_run_planner_phase_survives_delayed_cancellation(monkeypatch, tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    parent_task: asyncio.Task | None = None

    async def fake_run_planner(config, user_prompt, file_comm, workdir):
        nonlocal parent_task
        del config, user_prompt, workdir
        parent_task = asyncio.current_task()
        file_comm.write_sprint_plan({"total_sprints": 1, "sprints": [_stub_sprint(1)]})
        file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        asyncio.get_running_loop().call_soon(parent_task.cancel)
        return _stats(0.1)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_run_planner)

    await run_planner_phase(ctx)

    state = ctx.file_comm.read_state()
    assert state is not None
    assert state["last_completed_phase"] == "plan"
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_run_planner_phase_uses_dedicated_atomic_path_for_edit(
    monkeypatch, tmp_path: Path
):
    ctx = _make_ctx(tmp_path)
    (ctx.file_comm.dir / "edit_task_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "edit-task-contract-v1",
                "task_mode": "edit",
                "requested_target_routes": ["/"],
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    async def fake_atomic(config, user_prompt, file_comm, workdir):
        del config, user_prompt, file_comm, workdir
        calls.append("atomic")
        return _stats(0.01)

    async def forbidden_general(*args, **kwargs):
        del args, kwargs
        raise AssertionError("explicit Edit must not use the full Generate planner")

    monkeypatch.setattr("src.orchestration.phases.run_atomic_edit_planner", fake_atomic)
    monkeypatch.setattr("src.orchestration.phases.run_planner", forbidden_general)
    monkeypatch.setattr("src.orchestration.phases.materialize_edit_card", lambda **_: None)

    await run_planner_phase(ctx)

    assert calls == ["atomic"]
    assert ctx.file_comm.read_state()["last_completed_phase"] == "plan"


@pytest.mark.anyio
async def test_run_build_phase_does_not_auto_commit(monkeypatch, tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    parent_task: asyncio.Task | None = None
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()

    async def fake_run_generator(*args, **kwargs):
        nonlocal parent_task
        del args, kwargs
        parent_task = asyncio.current_task()
        return _stats(0.2)

    async def forbidden_commit_round(*args, **kwargs):
        raise AssertionError("Harness must not commit on behalf of the generator")

    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_run_generator)
    monkeypatch.setattr("src.orchestration.git_journal.commit_round", forbidden_commit_round)

    await run_build_phase(ctx, 1)

    state = ctx.file_comm.read_state()
    assert state is not None
    assert state["last_completed_phase"] == "build_r1"
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_run_build_phase_materializes_harness_owned_minimal_path(monkeypatch, tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    ctx.config.minimality_guard_enabled = False
    (tmp_path / "seed_manifest.json").write_text("{}")
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<main><button id="save">Save</button></main>', encoding="utf-8"
    )
    baseline = {
        "version": 2,
        "roots": [
            {"key": "main:unnamed", "fingerprint": "x", "anchors": ["#save"]}
        ],
    }
    for name in ("edit_dom_baseline.json", "edit_dom_source_sprint_1.json"):
        (ctx.file_comm.dir / name).write_text(json.dumps(baseline), encoding="utf-8")
    ctx.file_comm.write_ui_verification_plan(
        {
            "sprints": [
                {
                    "sprint": 1,
                    "checks": [
                        {
                            "id": "UI-001",
                            "feature_id": "F001",
                            "task": "Click save",
                            "expected_result": "Saved",
                            "critical": True,
                            "category": "interaction",
                            "actions": [
                                {"action": "click", "selector": "#save"},
                                {"action": "evaluate", "expression": "true"},
                            ],
                        }
                    ],
                }
            ]
        }
    )

    async def fake_run_generator(*args, **kwargs):
        del args, kwargs
        return _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_run_generator)

    await run_build_phase(ctx, 1)

    plan = json.loads(
        (ctx.file_comm.dir / "minimal_path_plan_round_1.json").read_text(encoding="utf-8")
    )
    scope = json.loads(
        (ctx.file_comm.dir / "edit_scope_round_1.json").read_text(encoding="utf-8")
    )
    assert plan["owner"] == "harness"
    assert plan["source_change_cone"]["local_paths"] == ["frontend/index.html"]
    assert scope["owner"] == "harness"
    assert scope["allowed_root_keys"] == ["main:unnamed"]


@pytest.mark.anyio
async def test_later_generate_sprint_is_a_scoped_incremental_edit(monkeypatch, tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    ctx.config.minimality_guard_enabled = False
    ctx.file_comm.write_sprint_plan({
        "total_sprints": 2,
        "sprints": [_stub_sprint(1), _stub_sprint(2)],
    })
    ctx.file_comm.write_accepted_sprints({
        "accepted": [1], "current_target": 2, "last_evaluated_round": 1,
    })
    _write_feature_list(ctx.file_comm, total=2)
    ctx.sprint_state = SprintState.load(ctx.file_comm)
    ctx.file_comm.write_ui_verification_plan({
        "sprints": [{
            "sprint": 2,
            "checks": [{
                "id": "UI-002", "feature_id": "F002", "task": "Use search",
                "expected_result": "Results update", "critical": True,
                "category": "interaction", "route": "/",
                "actions": [
                    {"action": "click", "selector": "#search"},
                    {"action": "evaluate", "expression": "true"},
                ],
            }],
        }],
    })
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<main><button id="search">Search</button></main>', encoding="utf-8"
    )
    stack = _DummyAppStack()

    async def fake_start_app_stack(*args, **kwargs):
        del args, kwargs
        return stack

    async def fake_capture_sprint_source_baseline(**kwargs):
        baseline = {
            "version": 3, "routes": ["/"],
            "roots": [{
                "key": "/::main:unnamed", "local_key": "main:unnamed",
                "route": "/", "fingerprint": "before", "anchors": ["#search"],
            }],
        }
        path = kwargs["file_comm"].dir / "edit_dom_source_sprint_2.json"
        path.write_text(json.dumps(baseline), encoding="utf-8")
        return baseline

    async def fake_run_generator(*args, **kwargs):
        del args, kwargs
        assert (ctx.file_comm.dir / "edit_dom_source_sprint_2.json").is_file()
        assert (ctx.file_comm.dir / "minimal_path_plan_round_2.json").is_file()
        return _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)
    monkeypatch.setattr(
        "src.orchestration.phases.capture_sprint_source_baseline",
        fake_capture_sprint_source_baseline,
    )
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_run_generator)

    await run_build_phase(ctx, 2)

    plan = json.loads(
        (ctx.file_comm.dir / "minimal_path_plan_round_2.json").read_text(encoding="utf-8")
    )
    assert plan["mode"] == "generate"
    assert plan["route_scope"]["target_routes"] == ["/"]
    assert plan["dom_change_cone"]["baseline"] == ".harness/edit_dom_source_sprint_2.json"
    assert stack.closed is True


@pytest.mark.anyio
async def test_run_evaluate_phase_normalizes_inconsistent_pass_and_recommendation(
    monkeypatch, tmp_path: Path
):
    ctx = _make_ctx(tmp_path)
    stack = _DummyAppStack()

    async def fake_run_evaluator(*args, **kwargs):
        del args, kwargs
        return (
            True,
            {
                "round": 1,
                "sprint": 1,
                "mode_recommendation": "generate_next_sprint",
                "overall_passed": True,
                "sprint_passed": True,
                "criteria": {
                    "design_quality": {"score": 7.0, "passed": True},
                    "functionality": {"score": 7.0, "passed": True},
                    "originality": {"score": 6.0, "passed": True},
                    "craft": {"score": 7.0, "passed": True},
                },
                "target_exit_criteria_results": [
                    {
                        "criterion_id": "EXIT-01-01",
                        "feature_id": "F001",
                        "critical": True,
                        "criterion": "S1 works.",
                        "passed": True,
                        "notes": "looks good",
                    }
                ],
                "ui_checks": [
                    {
                        "check_id": "UI-001",
                        "feature_id": "F001",
                        "critical": True,
                        "task": "critical check",
                        "expected_result": "works",
                        "status": "partial",
                        "notes": "still incomplete",
                    }
                ],
                "phase_results": {
                    "render_gate": "pass",
                    "ui_functionality": "partial",
                    "appearance": "pass",
                    "source_inspection": "pass",
                },
            },
            _stats(0.3),
        )

    async def fake_start_app_stack(*args, **kwargs):
        del args, kwargs
        return stack

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_run_evaluator)
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)
    monkeypatch.setattr("src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review)
    async def fake_collect_browser_evidence(**kwargs):
        return {"checks": []}
    monkeypatch.setattr("src.orchestration.phases.collect_browser_evidence", fake_collect_browser_evidence)

    verdict = await run_evaluate_phase(ctx, 1)

    assert verdict is Verdict.failed_review
    assert stack.closed is True
    assert ctx.file_comm.read_accepted_sprints() == {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 1,
    }

    grades = ctx.file_comm.read_grades(1)
    assert grades is not None
    assert grades["overall_passed"] is False
    assert grades["sprint_passed"] is False
    assert grades["mode_recommendation"] == "repair"

    state = ctx.file_comm.read_state()
    assert state is not None
    assert state["last_completed_phase"] == "evaluate_r1"
    assert state["last_verdict"] == "failed_review"
