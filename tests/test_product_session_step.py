import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("product_session_step", SCRIPTS / "run_product_session_step.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def edit_payload(edit_id="q2", instruction="Organize records into groups."):
    return {
        "edit_id": edit_id,
        "capability": "Grouped records",
        "instruction": instruction,
        "classification": {
            "primary": {"taxonomy": "webcompass", "type": "Data Table"},
            "secondary": [],
            "reason": "The edit adds a table operation.",
        },
        "target_routes": ["/"],
        "completion_criteria": ["Grouped records are visible."],
        "browser_check": {
            "id": "group-flow",
            "route": "/",
            "actions": [{
                "action": "assert_visible",
                "selector": "[data-testid='groups']",
            }],
        },
    }


@pytest.mark.anyio
@pytest.mark.parametrize("existing_plan", [False, True])
@pytest.mark.parametrize("no_api_budget", [False, True])
async def test_bridge_passes_only_current_edit_without_replanning(tmp_path, monkeypatch, existing_plan, no_api_budget):
    monkeypatch.setenv("TOKENWAVE_OPENAI_API_KEY", "test-only-no-network")
    states = iter([{"last_completed_phase":"plan"} if existing_plan else {}, {"last_verdict":"completed","costs":{"build":0.1}}])
    monkeypatch.setattr(bridge,"_read_state",lambda _:next(states))
    monkeypatch.setattr(bridge,"prepare_seed",lambda *args:args[1].mkdir())
    monkeypatch.setattr(bridge,"_latest_grade_path",lambda _:tmp_path/"grade.json")
    monkeypatch.setattr(bridge,"_accepted_checks",lambda *args,**kwargs:[{"accepted":True}])
    received = {}
    async def run(prompt, workdir, config, **kwargs): received.update(prompt=prompt, config=config, **kwargs)
    monkeypatch.setattr(bridge,"run_harness",run)
    edit = edit_payload()
    payload = {"workdir":str(tmp_path/"step"),"edit":edit,"source_project":str(tmp_path/"source"),
        "source_evaluation":str(tmp_path/"old_grade.json"),"budget_usd":1,"session_id":"s",
        "prior_checks":[[{"old":"behavior"}]], "no_api_budget": no_api_budget}
    payload['inspiration_code'] = {'snippets': [{'content': 'REFERENCE_ONLY'}]}
    if existing_plan:
        (tmp_path/"step/.harness").mkdir(parents=True)
        (tmp_path/"step/.harness/edit_task_contract.json").write_text(json.dumps({"chain_metadata":edit}))
    result = await bridge.execute(payload)
    assert json.loads((tmp_path/'step/.harness/inspiration_code.json').read_text()) == payload['inspiration_code']
    assert received["resume"] == existing_plan
    assert received["chain_metadata"] == (None if existing_plan else edit)
    assert received["prior_accepted_checks"] is None
    assert received["task_mode"] == "edit"
    if existing_plan:
        assert received["atomic_plan"] is None
    else:
        assert received["atomic_plan"]["checks"][0]["id"] == "group-flow"
    assert received["config"].minimality_guard_enabled is False
    assert received["config"].edit_originality_required is False
    assert received["config"].evaluator_evidence_route == "typed"
    assert received["config"].edit_collect_visual_failures_before_repair is False
    assert received["config"].edit_webcompass_defect_checks is True
    assert received["config"].edit_max_rounds == 2
    assert received["config"].max_budget_usd == (float("inf") if no_api_budget else 1)
    assert received["config"].allow_paid_resume_call is no_api_budget
    assert received["config"].planner_budget_usd == (float("inf") if no_api_budget else 1)
    assert received["config"].evaluator_budget_usd == (float("inf") if no_api_budget else 1)
    assert result["status"] == "completed"


@pytest.mark.anyio
async def test_bridge_rejects_changed_instruction_on_resume(tmp_path, monkeypatch):
    workdir=tmp_path/"step"
    (workdir/".harness").mkdir(parents=True)
    (workdir/".harness/edit_task_contract.json").write_text(json.dumps({"chain_metadata":{"instruction":"old"}}))
    monkeypatch.setattr(bridge,"_read_state",lambda _:{"last_verdict":"incomplete"})
    with pytest.raises(ValueError,match="changed Edit"):
        await bridge.execute({"workdir":str(workdir),"edit":{"instruction":"new"}})


@pytest.mark.anyio
async def test_bridge_rebuilds_incomplete_seed_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENWAVE_OPENAI_API_KEY", "test-only-no-network")
    workdir = tmp_path / "step"
    workdir.mkdir()
    (workdir / "partial.txt").write_text("interrupted")
    prepared = []
    def prepare(_source, target, _evaluation):
        prepared.append(target)
        target.mkdir()
    monkeypatch.setattr(bridge, "prepare_seed", prepare)
    states = iter([{}, {"last_verdict":"completed","costs":{}}])
    monkeypatch.setattr(bridge, "_read_state", lambda _: next(states))
    monkeypatch.setattr(bridge, "_latest_grade_path", lambda _: tmp_path/"grade.json")
    monkeypatch.setattr(bridge, "_accepted_checks", lambda *args, **kwargs: [])
    async def run(*args, **kwargs): pass
    monkeypatch.setattr(bridge, "run_harness", run)
    payload = {
        "workdir": str(workdir), "source_project": str(tmp_path/"source"),
        "source_evaluation": str(tmp_path/"evaluation.json"), "budget_usd": 1,
        "session_id": "s", "edit": edit_payload(instruction="Add modal."),
    }
    result = await bridge.execute(payload)
    assert prepared == [workdir]
    assert not (workdir / "partial.txt").exists()
    assert result["status"] == "completed"


def test_atomic_plan_rejects_invalid_external_action_before_generation():
    edit = edit_payload()
    edit["browser_check"]["actions"] = [
        {"action":"drag_and_drop", "source":"#card", "destination":"#column"},
        {"action":"assert_visible", "selector":"#card"},
    ]
    with pytest.raises(ValueError, match="unsupported fields"):
        bridge.atomic_plan_from_edit(edit)


def test_supplied_actions_survive_compatibility_materialization(tmp_path):
    from src.orchestration.atomic_edit_plan import write_atomic_edit_plan, read_atomic_edit_plan
    edit = edit_payload()
    actions = [
        {'action':'assert_count','selector':'.card','count':4},
        {'action':'drag_and_drop','source_selector':'#card','target_selector':'#done'},
        {'action':'assert_text','selector':'#status','value':'Done','match':'exact'},
    ]
    edit['browser_check']['actions'] = actions
    plan = bridge.atomic_plan_from_edit(edit)
    write_atomic_edit_plan(tmp_path, plan, preserve_actions=True)
    assert read_atomic_edit_plan(tmp_path)['checks'][0]['actions'] == actions


def test_many_completion_conditions_remain_one_atomic_acceptance_item(tmp_path):
    from src.orchestration.atomic_edit_plan import write_atomic_edit_plan, read_atomic_edit_plan
    edit = edit_payload()
    criteria = [f'Visible completion condition {i}.' for i in range(11)]
    edit['completion_criteria'] = list(criteria)
    plan = bridge.atomic_plan_from_edit(edit)
    write_atomic_edit_plan(tmp_path, plan, preserve_actions=True)
    saved = read_atomic_edit_plan(tmp_path)
    assert len(saved['exit_criteria']) == 1
    assert saved['exit_criteria'][0].splitlines() == criteria
    assert len(saved['checks']) == 1
    assert saved['checks'][0]['actions'] == edit['browser_check']['actions']
    assert edit['completion_criteria'] == criteria


def test_reference_code_is_only_rendered_when_supplied(tmp_path):
    from src.agents.generator import _render_inspiration_code
    assert _render_inspiration_code(tmp_path) == ''
    (tmp_path/'inspiration_code.json').write_text(json.dumps({'snippets':[]}))
    assert _render_inspiration_code(tmp_path) == ''
    (tmp_path/'inspiration_code.json').write_text(json.dumps({'snippets':[{'content':'SELECTED_CODE'}]}))
    result = _render_inspiration_code(tmp_path)
    assert 'SELECTED_CODE' in result and 'not host files' in result
