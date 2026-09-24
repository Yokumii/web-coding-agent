from __future__ import annotations

import copy
import asyncio
import json

import pytest

from src.orchestration.frozen_compound_edit import (
    normalize_frozen_compound_plan,
    read_frozen_compound_plan,
    write_frozen_compound_plan,
)


def frozen_tasks(count: int = 4):
    return [
        {
            "id": f"q{index}",
            "task_type": f"Type {index}",
            "instruction": f"Add capability {index}.",
        }
        for index in range(1, count + 1)
    ]


def raw_plan(tasks):
    return {
        "subtasks": [
            {
                **task,
                "target_routes": ["/"],
                "atomic_plan": {
                    "goal": task["instruction"],
                    "visual_evidence": "not_required",
                    "checks": [
                        {
                            "id": f"{task['id']}-flow",
                            "route": "/",
                            "actions": [
                                {
                                    "action": "assert_visible",
                                    "selector": f"[data-testid='{task['id']}-result']",
                                }
                            ],
                        }
                    ],
                },
            }
            for task in tasks
        ]
    }


def test_plan_freezes_exact_subtask_order_and_hash(tmp_path):
    tasks = frozen_tasks()
    normalized = normalize_frozen_compound_plan(
        raw_plan(tasks),
        case_id="case-1",
        source_code_sha256="abc",
        planner_model="gpt-5.6-luna",
        frozen_subtasks=tasks,
    )
    path = tmp_path / "plan.json"
    digest = write_frozen_compound_plan(path, normalized)

    loaded, loaded_digest = read_frozen_compound_plan(path)

    assert loaded_digest == digest
    assert [item["instruction"] for item in loaded["subtasks"]] == [
        item["instruction"] for item in tasks
    ]
    assert loaded["subtasks"][0]["atomic_plan"]["goal"] == tasks[0]["instruction"]


def test_plan_rejects_rewritten_frozen_instruction():
    tasks = frozen_tasks()
    payload = raw_plan(tasks)
    payload["subtasks"][2]["instruction"] = "Rewrite the entire website."

    with pytest.raises(ValueError, match="changed frozen subtask 3 field instruction"):
        normalize_frozen_compound_plan(
            payload,
            case_id="case-1",
            source_code_sha256="abc",
            planner_model="gpt-5.6-luna",
            frozen_subtasks=tasks,
        )


def test_compact_planner_output_binds_immutable_dataset_fields():
    tasks = frozen_tasks()
    payload = raw_plan(tasks)
    for item in payload["subtasks"]:
        item.pop("task_type")
        item.pop("instruction")
        item["atomic_plan"].pop("goal", None)
    result = normalize_frozen_compound_plan(payload, case_id="case", source_code_sha256="abc",
                                           planner_model="gpt-5.6-luna", frozen_subtasks=tasks)
    for task, item in zip(tasks, result["subtasks"]):
        assert item["instruction"] == item["atomic_plan"]["goal"] == task["instruction"]
        assert item["task_type"] == task["task_type"]


def test_plan_rejects_check_outside_write_routes():
    tasks = frozen_tasks()
    payload = raw_plan(tasks)
    payload["subtasks"][0]["atomic_plan"]["checks"][0]["route"] = "/settings"

    with pytest.raises(ValueError, match="outside target_routes"):
        normalize_frozen_compound_plan(
            payload,
            case_id="case-1",
            source_code_sha256="abc",
            planner_model="gpt-5.6-luna",
            frozen_subtasks=tasks,
        )


def test_plan_folds_redundant_navigate_into_check_route():
    tasks = frozen_tasks()
    payload = raw_plan(tasks)
    payload["subtasks"][0]["target_routes"] = ["/recipes.html"]
    check = payload["subtasks"][0]["atomic_plan"]["checks"][0]
    check["actions"].insert(
        0, {"action": "navigate", "url": "/recipes.html"}
    )

    normalized = normalize_frozen_compound_plan(
        payload,
        case_id="case-1",
        source_code_sha256="abc",
        planner_model="gpt-5.6-luna",
        frozen_subtasks=tasks,
    )

    saved_check = normalized["subtasks"][0]["atomic_plan"]["checks"][0]
    assert saved_check["route"] == "/recipes.html"
    assert all(action["action"] != "navigate" for action in saved_check["actions"])


