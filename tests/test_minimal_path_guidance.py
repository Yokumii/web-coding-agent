from __future__ import annotations

import json
from pathlib import Path

from src.orchestration.minimal_path_guidance import (
    MinimalPathPolicy,
    ensure_minimal_path_plan,
)


def _write_contract(workdir: Path) -> None:
    harness = workdir / ".harness"
    harness.mkdir()
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-001",
                                "feature_id": "F001",
                                "task": "Use the search box",
                                "expected_result": "Results update",
                                "critical": True,
                                "category": "interaction",
                                "actions": [
                                    {
                                        "action": "fill",
                                        "selector": "#catalog-search",
                                        "value": "camera",
                                    },
                                    {
                                        "action": "evaluate",
                                        "expression": (
                                            "document.querySelector('#catalog-search').value "
                                            "=== 'camera'"
                                        ),
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (harness / "edit_dom_source_sprint_1.json").write_text(
        json.dumps(
            {
                "version": 2,
                "roots": [
                    {
                        "key": "main:catalog",
                        "fingerprint": "before",
                        "anchors": ["#catalog-search", "[data-testid=\"catalog\"]"],
                    },
                    {
                        "key": "footer:unnamed",
                        "fingerprint": "stable",
                        "anchors": ["#legal-links"],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_harness_builds_change_cone_from_action_contract_and_dom(tmp_path: Path):
    workdir = tmp_path
    frontend = workdir / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "App.jsx").write_text(
        "import './catalog.css';\n"
        "export default () => <input id=\"catalog-search\" />;\n",
        encoding="utf-8",
    )
    (frontend / "src" / "catalog.css").write_text(
        "#catalog-search { width: 12rem; }\n", encoding="utf-8"
    )
    (frontend / "src" / "unrelated.jsx").write_text(
        "export const Legal = () => <footer id=\"legal-links\" />;\n",
        encoding="utf-8",
    )
    _write_contract(workdir)

    plan = ensure_minimal_path_plan(
        workdir=workdir,
        harness_dir=workdir / ".harness",
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=80,
        max_touched_files=3,
    )

    assert plan["owner"] == "harness"
    assert plan["target_contract"]["selectors"] == ["#catalog-search"]
    assert plan["dom_change_cone"]["allowed_root_keys"] == ["main:catalog"]
    assert plan["source_change_cone"]["local_paths"] == [
        "frontend/src/App.jsx",
        "frontend/src/catalog.css",
    ]
    assert "frontend/src/unrelated.jsx" in plan["source_change_cone"]["protected_paths"]
    scope = json.loads(
        (workdir / ".harness" / "edit_scope_round_1.json").read_text(encoding="utf-8")
    )
    assert scope == {
        "schema_version": "edit-scope-v2",
        "owner": "harness",
        "plan": ".harness/minimal_path_plan_round_1.json",
        "baseline": ".harness/edit_dom_source_sprint_1.json",
        "allowed_root_keys": ["main:catalog"],
        "allow_new_roots": False,
    }


def test_policy_guides_local_patch_and_rejects_collateral_source(tmp_path: Path):
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    target = frontend / "src" / "App.jsx"
    target.write_text("const label = 'old';\n", encoding="utf-8")
    unrelated = frontend / "src" / "unrelated.jsx"
    unrelated.write_text("const footer = true;\n", encoding="utf-8")
    plan = {
        "schema_version": "minimal-path-plan-v1",
        "owner": "harness",
        "round": 1,
        "source_change_cone": {
            "local_paths": ["frontend/src/App.jsx"],
            "dependency_paths": [],
            "protected_paths": ["frontend/src/unrelated.jsx"],
        },
        "budgets": {"max_patch_lines": 10, "max_touched_files": 2},
        "dom_change_cone": {"allow_new_roots": False},
    }
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "minimal_path_plan_round_1.json").write_text(json.dumps(plan))
    policy = MinimalPathPolicy.from_plan(tmp_path, plan)

    overwrite = policy.check(
        "write_file", {"path": "frontend/src/App.jsx", "content": "replacement"}
    )
    local_patch = policy.check(
        "apply_patch",
        {
            "path": "frontend/src/App.jsx",
            "old_text": "'old'",
            "new_text": "'new'",
        },
    )
    collateral = policy.check(
        "apply_patch",
        {
            "path": "frontend/src/unrelated.jsx",
            "old_text": "true",
            "new_text": "false",
        },
    )

    assert overwrite is not None and "exact patch" in overwrite
    assert local_patch is None
    assert collateral is not None and "outside the harness change cone" in collateral
    ledger = [
        json.loads(line)
        for line in (harness / "minimal_path_ledger_round_1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [item["decision"] for item in ledger] == ["deny", "allow", "deny"]


def test_policy_expands_only_along_recorded_dependency_edge(tmp_path: Path):
    frontend = tmp_path / "frontend" / "src"
    frontend.mkdir(parents=True)
    (frontend / "App.jsx").write_text("import './widget.css';\n")
    dependency = frontend / "widget.css"
    dependency.write_text(".widget { color: black; }\n")
    unrelated = frontend / "admin.css"
    unrelated.write_text(".admin { color: black; }\n")
    (tmp_path / ".harness").mkdir()
    plan = {
        "schema_version": "minimal-path-plan-v1",
        "owner": "harness",
        "round": 2,
        "source_change_cone": {
            "local_paths": ["frontend/src/App.jsx"],
            "dependency_paths": ["frontend/src/widget.css"],
            "protected_paths": ["frontend/src/admin.css"],
            "dependency_edges": [
                {
                    "from": "frontend/src/App.jsx",
                    "to": "frontend/src/widget.css",
                }
            ],
        },
        "budgets": {"max_patch_lines": 20, "max_touched_files": 3},
        "dom_change_cone": {"allow_new_roots": False},
    }
    policy = MinimalPathPolicy.from_plan(tmp_path, plan)

    expanded = policy.check(
        "Edit",
        {
            "file_path": str(dependency),
            "old_string": "black",
            "new_string": "navy",
        },
    )
    denied = policy.check(
        "Edit",
        {
            "file_path": str(unrelated),
            "old_string": "black",
            "new_string": "navy",
        },
    )

    assert expanded is None
    assert denied is not None
    ledger = [
        json.loads(line)
        for line in (tmp_path / ".harness" / "minimal_path_ledger_round_2.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert ledger[0]["scope_tier"] == "dependency"
    assert ledger[0]["expansion_reason"] == "recorded_dependency_edge"


def test_policy_rejects_large_patch_and_mutating_bash(tmp_path: Path):
    source = tmp_path / "frontend" / "index.html"
    source.parent.mkdir()
    source.write_text("\n".join(f"line {index}" for index in range(40)))
    (tmp_path / ".harness").mkdir()
    plan = {
        "schema_version": "minimal-path-plan-v1",
        "owner": "harness",
        "round": 3,
        "source_change_cone": {
            "local_paths": ["frontend/index.html"],
            "dependency_paths": [],
            "protected_paths": [],
        },
        "budgets": {"max_patch_lines": 5, "max_touched_files": 1},
        "dom_change_cone": {"allow_new_roots": True},
    }
    policy = MinimalPathPolicy.from_plan(tmp_path, plan)

    large = policy.check(
        "apply_patch",
        {
            "path": "frontend/index.html",
            "old_text": "\n".join(f"line {index}" for index in range(10)),
            "new_text": "replacement",
        },
    )
    command = policy.check(
        "run_command", {"command": "cp frontend/index.html frontend/copy.html"}
    )

    assert large is not None and "patch-line budget" in large
    assert command is not None and "mutation tools" in command
