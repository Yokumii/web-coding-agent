import json

import pytest
from aiohttp import web

from src.config import HarnessConfig
from src.orchestration.edit_dom_guard import (
    capture_baseline,
    compare_contract,
    snapshot_semantic_dom,
)
from src.orchestration.file_comm import FileComm
from src.agents.generator import _validate_edit_scope


def _snapshot(*items):
    return {"roots": [{"key": key, "fingerprint": fingerprint} for key, fingerprint in items]}


def _fragment_snapshot(*items):
    return {
        "version": 4,
        "stable": True,
        "fragments": [
            {"key": key, "fingerprint": fingerprint, "route": route, **extra}
            for key, fingerprint, route, extra in items
        ],
    }


def test_guard_allows_only_declared_semantic_surface():
    result = compare_contract(
        _snapshot(("header", "a"), ("main", "b"), ("footer", "c")),
        _snapshot(("header", "a"), ("main", "changed"), ("footer", "c")),
        {"allowed_root_keys": ["main"], "allow_new_roots": False},
    )
    assert result["passed"] is True


def test_guard_rejects_unrelated_change_even_when_task_root_is_allowed():
    result = compare_contract(
        _snapshot(("header", "a"), ("main", "b"), ("footer", "c")),
        _snapshot(("header", "changed"), ("main", "changed"), ("footer", "c")),
        {"allowed_root_keys": ["main"], "allow_new_roots": False},
    )
    assert result["passed"] is False
    assert result["violations"] == [{"root": "header", "kind": "semantic_changed"}]


def test_guard_requires_explicit_permission_for_new_surface():
    result = compare_contract(
        _snapshot(("main", "a")), _snapshot(("main", "a"), ("dialog", "b")),
        {"allowed_root_keys": [], "allow_new_roots": False},
    )
    assert result["passed"] is False
    assert result["violations"] == [{"root": "dialog", "kind": "unexpected_added"}]


def test_guard_allows_many_changes_inside_one_declared_surface():
    """A main-surface edit may change its cards without weakening header/footer guards."""
    result = compare_contract(
        _snapshot(("header", "same"), ("main", "twelve cards"), ("footer", "same")),
        _snapshot(("header", "same"), ("main", "five filtered cards"), ("footer", "same")),
        {"allowed_root_keys": ["main"], "allow_new_roots": False},
    )
    assert result["passed"] is True


def test_guard_protects_keyboard_reachability_as_part_of_surface_fingerprint():
    result = compare_contract(
        _snapshot(("header", "focusable-nav"), ("main", "editable")),
        _snapshot(("header", "lost-keyboard-focus"), ("main", "editable")),
        {"allowed_root_keys": ["main"], "allow_new_roots": False},
    )
    assert result["passed"] is False
    assert result["violations"] == [{"root": "header", "kind": "semantic_changed"}]


def test_fragment_guard_allows_target_but_protects_sibling_inside_same_main():
    baseline = _fragment_snapshot(
        ("main", "before", "/", {}),
        ("target-card", "before", "/", {"parent_key": "main"}),
        ("protected-card", "stable", "/", {"parent_key": "main"}),
    )
    current = _fragment_snapshot(
        ("main", "changed", "/", {}),
        ("target-card", "changed", "/", {"parent_key": "main"}),
        ("protected-card", "collateral", "/", {"parent_key": "main"}),
    )

    result = compare_contract(
        baseline,
        current,
        {
            "allowed_fragment_keys": ["target-card"],
            "expected_new_fragments": [],
            "target_routes": ["/"],
            "protected_routes": [],
        },
    )

    assert result["passed"] is False
    assert result["violations"] == [
        {"fragment": "protected-card", "kind": "semantic_changed"}
    ]


def test_fragment_guard_allows_dynamic_descendants_of_authorized_container():
    baseline = _fragment_snapshot(
        ("main", "before", "/catalog", {}),
        ("catalog-grid", "before", "/catalog", {"parent_key": "main"}),
        ("button:Klara", "before", "/catalog", {"parent_key": "catalog-grid"}),
        ("protected-summary", "stable", "/catalog", {"parent_key": "main"}),
    )
    current = _fragment_snapshot(
        ("main", "after", "/catalog", {}),
        ("catalog-grid", "after", "/catalog", {"parent_key": "main"}),
        ("button:Dune", "after", "/catalog", {"parent_key": "catalog-grid"}),
        ("protected-summary", "stable", "/catalog", {"parent_key": "main"}),
    )

    result = compare_contract(
        baseline,
        current,
        {
            "allowed_fragment_keys": ["catalog-grid"],
            "expected_new_fragments": [],
            "target_routes": ["/catalog"],
            "protected_routes": [],
        },
    )

    assert result["passed"] is True


