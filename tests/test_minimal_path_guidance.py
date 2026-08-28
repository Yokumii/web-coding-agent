from __future__ import annotations

import json
from pathlib import Path

from src.orchestration.minimal_path_guidance import (
    MinimalPathPolicy,
    _fragment_dom_scope,
    _validate_target_scoped_css,
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
                        "anchors": ["#catalog-search", '[data-testid="catalog"]'],
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


def test_fragment_scope_tracks_route_transitions_and_groups_new_subtrees():
    baseline = {
        "version": 4,
        "fragments": [
            {
                "key": "/library.html::home-link",
                "route": "/library.html",
                "anchors": ['a[href="/"]'],
            }
        ],
    }
    checks = [
        {
            "route": "/library.html",
            "actions": [
                {
                    "action": "assert_count",
                    "selector": ".catalog-item",
                    "count": 5,
                },
                {"action": "fill", "selector": "#catalog-filter", "value": "Dune"},
                {
                    "action": "click",
                    "selector": ".catalog-item:first-child .save-btn",
                },
                {"action": "click", "selector": "nav a[href='/']"},
                {"action": "assert_url", "value": "/"},
                {"action": "assert_visible", "selector": ".book-item"},
            ],
        }
    ]

    allowed, expected, evidence, unresolved = _fragment_dom_scope(
        baseline,
        checks,
        ["/", "/library.html"],
        existing_source_selectors={".book-item"},
    )

    assert allowed == ["/library.html::home-link"]
    assert expected == [
        {"route": "/library.html", "selector": ".catalog-item", "max_count": 5},
        {"route": "/library.html", "selector": "#catalog-filter", "max_count": 1},
    ]
    assert unresolved == []
    assert any(
        item.get("selector") == ".book-item"
        and item.get("route") == "/"
        and item.get("resolution") == "runtime_state_source_anchor"
        for item in evidence
    )
    assert any(
        item.get("selector") == ".catalog-item:first-child .save-btn"
        and item.get("resolution") == "covered_by_expected_subtree"
        for item in evidence
    )
def test_harness_builds_change_cone_from_action_contract_and_dom(tmp_path: Path):
    workdir = tmp_path
    frontend = workdir / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "App.jsx").write_text(
        "import './catalog.css';\n"
        'export default () => <input id="catalog-search" />;\n',
        encoding="utf-8",
    )
    (frontend / "src" / "catalog.css").write_text(
        "#catalog-search { width: 12rem; }\n", encoding="utf-8"
    )
    (frontend / "src" / "unrelated.jsx").write_text(
        'export const Legal = () => <footer id="legal-links" />;\n',
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
    assert plan["source_change_cone"]["initial_paths"] == ["frontend/src/App.jsx"]
    assert plan["source_change_cone"]["hotspots"][0]["role_evidence"] == ["behavior"]
    scope = json.loads(
        (workdir / ".harness" / "edit_scope_round_1.json").read_text(encoding="utf-8")
    )
    assert scope == {
        "schema_version": "edit-scope-v4",
        "owner": "harness",
        "plan": ".harness/minimal_path_plan_round_1.json",
        "baseline": ".harness/edit_dom_source_sprint_1.json",
        "allowed_root_keys": ["main:catalog"],
        "allow_new_roots": False,
        "allowed_fragment_keys": [],
        "expected_new_fragments": [],
        "expected_new_routes": [],
        "target_routes": ["/"],
        "protected_routes": [],
    }


def test_visual_contract_routes_initial_path_to_style_source(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<main id="hero" data-copy="#hero #hero #hero">Hello</main>\n'
    )
    (frontend / "styles.css").write_text("#hero { color: navy; }\n")
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-VISUAL",
                                "category": "visual",
                                "actions": [
                                    {
                                        "action": "click",
                                        "selector": "#hero",
                                    },
                                    {
                                        "action": "evaluate",
                                        "expression": "document.querySelector('#hero') !== null",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=20,
        max_touched_files=2,
    )

    assert plan["target_contract"]["requested_source_roles"] == ["style"]
    assert plan["source_change_cone"]["initial_paths"] == ["frontend/styles.css"]


def test_repair_prioritizes_source_role_of_failed_typed_checks(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<link rel="stylesheet" href="styles.css">\n'
        '<div id="catalog"><button class="import-btn">Import</button></div>\n'
        '<script src="app.js"></script>\n',
        encoding="utf-8",
    )
    (frontend / "styles.css").write_text(
        "\n".join(["#catalog .import-btn { color: navy; }"] * 8),
        encoding="utf-8",
    )
    (frontend / "app.js").write_text(
        "document.querySelector('.import-btn').addEventListener('click', () => {});\n",
        encoding="utf-8",
    )
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-VISUAL",
                                "category": "visual",
                                "actions": [
                                    {
                                        "action": "assert_visible",
                                        "selector": "#catalog .import-btn",
                                    }
                                ],
                            },
                            {
                                "id": "UI-IMPORT",
                                "category": "functional",
                                "actions": [
                                    {
                                        "action": "click",
                                        "selector": ".import-btn",
                                    }
                                ],
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (harness / "grade_round_1.json").write_text(
        json.dumps(
            {
                "ui_checks": [
                    {"check_id": "UI-VISUAL", "status": "pass"},
                    {"check_id": "UI-IMPORT", "status": "fail"},
                ]
            }
        ),
        encoding="utf-8",
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=2,
        sprint_num=1,
        mode="repair",
        max_patch_lines=20,
        max_touched_files=2,
    )

    assert plan["target_contract"]["requested_source_roles"] == [
        "behavior",
        "style",
    ]
    assert plan["target_contract"]["priority_source_roles"] == ["behavior"]
    assert plan["target_contract"]["failed_check_ids"] == ["UI-IMPORT"]
    assert plan["source_change_cone"]["initial_paths"] == ["frontend/app.js"]
    assert plan["source_change_cone"]["hotspots"][0]["path"] == "frontend/app.js"


def test_semantic_guard_only_repair_does_not_route_to_stylesheet(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<link rel="stylesheet" href="styles.css">\n'
        '<div id="catalog"><button class="import-btn">Import</button></div>\n'
        '<script src="app.js"></script>\n',
        encoding="utf-8",
    )
    (frontend / "styles.css").write_text(
        "\n".join(["#catalog .import-btn { color: navy; }"] * 8),
        encoding="utf-8",
    )
    (frontend / "app.js").write_text(
        "document.querySelector('.import-btn').addEventListener('click', () => {});\n",
        encoding="utf-8",
    )
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-VISUAL",
                                "category": "visual",
                                "actions": [
                                    {"action": "assert_visible", "selector": "#catalog"}
                                ],
                            },
                            {
                                "id": "UI-IMPORT",
                                "category": "functional",
                                "actions": [
                                    {"action": "click", "selector": ".import-btn"}
                                ],
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (harness / "grade_round_1.json").write_text(
        json.dumps(
            {
                "ui_checks": [
                    {"check_id": "UI-VISUAL", "status": "pass"},
                    {"check_id": "UI-IMPORT", "status": "pass"},
                ],
                "edit_guard": {
                    "passed": False,
                    "violations": [
                        {"fragment": "button:Other", "kind": "removed"}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=2,
        sprint_num=1,
        mode="repair",
        max_patch_lines=20,
        max_touched_files=2,
    )

    assert plan["target_contract"]["priority_source_roles"] == ["behavior"]
    assert plan["source_change_cone"]["initial_paths"] == ["frontend/app.js"]


def test_change_cone_can_widen_to_file_that_references_initial_source(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<input id="query"><script src="./search.js"></script>\n'
    )
    (frontend / "search.js").write_text(
        "document.querySelector('#query').addEventListener('input', () => {});\n"
    )
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-SEARCH",
                                "category": "interaction",
                                "actions": [
                                    {
                                        "action": "fill",
                                        "selector": "#query",
                                        "value": "camera",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )
    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=20,
        max_touched_files=2,
    )

    assert plan["source_change_cone"]["initial_paths"] == ["frontend/search.js"]
    assert "frontend/index.html" in plan["source_change_cone"]["local_paths"]
    policy = MinimalPathPolicy.from_plan(tmp_path, plan)
    policy.observe_result(
        "read_file", {"path": "frontend/search.js"}, ok=True, output="source"
    )
    patch = {
        "path": "frontend/search.js",
        "old_text": "() => {}",
        "new_text": "event => event.target.value",
    }
    assert policy.check("apply_patch", patch) is None
    policy.observe_result("apply_patch", patch, ok=True, output="patched")
    policy.observe_result(
        "run_command", {"command": "git -C frontend diff --check"}, ok=True, output=""
    )
    policy.observe_result(
        "read_file", {"path": "frontend/index.html"}, ok=True, output="source"
    )
    html_patch = {
        "path": "frontend/index.html",
        "old_text": '<input id="query">',
        "new_text": '<input id="query" type="search">',
    }

    assert policy.check("apply_patch", html_patch) is None
    state = json.loads((harness / "minimal_path_state_round_1.json").read_text())
    assert "frontend/index.html" in state["unlocked_paths"]
    assert state["validation_last_ok"] is True
    assert state["phase"] == "validated"


def test_multi_page_edit_scopes_dependencies_to_target_route(tmp_path: Path):
    frontend = tmp_path / "frontend"
    (frontend / "pages").mkdir(parents=True)
    (frontend / "shared").mkdir()
    (frontend / "index.html").write_text(
        '<a href="/catalog.html">Catalog</a><a href="/settings.html">Settings</a>\n'
    )
    (frontend / "catalog.html").write_text(
        '<link rel="stylesheet" href="./shared/site.css">\n'
        '<link rel="stylesheet" href="./pages/catalog.css">\n'
        '<input id="catalog-filter"><script src="./pages/catalog.js"></script>\n'
    )
    (frontend / "settings.html").write_text(
        '<link rel="stylesheet" href="./shared/site.css">\n'
        '<input id="profile-name"><script src="./pages/settings.js"></script>\n'
    )
    (frontend / "pages" / "catalog.js").write_text(
        "document.querySelector('#catalog-filter').addEventListener('input', () => {});\n"
    )
    (frontend / "pages" / "catalog.css").write_text(
        "#catalog-filter { inline-size: 20rem; }\n"
    )
    (frontend / "pages" / "settings.js").write_text(
        "document.querySelector('#profile-name').addEventListener('input', () => {});\n"
    )
    (frontend / "shared" / "site.css").write_text("body { color: #222; }\n")
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-CATALOG",
                                "route": "/catalog.html",
                                "category": "interaction",
                                "actions": [
                                    {
                                        "action": "fill",
                                        "selector": "#catalog-filter",
                                        "value": "camera",
                                    },
                                    {
                                        "action": "evaluate",
                                        "expression": "document.querySelector('#catalog-filter').value === 'camera'",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=30,
        max_touched_files=3,
    )

    route_scope = plan["route_scope"]
    assert route_scope["status"] == "multi_page_scoped"
    assert route_scope["target_routes"] == ["/catalog.html"]
    assert route_scope["target_page_entries"] == ["frontend/catalog.html"]
    assert route_scope["route_local_paths"] == [
        "frontend/catalog.html",
        "frontend/pages/catalog.css",
        "frontend/pages/catalog.js",
    ]
    assert route_scope["cross_route_shared_paths"] == ["frontend/shared/site.css"]
    assert "frontend/pages/settings.js" in route_scope["off_target_paths"]
    assert plan["source_change_cone"]["initial_paths"] == ["frontend/pages/catalog.js"]
    assert "frontend/shared/site.css" not in plan["source_change_cone"]["local_paths"]
    assert {
        "from": "frontend/index.html",
        "to": "frontend/catalog.html",
    } not in plan["source_change_cone"]["dependency_edges"]

    policy = MinimalPathPolicy.from_plan(tmp_path, plan)
    policy.observe_result(
        "read_file",
        {"path": "frontend/shared/site.css"},
        ok=True,
        output="shared",
    )
    denial = policy.check(
        "apply_patch",
        {
            "path": "frontend/shared/site.css",
            "old_text": "#222",
            "new_text": "#111",
        },
    )
    assert denial is not None and "non-target routes" in denial


def test_static_target_without_behavior_source_gets_isolated_companion_plan(
    tmp_path: Path,
):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<main id="dashboard"></main><script src="./app.js"></script>\n'
    )
    (frontend / "library.html").write_text(
        '<link rel="stylesheet" href="./styles.css">\n'
        '<main class="catalog-section"><h1>Library</h1></main>\n'
    )
    (frontend / "app.js").write_text(
        "const STORAGE_KEY = 'books';\n"
        "document.querySelector('#dashboard').textContent = localStorage.getItem(STORAGE_KEY);\n"
    )
    (frontend / "styles.css").write_text(".catalog-section { display: grid; }\n")
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-LIBRARY",
                                "route": "/library.html",
                                "category": "interaction",
                                "actions": [
                                    {
                                        "action": "fill",
                                        "selector": "#catalog-filter",
                                        "value": "Dune",
                                    },
                                    {
                                        "action": "assert_count",
                                        "selector": ".catalog-item",
                                        "count": 1,
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=120,
        max_touched_files=3,
    )

    strategy = plan["source_change_cone"]["route_isolation_strategy"]
    assert strategy == {
        "status": "recommended",
        "routes": [
            {
                "route": "/library.html",
                "entry_path": "frontend/library.html",
                "planned_companion_path": "frontend/library.js",
            }
        ],
        "reason": "interactive static route has no route-owned behavior source",
        "preserve_existing_behavior_sources": ["frontend/app.js"],
    }
    assert plan["source_change_cone"]["initial_paths"] == [
        "frontend/library.html"
    ]
    assert "frontend/library.js" in plan["source_change_cone"]["planned_new_paths"]
    assert "frontend/library.js" in plan["source_change_cone"]["dependency_paths"]
    assert "frontend/app.js" in plan["source_change_cone"]["protected_paths"]
    assert {
        "from": "frontend/library.html",
        "to": "frontend/library.js",
        "kind": "planned_route_companion",
    } in plan["source_change_cone"]["dependency_edges"]

    policy = MinimalPathPolicy.from_plan(tmp_path, plan)
    policy.observe_result(
        "read_file",
        {"path": "frontend/library.html"},
        ok=True,
        output="library",
    )
    entry_patch = {
        "path": "frontend/library.html",
        "old_text": "</main>",
        "new_text": '</main><script src="./library.js"></script>',
    }
    assert policy.check("apply_patch", entry_patch) is None
    policy.observe_result("apply_patch", entry_patch, ok=True, output="patched")
    planned_write = {
        "path": "frontend/library.js",
        "content": "document.querySelector('#catalog-filter');\n",
    }
    assert "validation attempt" in policy.check("write_file", planned_write)
    policy.observe_result(
        "run_command", {"command": "git diff --check"}, ok=True, output=""
    )
    assert policy.check("write_file", planned_write) is None
    assert "unplanned new source" in policy.check(
        "write_file",
        {"path": "frontend/extra.js", "content": "export {};\n"},
    )


def test_generate_can_plan_one_new_static_route_without_widening_existing_page(
    tmp_path: Path,
):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<main id="dashboard"></main><script src="./app.js"></script>\n'
    )
    (frontend / "app.js").write_text(
        "document.querySelector('#dashboard').textContent = 'accepted';\n"
    )
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-NEW-PAGE",
                                "route": "/library.html",
                                "category": "interaction",
                                "actions": [
                                    {"action": "fill", "selector": "#search", "value": "Dune"},
                                    {"action": "assert_count", "selector": ".catalog-item", "count": 1},
                                ],
                            },
                            {
                                "id": "UI-NEW-PAGE-VISUAL",
                                "route": "/library.html",
                                "category": "visual",
                                "actions": [
                                    {"action": "assert_visible", "selector": ".catalog-grid"},
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=120,
        max_touched_files=3,
    )

    assert plan["status"] == "ready"
    assert plan["route_scope"]["target_routes"] == ["/library.html"]
    assert plan["route_scope"]["planned_new_route_entries"] == [
        "frontend/library.html"
    ]
    assert plan["dom_change_cone"]["expected_new_routes"] == ["/library.html"]
    assert plan["source_change_cone"]["initial_paths"] == [
        "frontend/library.html"
    ]
    assert plan["source_change_cone"]["planned_new_paths"] == [
        "frontend/library.css",
        "frontend/library.html",
        "frontend/library.js",
    ]
    assert "frontend/app.js" in plan["source_change_cone"]["protected_paths"]

    policy = MinimalPathPolicy.from_plan(tmp_path, plan)
    planned_page = {
        "path": "frontend/library.html",
        "content": '<main><input id="search"></main><script src="library.js"></script>\n',
    }
    assert policy.check("write_file", planned_page) is None
    assert "unplanned new source" in policy.check(
        "write_file",
        {"path": "frontend/settings.html", "content": "<main>Settings</main>\n"},
    )


def test_explicit_edit_contract_rejects_planner_route_drift(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "catalog.html").write_text('<input id="catalog-filter">\n')
    (frontend / "settings.html").write_text('<input id="profile-name">\n')
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "edit_task_contract.json").write_text(json.dumps({
        "schema_version": "edit-task-contract-v1",
        "task_mode": "edit",
        "requested_target_routes": ["/catalog.html"],
        "protect_non_target_routes": True,
    }))
    (harness / "ui_verification_plan.json").write_text(json.dumps({
        "sprints": [{"sprint": 1, "checks": [{
            "id": "UI-WRONG", "route": "/settings.html", "category": "interaction",
            "actions": [{"action": "evaluate", "expression": "true"}],
        }]}],
    }))

    plan = ensure_minimal_path_plan(
        workdir=tmp_path, harness_dir=harness, round_num=1, sprint_num=1,
        mode="generate", max_patch_lines=30, max_touched_files=3,
    )

    assert plan["status"] == "blocked"
    assert plan["route_scope"]["status"] == "target_route_contract_mismatch"
    assert plan["route_scope"]["target_routes"] == ["/catalog.html"]
    assert plan["route_scope"]["protected_routes"] == ["/settings.html"]
    assert plan["route_scope"]["unexpected_check_routes"] == ["/settings.html"]


def test_multi_route_edit_contract_opens_only_current_sprint_route(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "catalog.html").write_text('<input id="catalog-filter">\n')
    (frontend / "settings.html").write_text('<input id="profile-name">\n')
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "edit_task_contract.json").write_text(json.dumps({
        "schema_version": "edit-task-contract-v1",
        "task_mode": "edit",
        "requested_target_routes": ["/catalog.html", "/settings.html"],
        "protect_non_target_routes": True,
    }))
    (harness / "ui_verification_plan.json").write_text(json.dumps({
        "sprints": [{"sprint": 1, "checks": [{
            "id": "UI-CATALOG", "route": "/catalog.html", "category": "interaction",
            "actions": [{"action": "evaluate", "expression": "true"}],
        }]}],
    }))

    plan = ensure_minimal_path_plan(
        workdir=tmp_path, harness_dir=harness, round_num=1, sprint_num=1,
        mode="generate", max_patch_lines=30, max_touched_files=3,
    )

    assert plan["status"] == "ready"
    assert plan["route_scope"]["contract_allowed_routes"] == [
        "/catalog.html", "/settings.html",
    ]
    assert plan["route_scope"]["target_routes"] == ["/catalog.html"]
    assert plan["route_scope"]["protected_routes"] == ["/settings.html"]


def test_shared_file_is_admissible_when_every_owner_route_is_targeted(tmp_path: Path):
    frontend = tmp_path / "frontend"
    (frontend / "shared").mkdir(parents=True)
    for page in ("catalog", "settings"):
        (frontend / f"{page}.html").write_text(
            '<script src="./shared/navigation.js"></script>\n'
            '<button class="global-nav">Open</button>\n'
        )
    (frontend / "shared" / "navigation.js").write_text(
        "document.querySelectorAll('.global-nav').forEach(button => "
        "button.addEventListener('click', () => {}));\n"
    )
    harness = tmp_path / ".harness"
    harness.mkdir()
    checks = []
    for index, page in enumerate(("catalog", "settings"), start=1):
        checks.append(
            {
                "id": f"UI-{index}",
                "route": f"/{page}.html",
                "category": "interaction",
                "actions": [
                    {"action": "click", "selector": ".global-nav"},
                    {"action": "evaluate", "expression": "true"},
                ],
            }
        )
    (harness / "ui_verification_plan.json").write_text(
        json.dumps({"sprints": [{"sprint": 1, "checks": checks}]})
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=30,
        max_touched_files=3,
    )

    assert plan["route_scope"]["target_routes"] == [
        "/catalog.html",
        "/settings.html",
    ]
    assert plan["route_scope"]["target_shared_paths"] == [
        "frontend/shared/navigation.js"
    ]
    assert plan["route_scope"]["cross_route_shared_paths"] == []
    assert plan["source_change_cone"]["initial_paths"] == [
        "frontend/shared/navigation.js"
    ]


def test_shared_file_opens_only_the_named_target_route_region(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    for page, module in (("catalog", "Catalog"), ("settings", "Settings")):
        (frontend / f"{page}.html").write_text(
            '<script src="./app.js"></script>\n'
            f'<script>{module}.init()</script>\n'
        )
    (frontend / "app.js").write_text(
        "/* --- CATALOG MODULE --- */\n"
        "const Catalog = {\n"
        "    init: () => {},\n"
        "    render: () => 'old catalog'\n"
        "};\n\n"
        "/* --- SETTINGS MODULE --- */\n"
        "const Settings = {\n"
        "    init: () => {},\n"
        "    render: () => 'old settings'\n"
        "};\n"
    )
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "edit_task_contract.json").write_text(
        json.dumps({"requested_target_routes": ["/catalog.html"]})
    )
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-CATALOG-PAGE",
                                "route": "/catalog.html",
                                "category": "interaction",
                                "actions": [
                                    {
                                        "action": "assert_text",
                                        "selector": "#current-page",
                                        "value": "1",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=30,
        max_touched_files=2,
    )

    assert plan["route_scope"]["cross_route_shared_paths"] == ["frontend/app.js"]
    assert plan["source_change_cone"]["guarded_shared_regions"] == [
        {
            "path": "frontend/app.js",
            "route": "/catalog.html",
            "symbol": "Catalog",
            "kind": "object",
            "start_line": 1,
            "end_line": 5,
        }
    ]
    assert "frontend/app.js" in plan["source_change_cone"]["local_paths"]
    assert {"from": "frontend/catalog.html", "to": "frontend/app.js"} in plan[
        "source_change_cone"
    ]["dependency_edges"]

    policy = MinimalPathPolicy.from_plan(tmp_path, plan)
    shared_before = (frontend / "app.js").read_text()
    assert (
        policy.validate_guarded_shared_file(
            "frontend/app.js",
            before=shared_before,
            after=shared_before.replace("old catalog", "new catalog"),
        )
        is None
    )
    assert "outside" in str(
        policy.validate_guarded_shared_file(
            "frontend/app.js",
            before=shared_before,
            after=shared_before.replace("old settings", "changed settings"),
        )
    )
    assert "cannot be resolved" in str(
        policy.validate_guarded_shared_file(
            "frontend/app.js",
            before=shared_before,
            after=shared_before.replace("const Catalog", "const CatalogRenamed"),
        )
    )
    policy.observe_result(
        "read_file",
        {"path": "frontend/catalog.html"},
        ok=True,
        output="source",
    )
    assert (
        policy.check(
            "apply_patch",
            {
                "path": "frontend/catalog.html",
                "old_text": '<script>Catalog.init()</script>',
                "new_text": '<main id="current-page">1</main>',
            },
        )
        is None
    )
    policy.observe_result(
        "apply_patch",
        {"path": "frontend/catalog.html"},
        ok=True,
        output="done",
    )
    policy.observe_validation(ok=False, output="target control is not wired", tool="test")
    policy.observe_result(
        "read_file",
        {"path": "frontend/app.js"},
        ok=True,
        output="source",
    )

    assert (
        policy.check(
            "apply_patch",
            {
                "path": "frontend/app.js",
                "old_text": "render: () => 'old catalog'",
                "new_text": "render: () => 'new catalog'",
            },
        )
        is None
    )
    settings_denial = policy.check(
        "apply_patch",
        {
            "path": "frontend/app.js",
            "old_text": "render: () => 'old settings'",
            "new_text": "render: () => 'changed settings'",
        },
    )
    assert settings_denial is not None
    assert "guarded target-route region" in settings_denial
    overwrite_denial = policy.check(
        "write_file", {"path": "frontend/app.js", "content": "replacement"}
    )
    assert overwrite_denial is not None
    assert "cannot be overwritten" in overwrite_denial


def test_vanilla_hash_router_maps_view_to_source_and_protects_siblings(tmp_path: Path):
    frontend = tmp_path / "frontend"
    (frontend / "src" / "views").mkdir(parents=True)
    (frontend / "index.html").write_text(
        '<link rel="stylesheet" href="styles.css">'
        '<main id="main-content"></main><script type="module" src="src/app.js"></script>\n'
    )
    (frontend / "styles.css").write_text(".page-btn { padding: .5rem; }\n")
    (frontend / "src" / "app.js").write_text(
        "import { registerRoute } from './router.js';\n"
        "import { renderReport } from './views/report.js';\n"
        "import { renderBoard } from './views/board.js';\n"
        "registerRoute('/report', renderReport);\n"
        "registerRoute('/board', renderBoard);\n"
    )
    (frontend / "src" / "router.js").write_text("export function registerRoute() {}\n")
    (frontend / "src" / "views" / "report.js").write_text(
        "export function renderReport() { return 'report'; }\n"
    )
    (frontend / "src" / "views" / "board.js").write_text(
        "export function renderBoard() { return 'board'; }\n"
    )
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "edit_task_contract.json").write_text(
        json.dumps({"requested_target_routes": ["/#/report"]})
    )
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "REPORT-PAGE",
                                "route": "/#/report",
                                "category": "interaction",
                                "actions": [
                                    {
                                        "action": "assert_computed_style",
                                        "selector": "#report-pagination",
                                        "property": "display",
                                        "value": "flex",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=40,
        max_touched_files=6,
    )

    assert plan["route_scope"]["target_routes"] == ["/#/report"]
    assert plan["route_scope"]["protected_routes"] == ["/#/board"]
    assert "frontend/src/views/report.js" in plan["route_scope"]["route_local_paths"]
    assert "frontend/src/views/board.js" in plan["route_scope"]["off_target_paths"]
    assert plan["route_scope"]["global_style_paths"] == ["frontend/styles.css"]
    assert "frontend/styles.css" in plan["route_scope"]["cross_route_shared_paths"]
    assert plan["source_change_cone"]["initial_paths"] == [
        "frontend/src/views/report.js"
    ]
    assert "frontend/styles.css" in plan["source_change_cone"]["dependency_paths"]
    css_contract = next(
        item
        for item in plan["source_change_cone"]["guarded_shared_regions"]
        if item.get("mutation_mode") == "target_scoped_css"
    )
    assert css_contract["allowed_anchors"] == ["#report-pagination"]


def test_plan_indexes_existing_css_tokens_for_design_system_guidance(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<link rel="stylesheet" href="styles.css"><main id="panel">Panel</main>\n'
    )
    (frontend / "styles.css").write_text(
        ":root { --color-primary: #245; --space-3: 0.75rem; }\n"
        "#panel { color: var(--color-primary); padding: var(--space-3); }\n"
    )
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "PANEL",
                                "route": "/",
                                "category": "visual",
                                "actions": [
                                    {"action": "assert_visible", "selector": "#panel"}
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=40,
        max_touched_files=3,
    )

    assert plan["design_system_context"]["status"] == "tokens_found"
    assert plan["design_system_context"]["css_custom_properties"] == [
        {
            "name": "--color-primary",
            "defined_in": ["frontend/styles.css"],
            "usage_count": 1,
        },
        {
            "name": "--space-3",
            "defined_in": ["frontend/styles.css"],
            "usage_count": 1,
        },
    ]


def test_shared_state_container_allows_only_target_named_additions(tmp_path: Path):
    frontend = tmp_path / "frontend"
    (frontend / "js").mkdir(parents=True)
    for page, script in (
        ("index.html", "dashboard.js"),
        ("report.html", "report.js"),
        ("staff.html", "staff.js"),
    ):
        (frontend / page).write_text(
            f'<main id="{Path(page).stem}"></main>'
            '<script type="module" src="js/store.js"></script>'
            f'<script type="module" src="js/{script}"></script>\n'
        )
        (frontend / "js" / script).write_text(
            "import {} from './store.js';\n"
            f"document.querySelector('#{Path(page).stem}');\n"
        )
    store_before = (
        "const INITIAL_STATE = {\n"
        "  logs: [],\n"
        "  activeProgramId: null\n"
        "};\n"
        "class Store {\n"
        "  clearLogs() { this.state.logs = []; this.save(); }\n"
        "  save() {}\n"
        "}\n"
    )
    (frontend / "js" / "store.js").write_text(store_before)
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "edit_task_contract.json").write_text(
        json.dumps(
            {"requested_target_routes": ["/", "/report.html"]}
        )
    )
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "DASHBOARD-PAGE",
                                "route": "/",
                                "category": "state",
                                "actions": [
                                    {
                                        "action": "assert_text",
                                        "selector": "#dashboardPageInfo",
                                        "value": "Page 1",
                                        "match": "contains",
                                    }
                                ],
                            },
                            {
                                "id": "REPORT-PAGE",
                                "route": "/report.html",
                                "category": "persistence",
                                "actions": [
                                    {
                                        "action": "assert_text",
                                        "selector": "#reportPageInfo",
                                        "value": "Page 1",
                                        "match": "contains",
                                    }
                                ],
                            },
                        ],
                    }
                ]
            }
        )
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=60,
        max_touched_files=6,
    )
    contracts = plan["source_change_cone"]["guarded_shared_regions"]
    assert {
        item["symbol"] for item in contracts if item.get("mutation_mode") == "additive_target_members"
    } == {"INITIAL_STATE", "Store"}
    assert plan["source_change_cone"]["initial_paths"] == [
        "frontend/index.html",
        "frontend/report.html",
    ]
    assert "frontend/js/store.js" in plan["source_change_cone"]["local_paths"]

    policy = MinimalPathPolicy.from_plan(tmp_path, plan)
    target_additions = store_before.replace(
        "  activeProgramId: null\n",
        "  activeProgramId: null,\n"
        "  dashboardPage: 1,\n"
        "  reportPage: 1,\n"
        "  pageSize: 4\n",
    ).replace(
        "  save() {}\n",
        "  setDashboardPage(page) { this.state.dashboardPage = page; this.save(); }\n"
        "  setReportPage(page) { this.state.reportPage = page; this.save(); }\n"
        "  save() {}\n",
    )
    assert (
        policy.validate_guarded_shared_file(
            "frontend/js/store.js", before=store_before, after=target_additions
        )
        is None
    )
    unrelated = target_additions.replace(
        "  pageSize: 4\n", "  pageSize: 4,\n  adminMode: true\n"
    )
    assert "target-named" in str(
        policy.validate_guarded_shared_file(
            "frontend/js/store.js", before=store_before, after=unrelated
        )
    )
    unrelated_route = target_additions.replace(
        "  pageSize: 4\n", "  pageSize: 4,\n  staffPage: 1\n"
    )
    assert "target-named" in str(
        policy.validate_guarded_shared_file(
            "frontend/js/store.js", before=store_before, after=unrelated_route
        )
    )
    destructive = target_additions.replace("logs: []", "logs: ['rewritten']")
    destructive_error = str(
        policy.validate_guarded_shared_file(
            "frontend/js/store.js", before=store_before, after=destructive
        )
    )
    assert "target-named" in destructive_error or "existing identifiers" in destructive_error


