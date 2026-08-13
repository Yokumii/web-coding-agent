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

    assert baseline["version"] == 3
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
        {"root": "/settings.html::settings", "kind": "semantic_changed"}
    ]


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