def test_plan_normalizes_unambiguous_action_field_aliases():
    tasks = frozen_tasks()
    payload = raw_plan(tasks)
    payload["subtasks"][0]["atomic_plan"]["checks"][0]["actions"] = [
        {
            "action": "assert_property",
            "selector": "#submit",
            "property": "disabled",
            "value": True,
        },
        {
            "action": "assert_storage_value",
            "storage": "sessionStorage",
            "key": "page",
            "value": 2,
        },
        {
            "action": "assert_property",
            "selector": "#status",
            "name": "data-running",
            "value": True,
        },
    ]

    normalized = normalize_frozen_compound_plan(
        payload,
        case_id="case-1",
        source_code_sha256="abc",
        planner_model="gpt-5.6-luna",
        frozen_subtasks=tasks,
    )

    actions = normalized["subtasks"][0]["atomic_plan"]["checks"][0]["actions"]
    assert actions[0]["name"] == "disabled"
    assert "property" not in actions[0]
    assert actions[1]["storage"] == "session"
    assert actions[2]["action"] == "assert_attribute"
    assert actions[2]["name"] == "data-running"


def test_plan_normalizes_common_interaction_aliases():
    tasks = frozen_tasks()
    payload = raw_plan(tasks)
    payload["subtasks"][0]["atomic_plan"]["checks"][0]["actions"] = [
        {
            "action": "drag_and_drop",
            "source": "[data-testid='first-card']",
            "destination": "[data-testid='second-card']",
        },
        {
            "action": "assert_visible",
            "selector": "[data-testid='second-card']",
        },
    ]

    normalized = normalize_frozen_compound_plan(
        payload,
        case_id="case-1",
        source_code_sha256="abc",
        planner_model="gpt-5.6-luna",
        frozen_subtasks=tasks,
    )

    drag = normalized["subtasks"][0]["atomic_plan"]["checks"][0]["actions"][0]
    assert drag == {
        "action": "drag_and_drop",
        "source_selector": "[data-testid='first-card']",
        "target_selector": "[data-testid='second-card']",
    }


def test_plan_splits_url_fragment_assertion_into_path_and_hash():
    tasks = frozen_tasks()
    payload = raw_plan(tasks)
    payload["subtasks"][0]["target_routes"] = ["/about.html"]
    check = payload["subtasks"][0]["atomic_plan"]["checks"][0]
    check["route"] = "/about.html"
    check["actions"] = [
        {"action": "click", "selector": "a[href='#team']"},
        {"action": "assert_url", "value": "/about.html#team"},
    ]

    normalized = normalize_frozen_compound_plan(
        payload,
        case_id="case-1",
        source_code_sha256="abc",
        planner_model="gpt-5.6-luna",
        frozen_subtasks=tasks,
    )

    actions = normalized["subtasks"][0]["atomic_plan"]["checks"][0]["actions"]
    assert actions[-2:] == [
        {"action": "assert_url", "value": "/about.html"},
        {"action": "assert_hash", "value": "#team"},
    ]


def test_plan_normalizes_scroll_aliases_and_accepts_bounded_history_wait():
    tasks = frozen_tasks()
    payload = raw_plan(tasks)
    payload["subtasks"][0]["atomic_plan"]["checks"][0]["actions"] = [
        {"action": "press", "selector": "button", "key": "Enter"},
        {"action": "scroll", "selector": "main", "direction": "down", "amount": 600},
        {"action": "go_back"},
        {"action": "wait", "milliseconds": 250},
        {"action": "assert_visible", "selector": "main"},
    ]

    normalized = normalize_frozen_compound_plan(
        payload,
        case_id="case-1",
        source_code_sha256="abc",
        planner_model="gpt-5.6-luna",
        frozen_subtasks=tasks,
    )

    actions = normalized["subtasks"][0]["atomic_plan"]["checks"][0]["actions"]
    assert actions[:4] == [
        {"action": "key_press", "selector": "button", "key": "Enter"},
        {"action": "scroll", "y": 600},
        {"action": "go_back"},
        {"action": "wait", "milliseconds": 250},
    ]


def test_frozen_plan_detects_post_plan_mutation(tmp_path):
    tasks = frozen_tasks()
    normalized = normalize_frozen_compound_plan(
        raw_plan(tasks),
        case_id="case-1",
        source_code_sha256="abc",
        planner_model="gpt-5.6-luna",
        frozen_subtasks=tasks,
    )
    path = tmp_path / "plan.json"
    write_frozen_compound_plan(path, normalized)
    raw = __import__("json").loads(path.read_text())
    raw["subtasks"][0]["instruction"] = "mutated"
    path.write_text(__import__("json").dumps(raw))

    with pytest.raises(ValueError, match="goal must equal|hash mismatch"):
        read_frozen_compound_plan(path)