def test_target_scoped_css_requires_every_selector_branch_to_stay_under_anchor():
    before = ".shared-card { color: var(--text); }\n"
    assert (
        _validate_target_scoped_css(
            before,
            before
            + "\n#report-pagination { display: flex; }\n"
            + "#report-pagination .page-btn:disabled { opacity: .5; }\n",
            ["#report-pagination"],
        )
        is None
    )
    generic = _validate_target_scoped_css(
        before,
        before + "\n.page-btn { opacity: .5; }\n",
        ["#report-pagination"],
    )
    assert generic is not None and "target-scoped" in generic
    mixed = _validate_target_scoped_css(
        before,
        before + "\n#report-pagination .page-btn, .global-btn { opacity: .5; }\n",
        ["#report-pagination"],
    )
    assert mixed is not None and "target-scoped" in mixed
    indirect = _validate_target_scoped_css(
        before,
        before + "\n:is(#report-pagination, .global-panel) .page-btn { opacity: .5; }\n",
        ["#report-pagination"],
    )
    assert indirect is not None and "target-scoped" in indirect
    sibling = _validate_target_scoped_css(
        before,
        before + "\n#report-pagination + .global-banner { display: none; }\n",
        ["#report-pagination"],
    )
    assert sibling is not None and "target-scoped" in sibling
    prefix_collision = _validate_target_scoped_css(
        before,
        before + "\n#report-pagination-extra { display: none; }\n",
        ["#report-pagination"],
    )
    assert prefix_collision is not None and "target-scoped" in prefix_collision
    nested_at_rule = _validate_target_scoped_css(
        before,
        before
        + "\n@media (min-width: 40rem) { #report-pagination { display: flex; } }\n",
        ["#report-pagination"],
    )
    assert nested_at_rule is not None and "target-scoped" in nested_at_rule
    nested_selector_escape = _validate_target_scoped_css(
        before,
        before
        + "\n#report-pagination { & + .global-banner { display: none; } }\n",
        ["#report-pagination"],
    )
    assert nested_selector_escape is not None and "target-scoped" in nested_selector_escape


