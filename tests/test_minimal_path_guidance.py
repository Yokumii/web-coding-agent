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
            "schema_version": "edit-scope-v3",
        "owner": "harness",
        "plan": ".harness/minimal_path_plan_round_1.json",
        "baseline": ".harness/edit_dom_source_sprint_1.json",
            "allowed_root_keys": ["main:catalog"],
            "allow_new_roots": False,
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
