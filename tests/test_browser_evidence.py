from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp import web

from src.orchestration.browser_evidence import (
    _action_settle_ms,
    _is_invalid_test_contract_error,
    _matches,
    _resolve_behavioral_selector,
    _same_origin_route_url,
    collect_browser_evidence,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_navigation_failure_is_not_an_invalid_instruction(tmp_path):
    import socket
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    evidence = await collect_browser_evidence(app_url=f'http://127.0.0.1:{port}',
        checks=[{'id':'unreachable','route':'/','actions':[{'action':'assert_visible','selector':'body'}]},
                {'id':'invalid-route','route':'https://example.org/','actions':[{'action':'assert_visible','selector':'body'}]}],
        output_path=tmp_path/'navigation.json',headless=True)
    assert [item['status'] for item in evidence['checks']] == ['navigation_failed', 'invalid_test_contract']
    assert evidence['checks'][0]['steps'] == []
    assert 'ERR_CONNECTION_REFUSED' in evidence['checks'][0]['navigation_error']


def test_nonempty_match_rejects_missing_and_blank_runtime_values():
    assert _matches("artifact-7", "", "nonempty") is True
    assert _matches(None, "", "nonempty") is False
    assert _matches("  ", "", "nonempty") is False


@pytest.mark.anyio
async def test_lenient_react_warning_is_recorded_but_runtime_error_still_blocks(tmp_path):
    warning = 'Warning: Received `%s` for a non-boolean attribute `%s`.'
    async def page(request):
        extra = '<script>throw new Error("runtime broke")</script>' if request.path=='/broken' else ''
        return web.Response(text=f'<button onclick="this.textContent=\'done\'">run</button>'
            f'<script>console.error({warning!r})</script>'+extra,content_type='text/html')
    app=web.Application(); app.router.add_get('/',page); app.router.add_get('/broken',page)
    runner=web.AppRunner(app); await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
    url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    try:
        for strict,route,expected in [(False,'/','ok'),(False,'/broken','action_failed'),(True,'/','action_failed')]:
            result=await collect_browser_evidence(app_url=url,headless=True,lenient_console=not strict,
                output_path=tmp_path/f'{strict}_{expected}.json', checks=[{'id':'flow','route':route,'actions':[
                    {'action':'click','selector':'button'},
                    {'action':'assert_text','selector':'button','value':'done','match':'exact'},
                    {'action':'assert_no_console_errors'}]}])
            check=result['checks'][0]
            assert check['status']==expected
            if not strict:
                assert check['console_warnings']==[warning]
            if route=='/broken':
                assert any('runtime broke' in e for e in check['console_errors'])
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_baseline_console_exemption_keeps_new_runtime_errors(tmp_path):
    async def page(request):
        extra = '<script>console.error("new runtime failure")</script>' if request.path == '/changed' else ''
        return web.Response(text='<main>ready</main><script>console.error("existing source error")</script>'+extra,
                            content_type='text/html')
    app = web.Application(); app.router.add_get('/', page); app.router.add_get('/changed', page)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    url = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    try:
        for suffix, expected in [('', 'ok'), ('/changed', 'action_failed')]:
            evidence = await collect_browser_evidence(app_url=url+suffix,
                checks=[{'id':'flow','route':suffix or '/', 'actions':[{'action':'assert_no_console_errors'}]}],
                output_path=tmp_path/f'{expected}.json', headless=True,
                baseline_console_errors=['existing source error'])
            assert evidence['checks'][0]['status'] == expected
    finally:
        await runner.cleanup()


def test_evaluate_syntax_error_is_invalid_test_contract():
    error = RuntimeError("Page.evaluate: SyntaxError: Illegal return statement")

    assert _is_invalid_test_contract_error("evaluate", error) is True
    assert _is_invalid_test_contract_error("click", error) is False


def test_action_settle_ms_is_explicit_and_bounded():
    assert _action_settle_ms({"action": "fill", "settle_ms": 200}, "fill") == 200
    assert _action_settle_ms({"action": "fill"}, "fill") == 0
    assert _action_settle_ms({"action": "evaluate", "settle_ms": 250}, "evaluate") == 0


@pytest.mark.anyio
async def test_numeric_progress_and_sized_file_fixture_use_real_browser(tmp_path):
    async def page(_request):
        return web.Response(text='''<input type="file" id="file"><progress id="p" max="1" value="0"></progress><output id="size"></output><output id="blank"></output>
          <script>document.querySelector('#file').onchange=e=>{
            document.querySelector('#size').textContent=e.target.files[0].size;
            setTimeout(()=>document.querySelector('#p').value=.35,150);
          };</script>''', content_type='text/html')
    app=web.Application();app.router.add_get('/',page)
    runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    try:
        result=await collect_browser_evidence(app_url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}',
            checks=[{'id':'numeric','route':'/','actions':[
                {'action':'set_input_files','selector':'#file','files':[{'name':'sized.txt','mime_type':'text/plain','content':'x','size_bytes':2048}]},
                {'action':'assert_number','selector':'#p','property':'value','min':.01,'max':.99,'timeout_ms':2000},
                {'action':'assert_text','selector':'#size','value':'2048','match':'exact'}]},
                {'id':'blank','route':'/','actions':[{'action':'assert_number','selector':'#blank','property':'textContent','min':0,'max':0,'timeout_ms':50}]}],
            output_path=tmp_path/'numeric.json',headless=True)
    finally:
        await runner.cleanup()
    assert result['checks'][0]['status']=='ok'
    assert result['checks'][0]['steps'][1]['output']['actual']==.35
    assert result['checks'][1]['status']!='ok'


def test_same_origin_route_url_accepts_only_bounded_hash_router_paths():
    assert (
        _same_origin_route_url("http://127.0.0.1:3000/preview", "/#/catalog")
        == "http://127.0.0.1:3000/#/catalog"
    )
    with pytest.raises(ValueError, match="unsafe browser route"):
        _same_origin_route_url("http://127.0.0.1:3000/preview", "/#https://evil.test")