def test_target_scoped_css_accepts_equivalent_quoted_data_anchor():
    before = ".shared-card { color: var(--text); }\n"
    after = before + "\n[data-testid='report-panel'] .page-btn { opacity: .5; }\n"

    assert (
        _validate_target_scoped_css(
            before,
            after,
            ['[data-testid="report-panel"]'],
        )
        is None
    )


def test_target_scoped_css_rejects_mixed_global_and_scoped_changes():
    before = ".shared-card { color: var(--text); }\n"
    after = (
        ".shared-card { color: red; }\n"
        "#report-pagination { display: flex; }\n"
    )

    error = _validate_target_scoped_css(before, after, ["#report-pagination"])

    assert error is not None and "target-scoped" in error


def test_multi_html_shared_css_opens_only_target_anchored_rules(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    pages = {
        "catalog.html": '<main id="catalog-page"><div id="catalog-pagination"></div></main>',
        "report.html": '<main id="report-page"><div id="report-summary"></div></main>',
        "settings.html": '<main id="settings-page"><button class="page-btn">Save</button></main>',
    }
    for name, body in pages.items():
        (frontend / name).write_text(
            '<link rel="stylesheet" href="styles.css">\n' + body + "\n",
            encoding="utf-8",
        )
    css_before = ":root { --space-2: .5rem; }\n.page-btn { padding: var(--space-2); }\n"
    (frontend / "styles.css").write_text(css_before, encoding="utf-8")
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "edit_task_contract.json").write_text(
        json.dumps(
            {
                "requested_target_routes": [
                    "/catalog.html",
                    "/report.html",
                ]
            }
        ),
        encoding="utf-8",
    )
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "CATALOG-LAYOUT",
                                "route": "/catalog.html",
                                "category": "visual",
                                "actions": [
                                    {
                                        "action": "assert_computed_style",
                                        "selector": "#catalog-pagination",
                                        "property": "display",
                                        "value": "flex",
                                    }
                                ],
                            },
                            {
                                "id": "REPORT-LAYOUT",
                                "route": "/report.html",
                                "category": "visual",
                                "actions": [
                                    {
                                        "action": "assert_computed_style",
                                        "selector": "#report-summary",
                                        "property": "display",
                                        "value": "grid",
                                    }
                                ],
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=40,
        max_touched_files=3,
    )

    assert plan["route_scope"]["discovered_routes"] == [
        "/catalog.html",
        "/report.html",
        "/settings.html",
    ]
    assert plan["route_scope"]["target_routes"] == [
        "/catalog.html",
        "/report.html",
    ]
    assert plan["route_scope"]["protected_routes"] == ["/settings.html"]
    assert "frontend/styles.css" in plan["route_scope"]["cross_route_shared_paths"]
    css_contract = next(
        item
        for item in plan["source_change_cone"]["guarded_shared_regions"]
        if item.get("mutation_mode") == "target_scoped_css"
    )
    assert css_contract["path"] == "frontend/styles.css"
    assert css_contract["allowed_anchors"] == [
        "#catalog-pagination",
        "#report-summary",
    ]
    assert plan["source_change_cone"]["initial_paths"] == [
        "frontend/styles.css"
    ]

    policy = MinimalPathPolicy.from_plan(tmp_path, plan)
    policy.observe_result(
        "read_file",
        {"path": "frontend/styles.css"},
        ok=True,
        output=css_before,
    )
    safe_after = (
        css_before
        + "#catalog-pagination { display: flex; gap: var(--space-2); }\n"
        + "#report-summary { display: grid; gap: var(--space-2); }\n"
    )
    assert (
        policy.check(
            "apply_patch",
            {
                "path": "frontend/styles.css",
                "old_text": css_before,
                "new_text": safe_after,
            },
        )
        is None
    )
    generic_denial = policy.check(
        "apply_patch",
        {
            "path": "frontend/styles.css",
            "old_text": css_before,
            "new_text": css_before + ".page-btn { opacity: .5; }\n",
        },
    )
    assert generic_denial is not None and "target-scoped" in generic_denial
    protected_denial = policy.check(
        "apply_patch",
        {
            "path": "frontend/settings.html",
            "old_text": "Save",
            "new_text": "Changed",
        },
    )
    assert protected_denial is not None and "outside the target page" in protected_denial


