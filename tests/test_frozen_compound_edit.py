from __future__ import annotations

import copy

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
