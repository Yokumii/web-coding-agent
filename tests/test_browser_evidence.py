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
