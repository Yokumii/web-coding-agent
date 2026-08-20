from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from src.orchestration.browser_evidence import (
    _action_settle_ms,
    _is_invalid_test_contract_error,
    _same_origin_route_url,
    collect_browser_evidence,
)


def test_evaluate_syntax_error_is_invalid_test_contract():
    error = RuntimeError("Page.evaluate: SyntaxError: Illegal return statement")

    assert _is_invalid_test_contract_error("evaluate", error) is True
    assert _is_invalid_test_contract_error("click", error) is False


def test_action_settle_ms_is_explicit_and_bounded():
    assert _action_settle_ms({"action": "fill", "settle_ms": 200}, "fill") == 200
    assert _action_settle_ms({"action": "fill"}, "fill") == 0
    assert _action_settle_ms({"action": "evaluate", "settle_ms": 250}, "evaluate") == 0


def test_same_origin_route_url_rejects_hash_router_path_until_it_can_be_owned():
    with pytest.raises(ValueError, match="unsafe browser route"):
        _same_origin_route_url("http://127.0.0.1:3000/preview", "/#/catalog")


@pytest.mark.anyio
async def test_browser_evidence_navigates_multi_page_checks_and_preserves_same_route_state(
    tmp_path: Path,
):
    async def root(_request):
        return web.Response(text="<main id='home'>Home</main>", content_type="text/html")

    async def catalog(_request):
        return web.Response(
            text="""
            <main><input id='query'><output id='value'></output>
            <script>
              document.querySelector('#query').addEventListener('input', event => {
                document.querySelector('#value').textContent = event.target.value;
              });
            </script></main>
            """,
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/catalog.html", catalog)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    output = tmp_path / "browser.json"
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[
                {
                    "id": "UI-1",
                    "route": "/catalog.html",
                    "actions": [
                        {"action": "fill", "selector": "#query", "value": "atlas"},
                        {"action": "evaluate", "expression": "document.querySelector('#value').textContent === 'atlas'"},
                    ],
                },
                {
                    "id": "UI-2",
                    "route": "/catalog.html",
                    "actions": [
                        {"action": "evaluate", "expression": "document.querySelector('#query').value === 'atlas'"},
                    ],
                },
            ],
            output_path=output,
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert [item["status"] for item in result["checks"]] == ["ok", "ok"]
    assert all(item["route"] == "/catalog.html" for item in result["checks"])
    assert result["checks"][0]["url"].endswith("/catalog.html")


@pytest.mark.anyio
async def test_browser_evidence_executes_0805_advanced_interaction_primitives(
    tmp_path: Path,
):
    async def advanced(_request):
        return web.Response(
            text="""
            <style>
              #source, #target { width: 80px; height: 40px; margin: 8px; border: 1px solid; }
              #ready { display: none; }
              #print-state { display: none; }
              @media print { #print-state { display: block; } }
            </style>
            <button id="tip">Hover</button>
            <button id="menu">Context</button>
            <div id="source" draggable="true">source</div><div id="target">target</div>
            <input id="upload" type="file"><output id="ready">ready</output>
            <div id="print-state">print</div>
            <script>
              const state = document.body.dataset;
              tip.addEventListener('mouseenter', () => state.hovered = 'yes');
              menu.addEventListener('contextmenu', event => {
                event.preventDefault(); state.context = 'yes';
              });
              source.addEventListener('dragstart', event => event.dataTransfer.setData('text/plain', 'source'));
              target.addEventListener('dragover', event => event.preventDefault());
              target.addEventListener('drop', event => {
                event.preventDefault(); state.dropped = event.dataTransfer.getData('text/plain');
              });
              upload.addEventListener('change', async () => {
                state.upload = await upload.files[0].text();
                setTimeout(() => { ready.style.display = 'block'; }, 40);
              });
            </script>
            """,
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", advanced)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{
                "id": "UI-ADVANCED",
                "route": "/",
                "actions": [
                    {"action": "hover", "selector": "#tip"},
                    {"action": "click", "selector": "#menu", "button": "right"},
                    {"action": "drag_and_drop", "source_selector": "#source", "target_selector": "#target"},
                    {
                        "action": "set_input_files",
                        "selector": "#upload",
                        "files": [{"name": "sample.txt", "mime_type": "text/plain", "content": "0805"}],
                    },
                    {"action": "wait_for", "selector": "#ready", "state": "visible", "timeout_ms": 1000},
                    {"action": "emulate_media", "media": "print"},
                    {
                        "action": "evaluate",
                        "expression": "document.body.dataset.hovered === 'yes' && document.body.dataset.context === 'yes' && document.body.dataset.dropped === 'source' && document.body.dataset.upload === '0805' && getComputedStyle(document.querySelector('#print-state')).display === 'block'",
                    },
                ],
            }],
            output_path=tmp_path / "advanced.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert result["checks"][0]["status"] == "ok"
    assert all(step["ok"] for step in result["checks"][0]["steps"])