def test_shared_css_stays_closed_when_anchor_exists_on_protected_html(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    for name in ("catalog.html", "settings.html"):
        (frontend / name).write_text(
            '<link rel="stylesheet" href="styles.css">\n'
            '<div id="shared-pagination"></div>\n',
            encoding="utf-8",
        )
    (frontend / "styles.css").write_text(".page-btn { padding: .5rem; }\n")
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "edit_task_contract.json").write_text(
        json.dumps({"requested_target_routes": ["/catalog.html"]})
    )
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "CATALOG-STYLE",
                                "route": "/catalog.html",
                                "category": "visual",
                                "actions": [
                                    {
                                        "action": "assert_computed_style",
                                        "selector": "#shared-pagination",
                                        "property": "display",
                                        "value": "flex",
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=20,
        max_touched_files=3,
    )

    assert not any(
        item.get("mutation_mode") == "target_scoped_css"
        for item in plan["source_change_cone"]["guarded_shared_regions"]
    )
    assert "frontend/styles.css" in plan["source_change_cone"]["protected_paths"]


def test_react_router_edit_protects_component_shared_with_other_route(tmp_path: Path):
    frontend = tmp_path / "frontend"
    (frontend / "src" / "pages").mkdir(parents=True)
    (frontend / "src" / "components").mkdir()
    (frontend / "index.html").write_text(
        '<div id="root"></div><script type="module" src="/src/main.jsx"></script>\n'
    )
    (frontend / "src" / "main.jsx").write_text("import './App.jsx';\n")
    (frontend / "src" / "App.jsx").write_text(
        "import Catalog from './pages/Catalog.jsx';\n"
        "import Settings from './pages/Settings.jsx';\n"
        '<Route path="/catalog" element={<Catalog />} />;\n'
        '<Route path="/settings" element={<Settings />} />;\n'
    )
    (frontend / "src" / "pages" / "Catalog.jsx").write_text(
        "import Shell from '../components/Shell.jsx';\n"
        "import './catalog.css';\n"
        'export default () => <Shell><input id="catalog-search" /></Shell>;\n'
    )
    (frontend / "src" / "pages" / "catalog.css").write_text(
        "#catalog-search { width: 20rem; }\n"
    )
    (frontend / "src" / "pages" / "Settings.jsx").write_text(
        "import Shell from '../components/Shell.jsx';\n"
        'export default () => <Shell><input id="profile-name" /></Shell>;\n'
    )
    (frontend / "src" / "components" / "Shell.jsx").write_text(
        "export default ({children}) => <main>{children}</main>;\n"
    )
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-CATALOG",
                                "route": "/catalog",
                                "category": "interaction",
                                "actions": [
                                    {
                                        "action": "fill",
                                        "selector": "#catalog-search",
                                        "value": "camera",
                                    },
                                    {"action": "evaluate", "expression": "true"},
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=30,
        max_touched_files=3,
    )

    assert plan["route_scope"]["discovered_routes"] == ["/catalog", "/settings"]
    assert plan["route_scope"]["route_local_paths"] == [
        "frontend/src/pages/Catalog.jsx",
        "frontend/src/pages/catalog.css",
    ]
    assert plan["route_scope"]["cross_route_shared_paths"] == [
        "frontend/src/components/Shell.jsx"
    ]
    assert "frontend/src/pages/Settings.jsx" in plan["route_scope"]["off_target_paths"]
    assert plan["source_change_cone"]["initial_paths"] == [
        "frontend/src/pages/Catalog.jsx"
    ]


def test_next_layout_and_global_css_are_cross_route_shared(tmp_path: Path):
    frontend = tmp_path / "frontend"
    (frontend / "src" / "app" / "catalog").mkdir(parents=True)
    (frontend / "src" / "app" / "settings").mkdir(parents=True)
    (frontend / "src" / "app" / "page.tsx").write_text(
        "export default () => <main id='home'>Home</main>;\n"
    )
    (frontend / "src" / "app" / "catalog" / "page.tsx").write_text(
        "import './catalog.css';\nexport default () => <input id='catalog-search' />;\n"
    )
    (frontend / "src" / "app" / "catalog" / "catalog.css").write_text(
        "#catalog-search { width: 20rem; }\n"
    )
    (frontend / "src" / "app" / "settings" / "page.tsx").write_text(
        "export default () => <main id='settings'>Settings</main>;\n"
    )
    (frontend / "src" / "app" / "layout.tsx").write_text(
        "import './globals.css';\nexport default ({children}) => <body>{children}</body>;\n"
    )
    (frontend / "src" / "app" / "globals.css").write_text("body { margin: 0; }\n")
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "ui_verification_plan.json").write_text(
        json.dumps(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-CATALOG",
                                "route": "/catalog",
                                "category": "interaction",
                                "actions": [
                                    {"action": "fill", "selector": "#catalog-search", "value": "x"},
                                    {"action": "evaluate", "expression": "true"},
                                ],
                            }
                        ],
                    }
                ]
            }
        )
    )

    plan = ensure_minimal_path_plan(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        sprint_num=1,
        mode="generate",
        max_patch_lines=30,
        max_touched_files=3,
    )

    route_scope = plan["route_scope"]
    assert route_scope["discovered_routes"] == ["/", "/catalog", "/settings"]
    assert route_scope["route_local_paths"] == [
        "frontend/src/app/catalog/catalog.css",
        "frontend/src/app/catalog/page.tsx",
    ]
    assert route_scope["cross_route_shared_paths"] == [
        "frontend/src/app/globals.css",
        "frontend/src/app/layout.tsx",
    ]
    assert "frontend/src/app/settings/page.tsx" in route_scope["off_target_paths"]


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
            "initial_paths": ["frontend/src/App.jsx"],
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
    blind_patch = policy.check(
        "apply_patch",
        {
            "path": "frontend/src/App.jsx",
            "old_text": "'old'",
            "new_text": "'new'",
        },
    )
    policy.observe_result(
        "read_file", {"path": "frontend/src/App.jsx"}, ok=True, output="source"
    )
    state_after_read = json.loads(
        (harness / "minimal_path_state_round_1.json").read_text()
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
    assert blind_patch is not None and "inspect" in blind_patch.lower()
    assert state_after_read["phase"] == "patch_initial"
    assert state_after_read["unlocked_paths"] == ["frontend/src/App.jsx"]
    assert local_patch is None
    assert collateral is not None and "outside the harness change cone" in collateral
    ledger = [
        json.loads(line)
        for line in (harness / "minimal_path_ledger_round_1.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [item["decision"] for item in ledger] == [
        "deny",
        "deny",
        "observe",
        "allow",
        "deny",
    ]


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
            "initial_paths": ["frontend/src/App.jsx"],
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

    policy.observe_result(
        "Read", {"file_path": str(frontend / "App.jsx")}, ok=True, output="source"
    )
    source_patch = {
        "file_path": str(frontend / "App.jsx"),
        "old_string": "import './widget.css';",
        "new_string": "import './widget.css';\n// scoped",
    }
    assert policy.check("Edit", source_patch) is None
    policy.observe_result("Edit", source_patch, ok=True, output="patched")
    policy.observe_result("Read", {"file_path": str(dependency)}, ok=True, output="css")
    premature = policy.check(
        "Edit",
        {
            "file_path": str(dependency),
            "old_string": "black",
            "new_string": "navy",
        },
    )
    policy.observe_result(
        "Bash", {"command": "npm run build"}, ok=False, output="missing style"
    )
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

    assert premature is not None and "validation attempt" in premature
    assert expanded is None
    assert denied is not None
    ledger = [
        json.loads(line)
        for line in (tmp_path / ".harness" / "minimal_path_ledger_round_2.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    dependency_allow = next(
        item
        for item in ledger
        if item.get("decision") == "allow" and item.get("scope_tier") == "dependency"
    )
    assert dependency_allow["expansion_reason"] == "recorded_dependency_edge"


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
            "initial_paths": ["frontend/index.html"],
            "dependency_paths": [],
            "protected_paths": [],
        },
        "budgets": {"max_patch_lines": 5, "max_touched_files": 1},
        "dom_change_cone": {"allow_new_roots": True},
    }
    policy = MinimalPathPolicy.from_plan(tmp_path, plan)
    policy.observe_result(
        "read_file", {"path": "frontend/index.html"}, ok=True, output="source"
    )

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
    interpreter = policy.check(
        "run_command",
        {"command": 'python3 -c \'open("frontend/extra.js", "w").write("x")\''},
    )
    arbitrary_package_script = policy.check(
        "run_command", {"command": "npm --prefix frontend run scaffold"}
    )
    git_stash = policy.check("run_command", {"command": "cd frontend && git stash"})
    readonly_package = policy.check(
        "run_command", {"command": "npm --prefix frontend list"}
    )
    explicit_validation = policy.check(
        "run_command", {"command": "node --check frontend/script.js"}
    )

    assert large is not None and "patch-line budget" in large
    assert command is not None and "mutation tools" in command
    assert interpreter is not None and "interpreter" in interpreter
    assert (
        arbitrary_package_script is not None
        and "package managers" in arbitrary_package_script
    )
    assert git_stash is not None and "mutation tools" in git_stash
    assert readonly_package is None
    assert explicit_validation is None


def test_policy_requires_successful_post_mutation_validation_before_commit(
    tmp_path: Path,
):
    source = tmp_path / "frontend" / "app.js"
    source.parent.mkdir()
    source.write_text("const value = 'old';\n")
    (tmp_path / ".harness").mkdir()
    plan = {
        "schema_version": "minimal-path-plan-v1",
        "owner": "harness",
        "round": 4,
        "source_change_cone": {
            "local_paths": ["frontend/app.js"],
            "initial_paths": ["frontend/app.js"],
            "dependency_paths": [],
            "protected_paths": [],
        },
        "budgets": {"max_patch_lines": 10, "max_touched_files": 1},
        "dom_change_cone": {"allow_new_roots": False},
    }
    policy = MinimalPathPolicy.from_plan(tmp_path, plan)
    policy.observe_result(
        "read_file", {"path": "frontend/app.js"}, ok=True, output="source"
    )
    patch = {
        "path": "frontend/app.js",
        "old_text": "'old'",
        "new_text": "'new'",
    }
    assert policy.check("apply_patch", patch) is None
    source.write_text("const value = 'new';\n")
    policy.observe_result("apply_patch", patch, ok=True, output="patched")

    before_validation = policy.check(
        "run_command", {"command": "git -C frontend commit -m fix:scoped"}
    )
    policy.observe_result(
        "run_command", {"command": "npm run build"}, ok=False, output="failed"
    )
    after_failure = policy.check("run_command", {"command": "git commit -m fix:scoped"})
    policy.observe_result(
        "run_command", {"command": "npm run build"}, ok=True, output="built"
    )
    after_success = policy.check("run_command", {"command": "git commit -m fix:scoped"})
    policy.observe_result(
        "run_command", {"command": "npm test"}, ok=False, output="test failed"
    )
    after_later_failure = policy.check(
        "run_command", {"command": "git commit -m fix:scoped"}
    )

    assert (
        before_validation is not None and "successful validation" in before_validation
    )
    assert after_failure is not None and "successful validation" in after_failure
    assert after_success is None
    assert (
        after_later_failure is not None
        and "successful validation" in after_later_failure
    )
    state = json.loads(
        (tmp_path / ".harness" / "minimal_path_state_round_4.json").read_text()
    )
    assert state["mutation_revision"] == 1
    assert state["validation_success_revision"] == 1
    assert state["validation_last_ok"] is False
    assert state["phase"] == "repair_or_expand"


def test_policy_denies_unplanned_source_and_harness_state_mutation(tmp_path: Path):
    source = tmp_path / "frontend" / "app.js"
    source.parent.mkdir()
    source.write_text("const value = 1;\n")
    (tmp_path / ".harness").mkdir()
    plan = {
        "schema_version": "minimal-path-plan-v1",
        "owner": "harness",
        "round": 5,
        "source_change_cone": {
            "local_paths": ["frontend/app.js"],
            "initial_paths": ["frontend/app.js"],
            "dependency_paths": [],
            "protected_paths": [],
        },
        "budgets": {"max_patch_lines": 10, "max_touched_files": 1},
        "dom_change_cone": {"allow_new_roots": False},
    }
    policy = MinimalPathPolicy.from_plan(tmp_path, plan)

    unplanned = policy.check(
        "write_file",
        {"path": "frontend/helper.js", "content": "export const helper = 1;\n"},
    )
    state_edit = policy.check(
        "write_file",
        {
            "path": ".harness/minimal_path_state_round_5.json",
            "content": "{}",
        },
    )
    ledger_edit = policy.check(
        "write_file",
        {
            "path": ".harness/minimal_path_ledger_round_5.jsonl",
            "content": "",
        },
    )

    assert unplanned is not None and "unplanned new source" in unplanned
    assert state_edit is not None and "harness-owned" in state_edit
    assert ledger_edit is not None and "harness-owned" in ledger_edit