def test_fragment_guard_allows_only_one_explicit_new_selector():
    baseline = _fragment_snapshot(("main", "same", "/catalog", {}))
    current = _fragment_snapshot(
        ("main", "same", "/catalog", {}),
        ("new-dialog", "new", "/catalog", {"anchors": ["#new-dialog"]}),
    )

    result = compare_contract(
        baseline,
        current,
        {
            "allowed_fragment_keys": [],
            "expected_new_fragments": [
                {"route": "/catalog", "selector": "#new-dialog", "max_count": 1}
            ],
            "target_routes": ["/catalog"],
            "protected_routes": [],
        },
    )

    assert result["passed"] is True


def test_fragment_guard_allows_repeated_expected_subtrees_and_changed_ancestors():
    baseline = _fragment_snapshot(
        ("main", "before", "/catalog", {}),
        ("section", "before", "/catalog", {"parent_key": "main"}),
        ("nav", "stable", "/catalog", {}),
    )
    additions = []
    for index in range(5):
        additions.extend(
            [
                (
                    f"catalog-{index}",
                    f"book-{index}",
                    "/catalog",
                    {"parent_key": "section", "anchors": [".catalog-item", ".save-btn"]},
                ),
                (
                    f"save-{index}",
                    f"button-{index}",
                    "/catalog",
                    {"parent_key": f"catalog-{index}", "anchors": [".save-btn"]},
                ),
            ]
        )
    current = _fragment_snapshot(
        ("main", "after", "/catalog", {}),
        ("section", "after", "/catalog", {"parent_key": "main"}),
        ("nav", "stable", "/catalog", {}),
        *additions,
    )

    result = compare_contract(
        baseline,
        current,
        {
            "allowed_fragment_keys": [],
            "expected_new_fragments": [
                {"route": "/catalog", "selector": ".catalog-item", "max_count": 5}
            ],
            "target_routes": ["/catalog"],
            "protected_routes": [],
        },
    )

    assert result["passed"] is True
    assert result["expected_new_hits"] == [5]


def test_fragment_guard_uses_real_selector_count_for_one_semantic_subtree():
    baseline = _fragment_snapshot(
        ("main", "before", "/catalog", {}),
        ("section", "before", "/catalog", {"parent_key": "main"}),
    )
    current = _fragment_snapshot(
        ("main", "after", "/catalog", {}),
        ("section", "after", "/catalog", {"parent_key": "main"}),
        (
            "catalog-list",
            "five-books",
            "/catalog",
            {"parent_key": "section", "anchors": [".catalog-item", ".save-btn"]},
        ),
        (
            "save-button",
            "first-save",
            "/catalog",
            {"parent_key": "catalog-list", "anchors": [".save-btn"]},
        ),
    )
    current["selector_counts"] = [
        {"route": "/catalog", "selector": ".catalog-item", "count": 5}
    ]

    result = compare_contract(
        baseline,
        current,
        {
            "allowed_fragment_keys": [],
            "expected_new_fragments": [
                {"route": "/catalog", "selector": ".catalog-item", "max_count": 5}
            ],
            "target_routes": ["/catalog"],
            "protected_routes": [],
        },
    )

    assert result["passed"] is True
    assert result["expected_new_hits"] == [5]


def test_fragment_guard_allows_only_the_declared_new_route():
    baseline = _fragment_snapshot(("/::main", "stable", "/", {}))
    current = _fragment_snapshot(
        ("/::main", "stable", "/", {}),
        ("/library.html::nav", "new-nav", "/library.html", {}),
        ("/library.html::main", "new-main", "/library.html", {}),
        (
            "/library.html::search",
            "new-search",
            "/library.html",
            {"parent_key": "/library.html::main", "anchors": ["#search"]},
        ),
    )

    result = compare_contract(
        baseline,
        current,
        {
            "allowed_fragment_keys": [],
            "expected_new_fragments": [],
            "expected_new_routes": ["/library.html"],
            "target_routes": ["/library.html"],
            "protected_routes": ["/"],
        },
    )

    assert result["passed"] is True