@pytest.mark.anyio
async def test_console_gate_ignores_implicit_favicon_but_keeps_real_resource_url(
    tmp_path: Path,
):
    async def page(_request):
        return web.Response(
            text='<main>Ready</main><script src="/missing.js"></script>',
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[
                {
                    "id": "RESOURCE-CLOSURE",
                    "route": "/",
                    "actions": [{"action": "assert_no_console_errors"}],
                }
            ],
            output_path=tmp_path / "resource-errors.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    step = result["checks"][0]["steps"][0]
    assert step["ok"] is False
    assert any("missing.js" in item for item in step["output"]["actual"])
    assert all("favicon.ico" not in item for item in step["output"]["actual"])


@pytest.mark.anyio
async def test_browser_evidence_preserves_same_route_state_across_split_parts(
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
                    "id": "UI-FLOW__part1",
                    "route": "/catalog.html",
                    "actions": [
                        {"action": "fill", "selector": "#query", "value": "atlas"},
                        {"action": "evaluate", "expression": "document.querySelector('#value').textContent === 'atlas'"},
                    ],
                },
                {
                    "id": "UI-FLOW__part1__part2",
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
async def test_browser_evidence_isolates_storage_between_top_level_checks(tmp_path: Path):
    async def root(_request):
        return web.Response(text="<main>Ready</main>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/", root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[
                {
                    "id": "WRITE",
                    "route": "/",
                    "actions": [
                        {"action": "set_storage_value", "storage": "local", "key": "state", "value": "atlas"},
                        {"action": "assert_storage_value", "storage": "local", "key": "state", "value": "atlas"},
                    ],
                },
                {
                    "id": "ISOLATED",
                    "route": "/",
                    "actions": [
                        {"action": "assert_storage_value", "storage": "local", "key": "state", "value": None},
                    ],
                },
            ],
            output_path=tmp_path / "isolated.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert [item["status"] for item in result["checks"]] == ["ok", "ok"]


@pytest.mark.anyio
async def test_browser_evidence_compares_attribute_snapshot_after_reload(tmp_path: Path):
    async def root(_request):
        return web.Response(
            text="<article id='card' data-id='artifact-1'>Artifact</article>",
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[
                {
                    "id": "ATTRIBUTE-SNAPSHOT",
                    "route": "/",
                    "actions": [
                        {
                            "action": "capture_attribute",
                            "selector": "#card",
                            "name": "data-id",
                            "snapshot": "first-card-id",
                        },
                        {"action": "reload"},
                        {
                            "action": "assert_attribute",
                            "selector": "#card",
                            "name": "data-id",
                            "snapshot": "first-card-id",
                        },
                    ],
                }
            ],
            output_path=tmp_path / "attribute-snapshot.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert result["checks"][0]["status"] == "ok"
    assert result["checks"][0]["steps"][-1]["output"] == {
        "actual": "artifact-1",
        "expected": "artifact-1",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("change, expected_status", [(True, "ok"), (False, "action_failed")])
async def test_attribute_negation_requires_real_change(tmp_path, change, expected_status):
    async def root(_request):
        handler = "this.dataset.state='after'" if change else "void 0"
        return web.Response(text=f"<button id='state' data-state='before' onclick=\"{handler}\">Update</button>", content_type="text/html")
    app = web.Application(); app.router.add_get("/", root)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(app_url=f"http://127.0.0.1:{port}", checks=[{
            "id": "change", "route": "/", "actions": [
                {"action": "capture_attribute", "selector": "#state", "name": "data-state", "snapshot": "before"},
                {"action": "click", "selector": "#state"},
                {"action": "assert_attribute", "selector": "#state", "name": "data-state", "snapshot": "before", "not": True}
            ]}], output_path=tmp_path / "evidence.json", headless=True)
        assert result["checks"][0]["status"] == expected_status
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_behavioral_selector_aliases_only_apply_to_assertions(tmp_path):
    async def root(_request):
        return web.Response(
            text='<output data-testid="summary-total-reports-value">3</output>'
            '<svg data-testid="metric-total-reports-trend"></svg>',
            content_type="text/html",
        )

    app = web.Application(); app.router.add_get("/", root)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    try:
        from playwright.async_api import async_playwright
        from src.utils.playwright_browser import launch_chromium
        async with async_playwright() as playwright:
            browser = await launch_chromium(playwright, headless=True)
            page = await browser.new_page()
            await page.goto(f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/")
            assert await _resolve_behavioral_selector(
                page, '[data-testid="summary-total-reports"]', "assert_visible"
            ) == '[data-testid="summary-total-reports-value"]'
            assert await _resolve_behavioral_selector(
                page, '[data-testid="metric-total-trend"]', "assert_visible"
            ) == '[data-testid="metric-total-reports-trend"]'
            assert await _resolve_behavioral_selector(
                page, '[data-testid="metric-total-trend"]', "click"
            ) == '[data-testid="metric-total-trend"]'
            await browser.close()
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_input_value_snapshot_detects_changed_nonempty_value(tmp_path: Path):
    async def root(_request):
        return web.Response(text="<select id='filter'><option value='a'>A</option><option value='b'>B</option></select>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/", root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    capture = {"action": "assert_value", "selector": "#filter", "match": "nonempty", "capture_as": "before"}
    compare = {"action": "assert_value", "selector": "#filter", "snapshot": "before"}
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[
                {"id": "PRESERVED__part1", "route": "/", "actions": [capture]},
                {"id": "PRESERVED__part2", "route": "/", "actions": [{"action": "reload"}, compare]},
                {"id": "CHANGED", "route": "/", "actions": [capture, {"action": "select_option", "selector": "#filter", "value": "b"}, compare]},
                {"id": "ISOLATED", "route": "/", "actions": [compare]},
            ], output_path=tmp_path / "values.json", headless=True,
        )
    finally:
        await runner.cleanup()
    assert [check["status"] for check in result["checks"]] == ["ok", "ok", "action_failed", "invalid_test_contract"]
    assert result["checks"][2]["steps"][-1]["output"] == {"actual": "b", "expected": "a"}


@pytest.mark.anyio
async def test_browser_evidence_returns_to_declared_route_after_check_navigates_away(
    tmp_path: Path,
):
    async def root(_request):
        return web.Response(
            text="<main id='root'>Root <a id='go' href='/other'>Other</a></main>",
            content_type="text/html",
        )

    async def other(_request):
        return web.Response(text="<main id='other'>Other</main>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/other", other)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[
                {
                    "id": "NAVIGATE",
                    "route": "/",
                    "actions": [
                        {"action": "click", "selector": "#go"},
                        {"action": "assert_url", "value": "/other"},
                    ],
                },
                {
                    "id": "ROOT-AGAIN",
                    "route": "/",
                    "actions": [{"action": "assert_visible", "selector": "#root"}],
                },
            ],
            output_path=tmp_path / "route-reset.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert [item["status"] for item in result["checks"]] == ["ok", "ok"]


@pytest.mark.anyio
@pytest.mark.parametrize("markup,mutation,expected,passed", [
    ('<textarea id="result">old default</textarea>', "result.value='saved result'", 'saved result', True),
    ('<textarea id="result">expected</textarea>', "result.value='wrong result'", 'expected', False),
    ('<textarea id="result">expected</textarea>', "result.value=''", 'expected', False),
    ('<input id="result" value="old default">', "result.value='edited value'", 'edited value', True),
    ('<select id="result"><option value="a">Alpha</option><option value="b" label="Beta label">Beta text</option></select>', "result.value='b'", 'Beta label', True),
    ('<select id="result"><option>Alpha</option><option>Beta</option></select>', "", 'Beta', False),
    ('<span id="result">old</span>', "result.textContent='updated'", 'updated', True),
])
async def test_assert_text_reads_live_control_state(tmp_path, markup, mutation, expected, passed):
    async def root(_request):
        change = "setTimeout(()=>{" + mutation + "},30)" if passed else mutation
        return web.Response(text=markup + '<button id="change">Change</button><script>'
            + "document.querySelector('#change').onclick=()=>{" + change
            + "}</script>", content_type="text/html")
    app = web.Application()
    app.router.add_get('/', root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        evidence = await collect_browser_evidence(app_url=f'http://127.0.0.1:{port}',
            checks=[{'id':'live-state', 'route':'/', 'actions':[
                {'action':'click', 'selector':'#change'},
                {'action':'assert_text', 'selector':'#result', 'value':expected, 'match':'exact'}]}],
            output_path=tmp_path/'evidence.json', headless=True, action_timeout_ms=300)
        assert evidence['checks'][0]['status'] == ('ok' if passed else 'action_failed')
        if passed:
            assert evidence['checks'][0]['steps'][-1]['output']['actual'] == expected
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_assert_text_waits_for_delayed_ui_value(tmp_path: Path):
    async def root(_request):
        return web.Response(
            text=(
                "<button id='increment'>Increment</button><span id='total'>0</span>"
                "<script>document.querySelector('#increment').onclick=()=>"
                "setTimeout(()=>document.querySelector('#total').textContent='1',250)</script>"
            ),
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{
                "id": "DELAYED-TEXT",
                "route": "/",
                "actions": [
                    {"action": "click", "selector": "#increment"},
                    {
                        "action": "assert_text",
                        "selector": "#total",
                        "value": "1",
                        "match": "exact",
                    },
                ],
            }],
            output_path=tmp_path / "delayed-text.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert result["checks"][0]["status"] == "ok"


@pytest.mark.anyio
async def test_browser_evidence_resets_hash_drift_on_the_same_path(tmp_path: Path):
    async def root(_request):
        return web.Response(
            text="""
            <main id="home">Home <a id="go" href="#/library">Library</a></main>
            <script>
              addEventListener('hashchange', () => {
                document.querySelector('main').id = location.hash ? 'library' : 'home';
              });
            </script>
            """,
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[
                {
                    "id": "HASH-NAVIGATE",
                    "route": "/",
                        "actions": [
                            {"action": "click", "selector": "#go"},
                            {
                                "action": "evaluate",
                                "expression": "location.hash === '#/library'",
                            },
                        ],
                },
                {
                    "id": "ROOT-AGAIN",
                    "route": "/",
                    "actions": [{"action": "assert_visible", "selector": "#home"}],
                },
            ],
            output_path=tmp_path / "hash-route-reset.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert [item["status"] for item in result["checks"]] == ["ok", "ok"]
    assert result["checks"][1]["url"].endswith("/")


@pytest.mark.anyio
async def test_browser_evidence_preserves_hash_across_split_check_parts(tmp_path: Path):
    async def root(_request):
        return web.Response(
            text="""
            <button id="set">Set</button><output id="value">All</output>
            <script>
              set.onclick = () => { location.hash = 'gallery?type=Timepiece'; };
              function render() {
                value.textContent = location.hash.includes('Timepiece') ? 'Timepiece' : 'All';
              }
              addEventListener('hashchange', render); render();
            </script>
            """,
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[
                {
                    "id": "FILTER__part1",
                    "route": "/",
                    "actions": [
                        {"action": "click", "selector": "#set"},
                        {"action": "assert_hash", "value": "Timepiece", "match": "contains"},
                        {"action": "reload"},
                    ],
                },
                {
                    "id": "FILTER__part2",
                    "route": "/",
                    "actions": [
                        {"action": "assert_text", "selector": "#value", "value": "Timepiece"}
                    ],
                },
            ],
            output_path=tmp_path / "split-hash-state.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert [item["status"] for item in result["checks"]] == ["ok", "ok"]
    assert "Timepiece" in result["checks"][1]["url"]


@pytest.mark.anyio
async def test_browser_evidence_executes_typed_dom_aria_and_console_assertions(
    tmp_path: Path,
):
    async def page(_request):
        return web.Response(
            text="""
            <label id="query-label" for="query">Search</label>
            <input id="query" aria-labelledby="query-label" required>
            <button id="toggle" aria-expanded="false">Open</button>
            <section id="panel" hidden>Details</section>
            <script>
              toggle.addEventListener('click', () => {
                toggle.setAttribute('aria-expanded', 'true');
                panel.hidden = false;
                localStorage.setItem('panel', JSON.stringify({state: 'open'}));
              });
            </script>
            """,
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[
                {
                    "id": "UI-ARIA",
                    "route": "/",
                    "actions": [
                        {"action": "click", "selector": "#toggle"},
                        {
                            "action": "assert_aria",
                            "selector": "#toggle",
                            "attribute": "aria-expanded",
                            "value": True,
                        },
                    ],
                },
                    {
                        "id": "UI-STORAGE",
                        "route": "/",
                        "actions": [
                            {"action": "click", "selector": "#toggle"},
                            {
                                "action": "assert_storage_value",
                            "storage": "local",
                            "key": "panel",
                            "value": "open",
                            "match": "contains",
                        }
                    ],
                },
                {
                    "id": "UI-CONSOLE",
                    "route": "/",
                    "actions": [{"action": "assert_no_console_errors"}],
                },
                {
                    "id": "UI-AX-STATE",
                    "route": "/",
                    "actions": [
                        {
                            "action": "assert_aria",
                            "selector": "#query",
                            "attribute": "role",
                            "value": "textbox",
                        },
                        {
                            "action": "assert_aria",
                            "selector": "#query",
                            "attribute": "accessible_name",
                            "value": "Search",
                        },
                        {
                            "action": "assert_property",
                            "selector": "#query",
                            "name": "required",
                            "value": True,
                        },
                    ],
                },
            ],
            output_path=tmp_path / "typed.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert [item["status"] for item in result["checks"]] == ["ok", "ok", "ok", "ok"]
    assert result["checks"][0]["steps"][-1]["output"] == {
        "actual": "true",
        "expected": True,
    }
    assert result["checks"][-1]["evidence_route"] == [
        "real_browser", "ax_semantics", "internal_state", "dom"
    ]


@pytest.mark.anyio
async def test_browser_evidence_executes_hash_storage_fixture_and_computed_style(
    tmp_path: Path,
):
    async def page(_request):
        return web.Response(
            text="""
            <style>#panel { display: none; }</style>
            <a id="report-link" href="#/report">Report</a>
            <section id="panel">Report panel</section>
            <output id="stored"></output>
            <script>
              function render() {
                if (location.hash === '#/report') panel.style.display = 'block';
                stored.textContent = localStorage.getItem('fixture') || '';
              }
              addEventListener('hashchange', render);
              render();
            </script>
            """,
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[
                {
                    "id": "HASH-STATE",
                    "route": "/",
                    "actions": [
                        {
                            "action": "set_storage_value",
                            "storage": "local",
                            "key": "fixture",
                            "value": {"page": 2},
                            "encoding": "json",
                        },
                        {"action": "set_hash", "value": "#/report", "settle_ms": 50},
                        {"action": "reload"},
                        {"action": "assert_hash", "value": "#/report"},
                        {
                            "action": "assert_computed_style",
                            "selector": "#panel",
                            "property": "display",
                            "value": "block",
                        },
                        {
                            "action": "assert_text",
                            "selector": "#stored",
                            "value": '"page":2',
                            "match": "contains",
                        },
                    ],
                }
            ],
            output_path=tmp_path / "hash-state-style.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert [item["status"] for item in result["checks"]] == ["ok"]
    assert "rendered_style" in result["checks"][0]["evidence_route"]


@pytest.mark.anyio
async def test_collection_visibility_and_text_assertions_are_not_strict_singletons(
    tmp_path: Path,
):
    async def page(_request):
        return web.Response(
            text=(
                '<article class="card">Timepiece One</article>'
                '<article class="card">Timepiece Two</article>'
            ),
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{
                "id": "COLLECTION",
                "route": "/",
                "actions": [
                    {"action": "assert_visible", "selector": ".card"},
                    {
                        "action": "assert_text",
                        "selector": ".card",
                        "value": "Timepiece",
                        "match": "contains",
                    },
                ],
            }],
            output_path=tmp_path / "collection.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert result["checks"][0]["status"] == "ok"
    assert result["checks"][0]["steps"][0]["output"]["actual"] == [True, True]


@pytest.mark.anyio
async def test_visible_assertion_waits_for_async_target_after_click(tmp_path: Path):
    async def page(_request):
        return web.Response(
            text=(
                '<button id="start">Start</button><div id="target" hidden>Done</div>'
                '<script>start.onclick=()=>setTimeout(()=>target.hidden=false,80)</script>'
            ),
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[
                {
                    "id": "ASYNC-TARGET",
                    "route": "/",
                    "actions": [
                        {"action": "click", "selector": "#start"},
                        {"action": "assert_visible", "selector": "#target"},
                    ],
                }
            ],
            output_path=tmp_path / "async-target.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert result["checks"][0]["status"] == "ok"


@pytest.mark.anyio
async def test_failed_flow_stops_before_cascading_dependent_timeouts(tmp_path: Path):
    async def page(_request):
        return web.Response(text="<main>Ready</main>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{"id": "FAIL-FAST", "route": "/", "actions": [
                {"action": "assert_visible", "selector": "#missing"},
                {"action": "click", "selector": "#also-missing"},
            ]}],
            output_path=tmp_path / "fail-fast.json",
            headless=True,
            action_timeout_ms=100,
        )
    finally:
        await runner.cleanup()

    assert result["checks"][0]["status"] == "action_failed"
    assert len(result["checks"][0]["steps"]) == 1


@pytest.mark.anyio
async def test_visible_failure_records_hidden_ancestor_diagnostic(tmp_path: Path):
    async def page(_request):
        return web.Response(
            text='<section id="gallery" style="display:none"><div id="detail">Ready</div></section>',
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{"id": "HIDDEN-PARENT", "route": "/", "actions": [
                {"action": "assert_visible", "selector": "#detail"},
            ]}],
            output_path=tmp_path / "hidden-parent.json",
            headless=True,
            action_timeout_ms=100,
        )
    finally:
        await runner.cleanup()

    step = result["checks"][0]["steps"][0]
    assert step["visibility_diagnostic"]["matched_count"] == 1
    assert step["visibility_diagnostic"]["hidden_ancestors"][0]["label"] == "#gallery"
    assert "display=none" in step["visibility_diagnostic"]["hidden_ancestors"][0]["hidden_reasons"]


@pytest.mark.anyio
async def test_missing_state_selector_records_existing_base_selector(tmp_path: Path):
    async def page(_request):
        return web.Response(
            text='<a class="sidebar-item" data-name="Audio Studio Driver">Audio</a>',
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{"id": "STATE", "route": "/", "actions": [{
                "action": "assert_visible",
                "selector": 'a.sidebar-item[data-name="Audio Studio Driver"][class~="highlighted"]',
            }]}],
            output_path=tmp_path / "state.json",
            headless=True,
            action_timeout_ms=100,
        )
    finally:
        await runner.cleanup()

    diagnostic = result["checks"][0]["steps"][0]["visibility_diagnostic"]
    assert diagnostic["matched_count"] == 0
    assert diagnostic["base_matched_count"] == 1
    assert diagnostic["base_selector"] == 'a.sidebar-item[data-name="Audio Studio Driver"]'


@pytest.mark.anyio
async def test_hidden_assertion_accepts_css_interaction_hidden_surface(tmp_path: Path):
    async def page(_request):
        return web.Response(
            text=(
                "<style>#menu{transition:opacity .05s}"
                ".closed{opacity:0;pointer-events:none}</style>"
                '<button id="close-menu">Close</button><nav id="menu">Menu</nav>'
                '<script>document.querySelector("#close-menu").onclick=()=>'
                'document.querySelector("#menu").className="closed"</script>'
            ),
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{"id": "CSS-HIDDEN", "route": "/", "actions": [
                {"action": "click", "selector": "#close-menu"},
                {"action": "assert_hidden", "selector": "#menu"},
            ]}],
            output_path=tmp_path / "css-hidden.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert result["checks"][0]["status"] == "ok"


@pytest.mark.anyio
async def test_click_time_scroll_snapshot_survives_actionability_scroll(tmp_path: Path):
    async def page(_request):
        return web.Response(
            text="""
              <main style="height:1800px;padding-top:700px">
                <button id="open-detail">Open</button><button id="go-back">Back</button>
              </main>
              <script>
                let saved = 0;
                document.querySelector('#open-detail').addEventListener('click', () => { saved = scrollY; scrollTo(0, 0); });
                document.querySelector('#go-back').addEventListener('click', () => scrollTo(0, saved));
              </script>
            """,
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{"id": "SCROLL-SNAPSHOT", "route": "/", "actions": [
                {"action": "set_viewport", "width": 800, "height": 400},
                {"action": "scroll", "y": 500},
                {
                    "action": "click",
                    "selector": "#open-detail",
                    "capture_scroll_as": "before-open",
                },
                {"action": "click", "selector": "#go-back"},
                {"action": "assert_scroll", "snapshot": "before-open"},
            ]}],
            output_path=tmp_path / "scroll-snapshot.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    check = result["checks"][0]
    assert check["status"] == "ok"
    assert check["steps"][2]["output"]["scroll_y"] > 0


@pytest.mark.anyio
async def test_in_view_assertion_uses_viewport_geometry(tmp_path: Path):
    async def page(_request):
        return web.Response(
            text='<main style="height:1200px"><div id="target" style="margin-top:700px">Target</div></main>',
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{"id": "IN-VIEW", "route": "/", "actions": [
                {"action": "set_viewport", "width": 800, "height": 400},
                {"action": "scroll", "y": 600},
                {"action": "assert_in_view", "selector": "#target"},
            ]}],
            output_path=tmp_path / "in-view.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert result["checks"][0]["status"] == "ok"


@pytest.mark.anyio
async def test_runtime_page_error_is_attached_to_failed_check(tmp_path: Path):
    async def page(_request):
        return web.Response(
            text=(
                '<button id="ready">Ready</button>'
                '<script>document.addEventListener("DOMContentLoaded",()=>missingHelper())</script>'
            ),
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{
                "id": "RUNTIME-ERROR",
                "route": "/",
                "actions": [
                    {"action": "assert_visible", "selector": "#ready"},
                ],
            }],
            output_path=tmp_path / "runtime-error.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    check = result["checks"][0]
    assert check["status"] == "action_failed"
    assert any("missingHelper" in error for error in check["console_errors"])


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


@pytest.mark.anyio
async def test_new_surface_visual_sanity_catches_nearly_invisible_text(tmp_path: Path):
    async def page(_request):
        return web.Response(
            text="""
            <style>
              body { color: #e6ebf5; background: #0a0e1a; }
              #panel { display: none; background: #f9fafb; }
            </style>
            <button id="show">Show summary</button>
            <section id="panel"><span>0 Downloads</span></section>
            <script>show.onclick = () => panel.style.display = 'block';</script>
            """,
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{
                "id": "NEW-SUMMARY",
                "route": "/",
                "actions": [
                    {"action": "click", "selector": "#show"},
                    {"action": "assert_visible", "selector": "#panel"},
                ],
            }],
            visual_sanity_selectors=[{
                "route": "/", "selector": "#panel", "min_count": 1, "max_count": 1
            }],
            output_path=tmp_path / "contrast.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    check = result["checks"][0]
    assert all(step["ok"] for step in check["steps"])
    assert check["status"] == "action_failed"
    assert check["visual_sanity"]["kind"] == "severe_text_contrast"
    assert check["visual_sanity"]["issues"][0]["ratio"] < 1.5


@pytest.mark.anyio
async def test_webcompass_risk_audits_distinguish_eleven_defects_from_fixed_dom(
    tmp_path: Path,
):
    svg = (
        "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
        "width='100' height='50'%3E%3Crect width='100' height='50'/%3E%3C/svg%3E"
    )
    page_state = {"fixed": False}

    def html() -> str:
        if not page_state["fixed"]:
            return f"""
            <style>
              #occlusion-wrap {{ position: relative; width: 120px; height: 40px; }}
              #occlusion-cover {{ position: absolute; inset: 0; background: red; z-index: 2; }}
              #crowding {{ display: flex; gap: 0; }}
              #overlap {{ position: relative; height: 30px; }}
              #overlap span {{ position: absolute; left: 0; top: 0; }}
              #alignment {{ display: flex; }} #alignment > :last-child {{ margin-top: 24px; }}
              #contrast {{ color: rgb(120,120,120); background: rgb(130,130,130); }}
              #overflow {{ width: 100px; overflow-x: visible; }} #overflow > div {{ width: 220px; }}
              #loss {{ pointer-events: none; }}
            </style>
            <div id="occlusion-wrap"><button id="occlusion">Open</button><i id="occlusion-cover"></i></div>
            <div id="crowding"><button data-testid="crowd-a">A</button><button data-testid="crowd-b">B</button></div>
            <div id="overlap"><span>First text</span><span>Second text</span></div>
            <div id="alignment"><i>A</i><i>B</i><i>C</i></div>
            <div id="contrast">Unreadable text</div>
            <div id="overflow"><div>wide content</div></div>
            <div id="sizing"><img src="{svg}" style="width:100px;height:100px"></div>
            <button id="loss">Save</button>
            <div id="semantic" onclick="void 0">Open settings</div>
            <div id="nesting"><button>Outer <span role="button">Inner</span></button></div>
            <div id="missing"><img src="{svg}"></div>
            """
        return f"""
        <style>
          #occlusion-wrap {{ position: relative; width: 120px; height: 40px; }}
          #occlusion-cover {{ display: none; }}
          #crowding {{ display: flex; gap: 8px; }}
          #overlap {{ display: flex; gap: 8px; }}
          #alignment {{ display: flex; }}
          #contrast {{ color: black; background: white; }}
          #overflow {{ width: 100px; overflow-x: auto; }} #overflow > div {{ width: 220px; }}
        </style>
        <div id="occlusion-wrap"><button id="occlusion">Open</button><i id="occlusion-cover"></i></div>
        <div id="crowding"><button data-testid="crowd-a">A</button><button data-testid="crowd-b">B</button></div>
        <div id="overlap"><span>First text</span><span>Second text</span></div>
        <div id="alignment"><i>A</i><i>B</i><i>C</i></div>
        <div id="contrast">Readable text</div>
        <div id="overflow"><div>wide content</div></div>
        <div id="sizing"><img alt="chart" src="{svg}" style="width:100px;height:50px"></div>
        <button id="loss">Save</button>
        <button id="semantic">Open settings</button>
        <div id="nesting"><button>Outer</button><button>Inner</button></div>
        <div id="missing"><img alt="preview" src="{svg}"></div>
        """

    async def page(_request):
        return web.Response(text=html(), content_type="text/html")

    checks = [
        {"id": "RISK-OCCLUSION", "route": "/", "actions": [{
            "action": "assert_webcompass_risk", "selector": "#occlusion",
            "defect_type": "Occlusion",
        }]},
        {"id": "RISK-CROWDING", "route": "/", "actions": [{
            "action": "assert_webcompass_risk", "selector": "#crowding",
            "defect_type": "Crowding",
        }]},
        {"id": "RISK-TEXT", "route": "/", "actions": [{
            "action": "assert_webcompass_risk", "selector": "#overlap",
            "defect_type": "Text Overlap",
        }]},
        {"id": "RISK-ALIGNMENT", "route": "/", "actions": [{
            "action": "assert_webcompass_risk", "selector": "#alignment",
            "defect_type": "Alignment",
        }]},
        {"id": "RISK-CONTRAST", "route": "/", "actions": [{
            "action": "assert_webcompass_risk", "selector": "#contrast",
            "defect_type": "Color Contrast",
        }]},
        {"id": "RISK-OVERFLOW", "route": "/", "actions": [{
            "action": "assert_webcompass_risk", "selector": "#overflow",
            "defect_type": "Overflow",
        }]},
        {"id": "RISK-SIZING", "route": "/", "actions": [{
            "action": "assert_webcompass_risk", "selector": "#sizing",
            "defect_type": "Sizing Proportion",
        }]},
        {"id": "RISK-LOSS", "route": "/", "actions": [{
            "action": "assert_webcompass_risk", "selector": "#loss",
            "defect_type": "Loss of Interactivity",
        }]},
        {"id": "RISK-SEMANTIC", "route": "/", "actions": [{
            "action": "assert_webcompass_risk", "selector": "#semantic",
            "defect_type": "Semantic Error",
        }]},
        {"id": "RISK-NESTING", "route": "/", "actions": [{
            "action": "assert_webcompass_risk", "selector": "#nesting",
            "defect_type": "Nesting Error",
        }]},
        {"id": "RISK-MISSING", "route": "/", "actions": [{
            "action": "assert_webcompass_risk", "selector": "#missing",
            "defect_type": "Missing Attributes",
        }]},
    ]
    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        broken = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=checks,
            output_path=tmp_path / "eleven-broken.json",
            headless=True,
        )
        page_state["fixed"] = True
        fixed = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=checks,
            output_path=tmp_path / "eleven-fixed.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert [item["status"] for item in broken["checks"]] == ["action_failed"] * 11
    assert [item["status"] for item in fixed["checks"]] == ["ok"] * 11
    assert all(
        item["steps"][0]["output"]["actual"]["issues"]
        for item in broken["checks"]
    )


@pytest.mark.anyio
async def test_risk_selectors_offscreen_controls_and_accessible_clipping(tmp_path):
    async def page(_request):
        return web.Response(text="""
        <style>.sr-only {position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}
        #far {position:absolute;top:1800px} #broken {pointer-events:none}</style>
        <button>Open workspace</button><label class="sr-only">Accessible input label</label>
        <button id="far">Far below fold</button><button id="broken">Broken control</button>
        """, content_type="text/html")
    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        evidence = await collect_browser_evidence(app_url=f"http://127.0.0.1:{port}",
            checks=[{"id": str(i), "route": "/", "actions": [{"action": "assert_webcompass_risk",
                "selector": selector, "defect_type": defect}]} for i, (selector, defect) in enumerate([
                    ("button:has-text('Open workspace')", "Loss of Interactivity"),
                    ("#far", "Loss of Interactivity"), (".sr-only", "Overflow"),
                    ("#broken", "Loss of Interactivity")])],
            output_path=tmp_path / "risks.json", headless=True)
    finally:
        await runner.cleanup()
    assert [c["status"] for c in evidence["checks"]] == ["ok", "ok", "ok", "action_failed"]


@pytest.mark.anyio
async def test_wrapped_alignment_and_focusable_groups_preserve_real_defects(tmp_path):
    async def page(_request):
        return web.Response(text="""
        <style>.row{display:flex;flex-wrap:wrap;align-items:flex-start;gap:10px;width:350px}
        .row>span{width:100px;height:40px} #bad>span:last-child{margin-top:15px}</style>
        <div id="wrap" class="row"><span>First</span><span>Second</span><span>Third</span><span>Next row</span></div>
        <div id="bad" class="row"><span>First</span><span>Second</span><span>Misaligned</span></div>
        <article id="group" tabindex="0"><button>Review</button><button>Edit</button></article>
        <div id="nested" role="button" tabindex="0">Outer<button>Inner</button></div>
        """,content_type="text/html")
    app=web.Application();app.router.add_get('/',page)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    try:
        evidence=await collect_browser_evidence(app_url=f'http://127.0.0.1:{port}',
            checks=[{'id':selector,'route':'/','actions':[{'action':'assert_webcompass_risk',
                'selector':selector,'defect_type':defect}]} for selector,defect in [('#wrap','Alignment'),
                ('#bad','Alignment'),('#group','Nesting Error'),('#nested','Nesting Error')]],
            output_path=tmp_path/'wrapped-risks.json',headless=True)
    finally:
        await runner.cleanup()
    assert [c['status'] for c in evidence['checks']]==['ok','action_failed','ok','action_failed']


@pytest.mark.anyio
async def test_scrolled_controls_do_not_hit_viewport_edge_or_sticky_header(tmp_path):
    async def page(_request):
        return web.Response(text="""
        <style>
        header{position:fixed;top:0;left:0;width:100%;height:50px;background:white;z-index:10}
        button{position:absolute;width:100px;height:100px;left:100px}
        #edge{top:-80px} #partial{top:-30px;left:230px}
        #covered{top:5px;left:360px;height:35px}
        #blocked{top:200px;left:500px}
        #cover{position:absolute;top:200px;left:500px;width:100px;height:100px;background:red;z-index:2}
        </style><header><div>Navigation</div></header>
        <button id="edge">Edge</button><button id="partial">Partly scrolled</button>
        <button id="covered">Covered</button><button id="blocked">Blocked</button><div id="cover"></div>
        """,content_type="text/html")
    app=web.Application();app.router.add_get('/',page)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    try:
        evidence=await collect_browser_evidence(app_url=f'http://127.0.0.1:{port}',
            checks=[{'id':selector,'route':'/','actions':[{'action':'assert_webcompass_risk',
                'selector':selector,'defect_type':'Occlusion'}]} for selector in ['#edge','#partial','#covered','#blocked']],
            output_path=tmp_path/'scrolled-risks.json',headless=True)
    finally:
        await runner.cleanup()
    assert [c['status'] for c in evidence['checks']]==['ok','ok','action_failed','action_failed']


@pytest.mark.anyio
async def test_modal_background_and_centered_mixed_controls_are_not_layout_defects(tmp_path):
    async def page(_request):
        return web.Response(text='''<style>
        #background,#modal {position:fixed;left:40px;top:40px;width:400px;height:300px;background:white}
        #modal {z-index:2;animation:enter .3s} @keyframes enter {from {opacity:0} to {opacity:1}}
        #mixed {display:flex;align-items:center;gap:12px}
        #mixed input[type=checkbox] {width:16px;height:16px}
        #mixed input[type=text],#mixed button {height:48px}
        #covered {position:relative;width:120px;height:40px}
        #cover {position:absolute;inset:0;background:red;z-index:3}
        #overlap {position:relative;height:50px}
        #overlap span {position:absolute;left:0;top:0}
        </style><div id="background" class="surface" data-testid="background"><span>Background text</span><button>Background action</button></div>
        <div id="modal" class="surface" role="dialog" aria-modal="true"><span>Modal text</span>
        <div id="mixed"><input type="checkbox" aria-label="Done"><input type="text" aria-label="Name"><button>Remove</button></div>
        <div id="covered"><button>Covered action</button><i id="cover"></i></div>
        <div id="overlap"><span>First overlapping text</span><span>Second overlapping text</span></div></div>''',content_type="text/html")
    app = web.Application(); app.router.add_get("/",page)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner,"127.0.0.1",0); await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        evidence = await collect_browser_evidence(app_url=f"http://127.0.0.1:{port}",
            checks=[{"id":str(i),"route":"/","actions":[{"action":"assert_webcompass_risk","selector":selector,"defect_type":defect}]} for i,(selector,defect) in enumerate([
                ("#background", "Occlusion"), ("#background, #modal > span", "Text Overlap"),
                ("#mixed", "Alignment"), ("#covered button", "Occlusion"), ("#overlap", "Text Overlap"),
                ("#background button, #mixed button", "Loss of Interactivity"), ("#covered button", "Loss of Interactivity")])],
            output_path=tmp_path/"modal-risks.json",headless=True)
    finally:
        await runner.cleanup()
    assert [c["status"] for c in evidence["checks"]] == ["ok","ok","ok","action_failed","action_failed","ok","action_failed"]


@pytest.mark.anyio
async def test_wrapped_inline_text_uses_line_rects_not_union_box(tmp_path):
    async def page(_request):
        return web.Response(text='''<main id="card" style="width:180px">
        <p><strong>Source:</strong> <span>Central Westgate event gallery with a long wrapped label</span></p>
        <p><strong>Note:</strong> <span style="display:block">Add event photos when available.</span></p>
        </main>''', content_type="text/html")
    app = web.Application(); app.router.add_get("/", page)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        evidence = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=[{"id":"wrapped","route":"/","actions":[{
                "action":"assert_webcompass_risk","selector":"#card",
                "defect_type":"Text Overlap",
            }]}],
            output_path=tmp_path/"wrapped-text.json", headless=True,
        )
    finally:
        await runner.cleanup()
    assert evidence["checks"][0]["status"] == "ok"


@pytest.mark.anyio
async def test_table_boxes_can_touch_but_crowded_child_buttons_still_fail(tmp_path):
    async def page(_request):
        return web.Response(text='''<style>table{border-collapse:collapse}td,th{padding:12px}
          #bad td{padding:12px}button{margin:0}</style>
          <table id="good"><thead><tr><th data-testid="header-a">A</th><th data-testid="header-b">B</th></tr></thead>
          <tbody><tr data-testid="row-a"><td>Alpha</td><td>Beta</td></tr>
          <tr data-testid="row-b"><td>Gamma</td><td>Delta</td></tr></tbody></table>
          <table id="bad"><tr><td><button>A</button><button>B</button></td></tr></table>''',content_type='text/html')
    app=web.Application();app.router.add_get('/',page)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    try:
        result=await collect_browser_evidence(app_url=f'http://127.0.0.1:{port}',
            checks=[{'id':name,'route':'/','actions':[{'action':'assert_webcompass_risk',
                    'selector':'#'+name,'defect_type':'Crowding'}]} for name in ['good','bad']],
            output_path=tmp_path/'table-spacing.json',headless=True)
    finally:
        await runner.cleanup()
    assert [check['status'] for check in result['checks']]==['ok','action_failed']


@pytest.mark.anyio
async def test_visual_capture_uses_completed_interaction_flow(tmp_path):
    async def page(_request):
        return web.Response(text='''<button onclick="document.querySelector('main').hidden=false">Open report</button><main hidden style="background:#114488;color:white;height:400px">Report contents</main>''',content_type='text/html')
    app = web.Application(); app.router.add_get('/',page)
    runner=web.AppRunner(app); await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0); await site.start()
    port=site._server.sockets[0].getsockname()[1]
    try:
        result=await collect_browser_evidence(app_url=f'http://127.0.0.1:{port}',headless=True,
            output_path=tmp_path/'evidence.json',capture_screenshots=True,checks=[
            {'id':'report__part1','route':'/','actions':[{'action':'click','selector':'button'}]},
            {'id':'report__part2','route':'/','actions':[{'action':'assert_visible','selector':'main'}]}],
            visual_sanity_selectors=[{'route':'/','selector':'main'}])
        assert [c['status'] for c in result['checks']]==['ok','ok']
        assert 'screenshot' not in result['checks'][0]
        screenshot=tmp_path/result['checks'][1]['screenshot']
        assert screenshot.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
        assert result['checks'][1]['screenshot_selector'] == 'main'
        import struct
        assert struct.unpack('>II', screenshot.read_bytes()[16:24]) == (1264,400)
    finally:
        await runner.cleanup()


@pytest.mark.anyio
@pytest.mark.parametrize("credit,extra,mode,passed", [
    ('CCSleep team', '', 'contains', True),
    ('CCsleep team', '', 'contains', False),
    ('CCSleep team', '<article data-entry-id="x">Wrong credit</article>', 'contains', False),
    ('CCSleep team', '<article data-entry-id="x">Credit: CCSleep team</article>', 'contains', True),
    ('CCSleep team', '', 'exact', False),
])
async def test_text_contains_deduplicates_nested_matches_but_checks_each_region(tmp_path, credit, extra, mode, passed):
    async def root(_request):
        return web.Response(text='<article data-entry-id="x">Credit: '+credit+
            '<button data-entry-id="x">Edit</button></article>'+extra, content_type='text/html')
    app = web.Application()
    app.router.add_get('/', root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        evidence = await collect_browser_evidence(app_url=f'http://127.0.0.1:{port}',
            checks=[{'id':'card-credit', 'route':'/', 'actions':[
                {'action':'assert_text', 'selector':'[data-entry-id="x"]',
                 'value':'Credit: CCSleep team', 'match':mode}]}],
            output_path=tmp_path/'evidence.json', headless=True, action_timeout_ms=300)
        assert evidence['checks'][0]['status'] == ('ok' if passed else 'action_failed')
        assert evidence['policy_version']
    finally:
        await runner.cleanup()


@pytest.mark.anyio
async def test_risk_group_collects_multiple_defects_without_repeating_toggle(tmp_path):
    from src.orchestration.repair_packet import write_repair_packet
    from src.orchestration.phases import _append_hidden_risk_repairs
    app = web.Application()
    async def page(request):
        return web.Response(text='''
      <button onclick="panel.hidden=!panel.hidden">Toggle</button>
      <section id="panel" hidden style="width:200px;overflow:hidden">
        <div style="width:600px">Wide content</div>
        <input><textarea></textarea>
      </section>''', content_type='text/html')
    app.router.add_get('/', page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    checks = [{
        'id': f'RISK-SCENE-01__part{i}', 'route': '/',
        'origin': 'source_edit_risk_analysis', 'repair_type': kind,
        'actions': [{'action': 'click', 'selector': 'button'},
                    {'action': 'assert_webcompass_risk', 'selector': '#panel', 'defect_type': kind}],
    } for i, kind in enumerate(['Overflow', 'Missing Attributes'], 1)]
    try:
        evidence = await collect_browser_evidence(app_url=f'http://127.0.0.1:{port}', checks=checks,
            output_path=tmp_path/'.harness/hidden_oracle_evidence_round_1.json',
            headless=True, capture_screenshots=True)
    finally:
        await runner.cleanup()
    assert [r['status'] for r in evidence['checks']] == ['action_failed', 'action_failed']
    assert all(r.get('screenshot') for r in evidence['checks'])
    assert [s['action'] for s in evidence['checks'][1]['steps']] == ['assert_webcompass_risk']
    grades = {}
    _append_hidden_risk_repairs(grades=grades, hidden_checks=checks, hidden_evidence=evidence,
        failed_check_ids=[c['id'] for c in checks])
    packet = write_repair_packet(workdir=tmp_path, round_num=1, sprint_num=1, grades=grades)
    assert {d['task_type'] for d in packet['repair_task_descriptions']} == {'Overflow', 'Missing Attributes'}
    assert len(packet['failed_checks']) == 2
    issues = packet['failed_checks'][1]['steps'][-1]['output']['actual']['issues']
    assert len(issues) == 2


@pytest.mark.anyio
async def test_failed_risk_setup_blocks_siblings_without_inventing_defects(tmp_path):
    app = web.Application()
    async def page(request):
        return web.Response(text='<button>Existing</button>', content_type='text/html')
    app.router.add_get('/', page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    checks = [{'id':f'RISK__part{i}', 'route':'/', 'origin':'source_edit_risk_analysis',
        'actions':[{'action':'click','selector':'#absent'},
                   {'action':'assert_webcompass_risk','selector':'#panel','defect_type':kind}]}
        for i,kind in enumerate(['Overflow','Missing Attributes'],1)]
    checks.append({'id':'missing-region', 'route':'/', 'actions':[
        {'action':'assert_webcompass_risk','selector':'#absent-region','defect_type':'Color Contrast'}]})
    try:
        result = await collect_browser_evidence(app_url=f'http://127.0.0.1:{port}',checks=checks,
            output_path=tmp_path/'evidence.json',headless=True,action_timeout_ms=100)
    finally:
        await runner.cleanup()
    assert [c['status'] for c in result['checks']] == ['action_failed','blocked_by_setup','ok']
    assert result['checks'][2]['steps'][0]['output']['actual']['applicable'] is False
    assert result['checks'][1]['steps'] == []


@pytest.mark.anyio
async def test_key_press_dispatches_modifier_chords_and_character_shortcuts(tmp_path):
    async def page(_request):
        return web.Response(text='''<div id="card" tabindex="0">Card</div><output id="moves">0</output><input id="draft" value="old draft"><output id="shortcut"></output>
          <script>card.onkeydown=e=>{if(e.altKey&&e.key==='ArrowDown'){e.preventDefault();moves.textContent=Number(moves.textContent)+1;}if(e.key==='q')shortcut.textContent='opened';};</script>''', content_type='text/html')
    app=web.Application();app.router.add_get('/',page)
    runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    try:
        result=await collect_browser_evidence(app_url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}',
            checks=[{'id':'keys','route':'/','actions':[
                {'action':'key_press','selector':'#card','key':'Alt+ArrowDown','count':2},
                {'action':'assert_text','selector':'#moves','value':'2','match':'exact'},
                {'action':'key_press','selector':'#card','key':'q'},
                {'action':'assert_text','selector':'#shortcut','value':'opened','match':'exact'},
                {'action':'key_press','selector':'#draft','key':'ControlOrMeta+A'},
                {'action':'key_press','selector':'#draft','key':'Replacement text'},
                {'action':'assert_value','selector':'#draft','value':'Replacement text'},
            ]}],output_path=tmp_path/'keys.json',headless=True)
    finally:await runner.cleanup()
    assert result['checks'][0]['status']=='ok',result


@pytest.mark.anyio
async def test_ambiguous_wait_is_contract_error_not_broken_product(tmp_path):
    async def page(_request):
        return web.Response(text='<article>Alex</article><article>Alex</article>', content_type='text/html')
    app = web.Application(); app.router.add_get('/', page)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    try:
        result = await collect_browser_evidence(
            app_url=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}',
            checks=[{'id':'ambiguous','route':'/','actions':[
                {'action':'wait_for','selector':'article','state':'attached'}]}],
            output_path=tmp_path/'ambiguous.json', headless=True)
    finally:
        await runner.cleanup()
    assert result['checks'][0]['status'] == 'invalid_test_contract'
    assert '<article>Alex</article>' in result['checks'][0]['failure_dom']