@pytest.mark.parametrize('mode',['required','conditional','not_required'])
def test_visual_evidence_inline_reason_preserves_mode_and_explanation(mode):
    tasks=frozen_tasks();payload=raw_plan(tasks)
    payload['subtasks'][0]['atomic_plan']['visual_evidence']=mode+': inspect only the target panel'
    result=normalize_frozen_compound_plan(payload,case_id='case',source_code_sha256='abc',planner_model='gpt-5.6-luna',frozen_subtasks=tasks)
    actual=result['subtasks'][0]['atomic_plan']
    assert actual['visual_evidence']==mode
    assert actual['visual_evidence_reason']=='inspect only the target panel'


def test_visual_evidence_unknown_mode_is_not_guessed():
    tasks=frozen_tasks();payload=raw_plan(tasks)
    payload['subtasks'][0]['atomic_plan']['visual_evidence']='skip: inspect only the target panel'
    with pytest.raises(ValueError):normalize_frozen_compound_plan(payload,case_id='case',source_code_sha256='abc',planner_model='gpt-5.6-luna',frozen_subtasks=tasks)


def test_planner_corrects_hidden_entry_once_and_reuses_saved_responses(tmp_path, monkeypatch):
    from src.agents.compound_edit_planner import plan_frozen_compound_edit
    from src.agents.openai_runner import OpenAIHTTPClient
    from src.config import HarnessConfig
    tasks=frozen_tasks(); bad=raw_plan(tasks); good=raw_plan(tasks); calls=[]
    bad['subtasks'][0]['atomic_plan']['checks'][0]['actions'].insert(0,{'action':'click','selector':'#hidden'})
    good['subtasks'][0]['atomic_plan']['checks'][0]['actions'].insert(0,{'action':'click','selector':'#open'})
    async def complete(self, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        return {'choices':[{'message':{'content':json.dumps(bad if len(calls)==1 else good)}}],
                'usage':{'input_tokens':10,'output_tokens':20}}
    monkeypatch.setattr(OpenAIHTTPClient,'complete',complete)
    kwargs=dict(config=HarnessConfig(edit_skills_enabled=True,planner_model='gpt-5.5'),case_id='case',
        source_code_sha256='abc',frozen_subtasks=tasks,source_ui_contract={'pages':[{'route':'/',
        'controls':[{'selector':'#hidden','initially_visible':False},{'selector':'#open','initially_visible':True}]}]},output_path=tmp_path/'plan.json')
    result=asyncio.run(plan_frozen_compound_edit(**kwargs))
    assert len(calls)==2 and 'initially hidden' in calls[1]['messages'][-1]['content']
    assert result['usage']['input_tokens']==20
    replay=asyncio.run(plan_frozen_compound_edit(**kwargs))
    assert len(calls)==2 and replay['frozen_plan_sha256']==result['frozen_plan_sha256']


def test_planner_rejects_menu_control_hidden_by_an_earlier_entry(tmp_path, monkeypatch):
    from src.agents.compound_edit_planner import plan_frozen_compound_edit
    from src.agents.openai_runner import OpenAIHTTPClient
    from src.config import HarnessConfig
    tasks=frozen_tasks(); bad=raw_plan(tasks); good=raw_plan(tasks); calls=[]
    bad['subtasks'][0]['atomic_plan']['checks'][0]['actions'][:0]=[
        {'action':'click','selector':'#start'}, {'action':'click','selector':'#menu-feature'}]
    good['subtasks'][0]['atomic_plan']['checks'][0]['actions'][:0]=[
        {'action':'click','selector':'#menu-feature'}, {'action':'click','selector':'#start'}]
    async def complete(self, **kwargs):
        calls.append(kwargs)
        return {'choices':[{'message':{'content':json.dumps(bad if len(calls)==1 else good)}}]}
    monkeypatch.setattr(OpenAIHTTPClient,'complete',complete)
    kwargs=dict(config=HarnessConfig(edit_skills_enabled=True,planner_model='gpt-5.6-luna'),
        case_id='case',source_code_sha256='abc',frozen_subtasks=tasks,
        source_ui_contract={'pages':[{'route':'/','controls':[],
            'entry_transitions':[{'click':'#start','hides':['#menu-feature']}]}]},
        output_path=tmp_path/'plan.json')
    asyncio.run(plan_frozen_compound_edit(**kwargs))
    assert len(calls)==2
    assert 'source entry navigation hides #menu-feature' in calls[1]['messages'][-1]['content']