def test_fragment_guard_aligns_legacy_single_route_baseline_with_routed_current():
    baseline = _fragment_snapshot(
        ("header", "stable-header", "/", {}),
        ("main", "stable-main", "/", {}),
    )
    current = _fragment_snapshot(
        ("/::header", "stable-header", "/", {}),
        ("/::main", "stable-main", "/", {}),
        ("/library.html::nav", "new-nav", "/library.html", {}),
        ("/library.html::main", "new-main", "/library.html", {}),
    )
    current["routes"] = ["/", "/library.html"]

    result = compare_contract(
        baseline,
        current,
        {
            "allowed_fragment_keys": [],
            "expected_new_fragments": [],
            "expected_new_routes": ["/library.html"],
            "target_routes": ["/library.html"],
            "protected_routes": ["/"],
        },
    )

    assert result["passed"] is True


def test_generator_scope_validation_accepts_bounded_repeated_new_fragments(tmp_path):
    (tmp_path / "seed_manifest.json").write_text("{}")
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "edit_dom_baseline.json").write_text(
        json.dumps(
            {
                "version": 4,
                "stable": True,
                "roots": [],
                "fragments": [],
                "routes": ["/catalog"],
            }
        )
    )
    (harness / "edit_scope_round_1.json").write_text(
        json.dumps(
            {
                "allowed_root_keys": [],
                "allowed_fragment_keys": [],
                "expected_new_fragments": [
                    {"route": "/catalog", "selector": ".catalog-item", "max_count": 5}
                ],
                "target_routes": ["/catalog"],
                "protected_routes": [],
                "allow_new_roots": True,
            }
        )
    )

    assert _validate_edit_scope(tmp_path, 1) is None


def test_forward_edit_requires_small_machine_readable_scope(tmp_path):
    (tmp_path / "seed_manifest.json").write_text("{}")
    harness = tmp_path / ".harness"
    harness.mkdir()
    assert _validate_edit_scope(tmp_path, 1) is not None
    (harness / "edit_dom_baseline.json").write_text('{"roots":[{"key":"main"}]}')
    (harness / "edit_scope_round_1.json").write_text(
        '{"allowed_root_keys":["main"],"allow_new_roots":false}'
    )
    assert _validate_edit_scope(tmp_path, 1) is None
    (harness / "edit_scope_round_1.json").write_text(
        '{"allowed_root_keys":["frontend"],"allow_new_roots":false}'
    )
    assert "unknown baseline roots" in _validate_edit_scope(tmp_path, 1)


def test_non_forward_repair_scope_uses_failed_source_baseline(tmp_path):
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "repair_dom_source_round_2.json").write_text(
        '{"roots":[{"key":"dialog"},{"key":"main"}]}'
    )
    (harness / "edit_scope_round_2.json").write_text(
        '{"allowed_root_keys":["dialog"],"allow_new_roots":false}'
    )

    assert _validate_edit_scope(
        tmp_path,
        2,
        required=True,
        baseline_filename="repair_dom_source_round_2.json",
    ) is None


def test_harness_owned_scope_can_reference_current_sprint_baseline(tmp_path):
    (tmp_path / "seed_manifest.json").write_text("{}")
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "edit_dom_baseline.json").write_text(
        '{"roots":[{"key":"seed-main"}]}'
    )
    (harness / "edit_dom_source_sprint_2.json").write_text(
        '{"roots":[{"key":"accepted-search"}]}'
    )
    (harness / "edit_scope_round_2.json").write_text(
        '{"owner":"harness","baseline":".harness/edit_dom_source_sprint_2.json",'
        '"allowed_root_keys":["accepted-search"],"allow_new_roots":false}'
    )

    assert _validate_edit_scope(tmp_path, 2) is None


@pytest.mark.anyio
async def test_capture_baseline_uses_non_overlapping_semantic_surfaces(tmp_path):
    async def page(_request):
        return web.Response(text="""
        <body><div id='root'><header><a href='/docs'>Docs</a></header>
        <main><article aria-label='first'>One</article><article aria-label='second'>Two</article></main>
        <footer><button>Help</button></footer></div></body>
        """, content_type="text/html")

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        workdir = tmp_path / "edit"
        workdir.mkdir()
        snapshot = await capture_baseline(
            workdir=workdir,
            file_comm=FileComm(workdir / ".harness"),
            config=HarnessConfig(playwright_headless=True),
            app_url=f"http://127.0.0.1:{port}",
        )
    finally:
        await runner.cleanup()

    # The two articles are covered by main rather than becoming separately
    # protected roots, so a legitimate list/filter edit can be scoped to main.
    assert [root["key"] for root in snapshot["roots"]] == ["header:unnamed", "main:unnamed", "footer:unnamed"]
    assert '#root' not in snapshot["roots"][0]["anchors"]
    assert 'a[href="/docs"]' in snapshot["roots"][0]["anchors"]
    assert '[aria-label="first"]' in snapshot["roots"][1]["anchors"]


@pytest.mark.anyio
async def test_multi_route_semantic_guard_detects_protected_page_change():
    state = {"settings": "Settings stable"}

    async def home(_request):
        return web.Response(text="<main id='home'>Home</main>", content_type="text/html")

    async def catalog(_request):
        return web.Response(
            text="<main id='catalog'><input id='catalog-search'></main>",
            content_type="text/html",
        )

    async def settings(_request):
        return web.Response(
            text=f"<main id='settings'>{state['settings']}</main>",
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", home)
    app.router.add_get("/catalog.html", catalog)
    app.router.add_get("/settings.html", settings)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    routes = ["/", "/catalog.html", "/settings.html"]
    try:
        baseline = await snapshot_semantic_dom(
            f"http://127.0.0.1:{port}", headless=True, routes=routes
        )
        state["settings"] = "Settings changed by collateral edit"
        current = await snapshot_semantic_dom(
            f"http://127.0.0.1:{port}", headless=True, routes=routes
        )
    finally:
        await runner.cleanup()

    assert baseline["version"] == 4
    assert baseline["stable"] is True
    assert baseline["routes"] == routes
    assert {root["key"] for root in baseline["roots"]} == {
        "/::home",
        "/catalog.html::catalog",
        "/settings.html::settings",
    }
    result = compare_contract(
        baseline,
        current,
        {
            "allowed_root_keys": ["/catalog.html::catalog"],
            "allow_new_roots": False,
            "target_routes": ["/catalog.html"],
            "protected_routes": ["/", "/settings.html"],
        },
    )
    assert result["passed"] is False
    assert result["violations"] == [
        {"fragment": "/settings.html::settings", "kind": "semantic_changed"}
    ]


@pytest.mark.anyio
async def test_semantic_snapshot_records_expected_selector_counts():
    async def catalog(_request):
        return web.Response(
            text="<main><ul>" + "".join(
                f"<li class='catalog-item'>Book {index}</li>" for index in range(5)
            ) + "</ul></main>",
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/catalog", catalog)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        snapshot = await snapshot_semantic_dom(
            f"http://127.0.0.1:{port}",
            headless=True,
            routes=["/catalog"],
            selector_contracts=[
                {"route": "/catalog", "selector": ".catalog-item", "max_count": 5}
            ],
        )
    finally:
        await runner.cleanup()

    assert snapshot["selector_counts"] == [
        {"route": "/catalog", "selector": ".catalog-item", "count": 5}
    ]


def test_fragment_guard_fails_closed_on_unstable_baseline():
    baseline = _fragment_snapshot(("main", "a", "/", {}))
    baseline["stable"] = False
    baseline["unstable_fragment_keys"] = ["main"]

    result = compare_contract(
        baseline,
        _fragment_snapshot(("main", "a", "/", {})),
        {"allowed_fragment_keys": [], "expected_new_fragments": []},
    )

    assert result["passed"] is False
    assert "unstable" in result["reason"]


def test_multi_route_scope_rejects_allowed_root_from_protected_page():
    baseline = {
        "version": 3,
        "routes": ["/catalog", "/settings"],
        "roots": [
            {"key": "/catalog::main", "route": "/catalog", "fingerprint": "a"},
            {"key": "/settings::main", "route": "/settings", "fingerprint": "b"},
        ],
    }
    result = compare_contract(
        baseline,
        baseline,
        {
            "allowed_root_keys": ["/settings::main"],
            "allow_new_roots": False,
            "target_routes": ["/catalog"],
            "protected_routes": ["/settings"],
        },
    )
    assert result["passed"] is False
    assert "outside target routes" in result["reason"]


def test_generator_accepts_two_roots_for_each_target_route(tmp_path):
    (tmp_path / "seed_manifest.json").write_text("{}")
    harness = tmp_path / ".harness"
    harness.mkdir()
    roots = [
        {"key": f"{route}::{name}", "route": route, "fingerprint": name}
        for route in ("/catalog", "/search")
        for name in ("main", "dialog")
    ]
    (harness / "edit_dom_baseline.json").write_text(
        json.dumps(
            {
                "version": 3,
                "routes": ["/catalog", "/search", "/settings"],
                "roots": roots,
            }
        )
    )
    (harness / "edit_scope_round_1.json").write_text(
        json.dumps(
            {
                "schema_version": "edit-scope-v3",
                "allowed_root_keys": [root["key"] for root in roots],
                "allow_new_roots": False,
                "target_routes": ["/catalog", "/search"],
                "protected_routes": ["/settings"],
            }
        )
    )

    assert _validate_edit_scope(tmp_path, 1) is None
