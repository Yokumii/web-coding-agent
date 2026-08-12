from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from src.agents.sdk_runner import (
    AgentRunStats,
    _DEFAULT_CLAUDE_CODE_ENV,
    SdkTraceWriter,
    build_agent_run_stats,
    build_agent_options,
    _CLAUDE_SDK_SKIP_VERSION_CHECK,
    make_tool_permission_callback,
    run_sdk_agent,
)
from src.config import HarnessConfig
from src.orchestration.pricing import estimate_cost_usd
from src.utils.claude_http_trace import (
    DEFAULT_ANTHROPIC_BASE_URL,
    _uses_loopback_host,
    capture_claude_http_traffic,
    generate_claude_http_trace_html,
)


def test_claude_http_trace_bypasses_proxy_for_loopback_upstreams():
    assert _uses_loopback_host("http://127.0.0.1:5173") is True
    assert _uses_loopback_host("http://[::1]:5173") is True
    assert _uses_loopback_host("https://api.example.com") is False


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_permission_callback_denies_path_escape(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=False,
        allow_playwright=False,
    )
    result = await callback("Read", {"file_path": "../secret.txt"}, None)
    assert result.behavior == "deny"
    assert "path escapes workdir" in result.message


@pytest.mark.anyio
async def test_permission_callback_denies_disallowed_bash(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
        bash_profile="read_only",
    )
    result = await callback("Bash", {"command": "pwd && ls"}, None)
    assert result.behavior == "deny"
    assert "shell control operator not allowed" in result.message


@pytest.mark.anyio
async def test_permission_callback_allows_compound_bash_in_full_profile(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
        bash_profile="full",
    )
    result = await callback(
        "Bash",
        {"command": "cd frontend && npm run build"},
        None,
    )
    assert result.behavior == "allow"


@pytest.mark.anyio
async def test_permission_callback_allows_pipeline_bash_in_full_profile(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
        bash_profile="full",
    )
    result = await callback(
        "Bash",
        {"command": "find frontend/src -type f | head -40"},
        None,
    )
    assert result.behavior == "allow"


@pytest.mark.anyio
async def test_permission_callback_still_denies_background_bash_in_full_profile(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
        bash_profile="full",
    )
    result = await callback("Bash", {"command": "npm run dev &"}, None)
    assert result.behavior == "deny"
    assert "shell control operator not allowed: &" in result.message


@pytest.mark.anyio
async def test_permission_callback_allows_playwright_mcp(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=False,
        allow_playwright=True,
        frontend_port=5173,
    )
    result = await callback(
        "mcp__playwright__browser_navigate",
        {"url": "http://127.0.0.1:5173/"},
        None,
    )
    assert result.behavior == "allow"


# --- playwright URL allowlist ---


@pytest.mark.anyio
async def test_permission_callback_denies_playwright_file_url(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=False,
        allow_playwright=True,
        frontend_port=5173,
    )
    result = await callback(
        "mcp__playwright__browser_navigate",
        {"url": "file:///Users/yokumi/.ssh/id_rsa"},
        None,
    )
    assert result.behavior == "deny"


@pytest.mark.anyio
async def test_permission_callback_denies_playwright_metadata_ip(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=False,
        allow_playwright=True,
        frontend_port=5173,
    )
    result = await callback(
        "mcp__playwright__browser_navigate",
        {"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"},
        None,
    )
    assert result.behavior == "deny"


@pytest.mark.anyio
async def test_permission_callback_denies_playwright_other_localhost_port(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=False,
        allow_playwright=True,
        frontend_port=5173,
    )
    result = await callback(
        "mcp__playwright__browser_navigate",
        {"url": "http://localhost:9090/"},
        None,
    )
    assert result.behavior == "deny"


@pytest.mark.anyio
async def test_permission_callback_allows_playwright_loopback_with_correct_port(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=False,
        allow_playwright=True,
        frontend_port=4173,
    )
    result = await callback(
        "mcp__playwright__browser_navigate",
        {"url": "http://localhost:4173/dashboard"},
        None,
    )
    assert result.behavior == "allow"


# --- git subcommand allowlist ---


@pytest.mark.anyio
async def test_permission_callback_denies_git_push(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
    )
    result = await callback("Bash", {"command": "git push origin main"}, None)
    assert result.behavior == "deny"
    assert "git" in result.message.lower()


@pytest.mark.anyio
async def test_permission_callback_denies_git_config_global(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
    )
    result = await callback("Bash", {"command": "git config --global user.email evil"}, None)
    assert result.behavior == "deny"


@pytest.mark.anyio
async def test_permission_callback_denies_git_remote_add(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
    )
    result = await callback("Bash", {"command": "git remote add x http://attacker/r"}, None)
    assert result.behavior == "deny"


@pytest.mark.anyio
async def test_permission_callback_allows_git_status(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
    )
    result = await callback("Bash", {"command": "git status"}, None)
    assert result.behavior == "allow"


@pytest.mark.anyio
async def test_permission_callback_denies_git_flag_before_subcommand(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
    )
    # `git -c http.extraheader='X: x' status` would smuggle credentials.
    result = await callback("Bash", {"command": "git -c http.extraheader=evil status"}, None)
    assert result.behavior == "deny"


# --- find -exec / -delete ---


@pytest.mark.anyio
async def test_permission_callback_denies_find_exec(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
    )
    result = await callback(
        "Bash",
        {"command": "find . -name *.py -exec node payload.js {} +"},
        None,
    )
    assert result.behavior == "deny"
    assert "find" in result.message.lower() or "exec" in result.message.lower()


@pytest.mark.anyio
async def test_permission_callback_denies_find_delete(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
    )
    result = await callback("Bash", {"command": "find . -delete"}, None)
    assert result.behavior == "deny"


@pytest.mark.anyio
async def test_permission_callback_allows_plain_find(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
    )
    result = await callback("Bash", {"command": "find frontend -name *.tsx"}, None)
    assert result.behavior == "allow"


@pytest.mark.anyio
async def test_permission_callback_applies_minimal_path_policy(tmp_path: Path):
    class Policy:
        def check(self, tool_name, tool_input):
            assert tool_name == "Edit"
            assert tool_input["file_path"].endswith("frontend/App.jsx")
            return "outside the harness change cone"

    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=False,
        allow_playwright=False,
        mutation_policy=Policy(),
    )
    result = await callback(
        "Edit",
        {
            "file_path": str(tmp_path / "frontend" / "App.jsx"),
            "old_string": "old",
            "new_string": "new",
        },
        None,
    )

    assert result.behavior == "deny"
    assert "change cone" in result.message


def test_build_agent_options_includes_playwright_server(tmp_path: Path):
    options = build_agent_options(
        config=HarnessConfig(playwright_headless=True),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=10,
        allow_bash=False,
        allow_playwright=True,
    )
    assert options.cwd == tmp_path
    assert options.system_prompt["preset"] == "claude_code"
    assert options.mcp_servers["playwright"]["args"] == [
        "@playwright/mcp@latest",
        "--isolated",
        "--headless",
    ]


def test_build_agent_options_sets_allowed_tools(tmp_path: Path):
    options = build_agent_options(
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=10,
        allow_bash=False,
    )
    assert "Write" in options.allowed_tools
    assert "Bash" not in options.allowed_tools


def test_build_agent_options_uses_plain_system_prompt_for_qwen(tmp_path: Path):
    options = build_agent_options(
        config=HarnessConfig(),
        workdir=tmp_path,
        model="Qwen3-235B-A22B",
        system_prompt="harness system",
        max_turns=10,
        allow_bash=True,
    )

    assert options.system_prompt == "harness system"


def test_build_agent_options_sets_sdk_buffer_size(tmp_path: Path):
    options = build_agent_options(
        config=HarnessConfig(sdk_max_buffer_size=6 * 1024 * 1024),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=10,
        allow_bash=False,
    )
    assert options.max_buffer_size == 6 * 1024 * 1024


def test_build_agent_options_sets_default_claude_code_env(tmp_path: Path):
    options = build_agent_options(
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=10,
        allow_bash=False,
    )
    for key, value in _DEFAULT_CLAUDE_CODE_ENV.items():
        assert options.env[key] == value


def test_build_agent_options_adds_keepalive_hook_for_permission_callback(tmp_path: Path):
    options = build_agent_options(
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=10,
        allow_bash=False,
    )
    assert options.hooks is not None
    assert "Stop" in options.hooks
    assert len(options.hooks["Stop"]) == 1


def test_build_agent_options_appends_custom_stop_hook(tmp_path: Path):
    async def custom_stop_hook(_input, _tool_use_id, _context):
        return {"continue_": True}

    options = build_agent_options(
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=10,
        allow_bash=False,
        stop_hooks=[custom_stop_hook],
    )
    assert options.hooks is not None
    stop_matchers = options.hooks["Stop"]
    assert len(stop_matchers) == 1
    assert stop_matchers[0].hooks[0].__name__ == "_keepalive_hook"
    assert stop_matchers[0].hooks[1] is custom_stop_hook


def test_build_agent_options_wires_stderr_callback_into_trace(tmp_path: Path):
    trace_path = tmp_path / "trace.jsonl"
    writer = SdkTraceWriter(trace_path)
    options = build_agent_options(
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=10,
        allow_bash=False,
        trace_writer=writer,
    )
    assert options.stderr is not None
    options.stderr("cli stderr line")
    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert records[-1]["event"] == "sdk_stderr"
    assert records[-1]["line"] == "cli stderr line"


@pytest.mark.anyio
async def test_build_agent_options_reports_sdk_tool_results_to_controller(tmp_path: Path):
    class Policy:
        def __init__(self):
            self.results = []

        def check(self, tool_name, tool_input):
            return None

        def observe_result(self, tool_name, tool_input, *, ok, output):
            self.results.append((tool_name, tool_input, ok, output))

    policy = Policy()
    options = build_agent_options(
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=10,
        allow_bash=True,
        mutation_policy=policy,
    )

    post = options.hooks["PostToolUse"][0].hooks[0]
    failure = options.hooks["PostToolUseFailure"][0].hooks[0]
    await post(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(tmp_path / "frontend" / "App.jsx")},
            "tool_response": "source",
            "tool_use_id": "read-1",
        },
        "read-1",
        {},
    )
    await failure(
        {
            "hook_event_name": "PostToolUseFailure",
            "tool_name": "Bash",
            "tool_input": {"command": "npm run build"},
            "error": "build failed",
            "tool_use_id": "build-1",
        },
        "build-1",
        {},
    )

    assert [(item[0], item[2]) for item in policy.results] == [
        ("Read", True),
        ("Bash", False),
    ]


@pytest.mark.anyio
async def test_capture_claude_http_traffic_records_streaming_response(tmp_path: Path):
    async def handle_messages(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        assert body["stream"] is True
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        chunks = [
            (
                'event: message_start\n'
                'data: {"type":"message_start","message":{"id":"m1","type":"message","role":"assistant","model":"claude-sonnet-4-6","content":[]}}\n\n'
            ),
            (
                'event: content_block_delta\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n'
            ),
            (
                'event: message_delta\n'
                'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}\n\n'
            ),
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]
        for chunk in chunks:
            await response.write(chunk.encode("utf-8"))
        await response.write_eof()
        return response

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/messages", handle_messages)
    upstream_runner = web.AppRunner(upstream_app)
    await upstream_runner.setup()
    upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
    await upstream_site.start()
    upstream_port = upstream_site._server.sockets[0].getsockname()[1]
    upstream_url = f"http://127.0.0.1:{upstream_port}"

    trace_path = tmp_path / "http_trace.jsonl"
    try:
        async with capture_claude_http_traffic(
            trace_path=trace_path,
            target_url=upstream_url,
        ) as proxy:
            assert proxy is not None
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy.base_url}/v1/messages",
                    headers={
                        "Authorization": "Bearer secret-token",
                        "x-api-key": "secret-api-key",
                    },
                    json={
                        "model": "claude-sonnet-4-6",
                        "stream": True,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                ) as response:
                    text = await response.text()

            assert "message_start" in text
            assert "content_block_delta" in text
    finally:
        await upstream_runner.cleanup()

    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["request"]["path"] == "/v1/messages"
    assert record["request"]["body"]["messages"][0]["content"] == "ping"
    assert record["request"]["headers"]["Authorization"] == "Bearer secre..."
    assert record["request"]["headers"]["x-api-key"] == "secret-api-k..."
    assert record["response"]["status"] == 200
    assert [event["event"] for event in record["response"]["sse_events"]] == [
        "message_start",
        "content_block_delta",
        "message_delta",
        "message_stop",
    ]
    assert record["upstream_base_url"] == upstream_url


@pytest.mark.anyio
async def test_capture_claude_http_traffic_rejects_unknown_paths(tmp_path: Path):
    trace_path = tmp_path / "http_trace.jsonl"
    async with capture_claude_http_traffic(
        trace_path=trace_path,
        target_url="http://127.0.0.1:9",
    ) as proxy:
        assert proxy is not None
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{proxy.base_url}/metrics") as response:
                assert response.status == 404
                assert await response.text() == "Not Found"

    assert trace_path.read_text(encoding="utf-8") == ""


def test_generate_claude_http_trace_html_renders_core_debug_fields(tmp_path: Path):
    trace_path = tmp_path / "planner.http.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "turn": 2,
                "duration_ms": 123,
                "request": {
                    "method": "POST",
                    "path": "/v1/messages",
                    "headers": {"authorization": "***"},
                    "body": {
                        "model": "claude-sonnet-4-6",
                        "messages": [
                            {"role": "user", "content": "Build the dashboard"}
                        ],
                    },
                },
                "response": {
                    "status": 200,
                    "body": {
                        "content": [
                            {"type": "text", "text": "Here is the plan"},
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "input": {"file_path": "frontend/src/App.tsx"},
                            },
                            {"type": "thinking", "thinking": "Check layout first"},
                        ],
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 20,
                            "cache_read_input_tokens": 3,
                            "cache_creation_input_tokens": 4,
                        },
                    },
                    "sse_events": [
                        {"event": "message_start", "data": {"type": "message_start"}}
                    ],
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    html_path = generate_claude_http_trace_html(trace_path)

    assert html_path == tmp_path / "planner.http.html"
    html = html_path.read_text(encoding="utf-8")
    assert "Claude HTTP Trace Viewer" in html
    assert "sidebar" in html
    assert "path-filter" in html
    assert "theme-toggle" in html
    assert "EMBEDDED_TRACE_DATA" in html
    assert '"turn": 2' in html
    assert "claude-sonnet-4-6" in html
    assert '"status": 200' in html
    assert '"duration_ms": 123' in html
    assert '"input_tokens": 10' in html
    assert '"output_tokens": 20' in html
    assert "Build the dashboard" in html
    assert "Here is the plan" in html
    assert "Write" in html
    assert "frontend/src/App.tsx" in html
    assert "Check layout first" in html
    assert "message_start" in html
    assert "renderJSONTree" in html
    assert "renderMessages" in html


def test_generate_claude_http_trace_html_escapes_trace_content(tmp_path: Path):
    trace_path = tmp_path / "generator.http.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "turn": 1,
                "duration_ms": 1,
                "request": {
                    "body": {
                        "model": "claude-sonnet-4-6",
                        "messages": [
                            {
                                "role": "user",
                                "content": '<script>alert("x")</script><b>bold</b>',
                            }
                        ],
                    }
                },
                "response": {
                    "status": 200,
                    "body": {
                        "content": [
                            {
                                "type": "text",
                                "text": '<img src=x onerror="alert(1)">',
                            }
                        ],
                    },
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    html_path = generate_claude_http_trace_html(trace_path)
    html = html_path.read_text(encoding="utf-8")

    assert '<script>alert("x")</script>' not in html
    assert '<\\/script>' in html
    assert '<img src=x onerror=\\"alert(1)\\">' in html
    assert "EMBEDDED_TRACE_DATA" in html


def test_build_agent_run_stats_extracts_usage_and_serializes_wall_time():
    stats = build_agent_run_stats(
        ResultMessage(
            subtype="result",
            duration_ms=1250,
            duration_api_ms=900,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=1.5,
            usage={
                "input_tokens": 1200,
                "output_tokens": 340,
                "cache_read_input_tokens": 75,
            },
            model_usage={"cache_creation_input_tokens": 33},
        ),
        model="claude-sonnet-4-6",
    ).with_wall_duration(1800)

    # Cost is now computed from the local pricing table — Claude
    # Sonnet 4-6 at $3 / $15 / $0.30 / $3.75 per 1M tokens.
    expected_cost = round(
        (1200 * 3.0 + 340 * 15.0 + 75 * 0.30 + 33 * 3.75) / 1_000_000.0,
        6,
    )
    assert stats == AgentRunStats(
        cost_usd=expected_cost,
        duration_ms=1250,
        duration_api_ms=900,
        token_usage={
            "input_tokens": 1200,
            "output_tokens": 340,
            "cache_read_input_tokens": 75,
            "cache_creation_input_tokens": 33,
        },
        usage={
            "input_tokens": 1200,
            "output_tokens": 340,
            "cache_read_input_tokens": 75,
        },
        model_usage={"cache_creation_input_tokens": 33},
        wall_duration_ms=1800,
    )
    assert stats.to_dict()["wall_duration_ms"] == 1800


def test_build_agent_run_stats_ignores_sdk_total_cost_usd():
    """The CLI's ``total_cost_usd`` is intentionally ignored even when
    set — the local pricing table is the single source of truth so a
    GLM/OpenAI proxy can't bypass the budget gate by reporting 0."""
    stats = build_agent_run_stats(
        ResultMessage(
            subtype="result",
            duration_ms=10,
            duration_api_ms=10,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=999.0,  # CLI claims a huge cost
            usage={"input_tokens": 100, "output_tokens": 100},
        ),
        model="claude-sonnet-4-6",
    )
    expected = round((100 * 3.0 + 100 * 15.0) / 1_000_000.0, 6)
    assert stats.cost_usd == expected
    assert stats.cost_usd != 999.0


@pytest.mark.anyio
async def test_run_sdk_agent_returns_cost(monkeypatch, tmp_path: Path):
    async def fake_query(*, prompt, options):
        messages = [message async for message in prompt]
        del options
        assert os.environ.get(_CLAUDE_SDK_SKIP_VERSION_CHECK) == "1"
        assert messages[0]["type"] == "user"
        assert messages[0]["message"]["content"] == "hello"
        yield AssistantMessage(
            content=[TextBlock(text="assistant text")],
            model="glm-5.1",
        )
        yield ResultMessage(
            subtype="result",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            # Deliberately a huge cost reported by the CLI; the harness
            # must compute cost from the local pricing table instead.
            total_cost_usd=1.5,
            usage={"input_tokens": 1_000_000},
            result="done",
        )

    monkeypatch.setattr("src.agents.sdk_runner.query", fake_query)

    trace_path = tmp_path / "trace.jsonl"
    result, cost, assistant_text, permission_denials = await run_sdk_agent(
        prompt="hello",
        config=HarnessConfig(base_url=""),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=5,
        allow_bash=False,
        trace_path=trace_path,
    )

    assert result.result == "done"
    expected_cost = estimate_cost_usd("glm-5.1", {"input_tokens": 1_000_000})
    # The SDK's total_cost_usd (1.5) is intentionally ignored so a proxy
    # to a non-Claude backend can't bypass the budget gate.
    assert cost == expected_cost
    assert cost != 1.5
    assert assistant_text == "assistant text"
    assert permission_denials == []
    lines = trace_path.read_text().strip().splitlines()
    assert any(json.loads(line)["event"] == "run_start" for line in lines)
    assert any(json.loads(line)["event"] == "sdk_message" for line in lines)
    assert any(json.loads(line)["event"] == "run_complete" for line in lines)
    assert os.environ.get(_CLAUDE_SDK_SKIP_VERSION_CHECK) is None


@pytest.mark.anyio
async def test_run_sdk_agent_routes_base_url_through_http_trace_proxy(
    monkeypatch,
    tmp_path: Path,
):
    captured: dict[str, object] = {}

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_capture_claude_http_traffic(*, trace_path, target_url):
        captured["trace_path"] = trace_path
        captured["target_url"] = target_url
        yield SimpleNamespace(base_url="http://127.0.0.1:43123")

    async def fake_query(*, prompt, options):
        messages = [message async for message in prompt]
        assert messages[0]["message"]["content"] == "hello"
        assert options.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:43123"
        yield ResultMessage(
            subtype="result",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=0.0,
            result="done",
        )

    monkeypatch.setattr(
        "src.agents.sdk_runner.capture_claude_http_traffic",
        fake_capture_claude_http_traffic,
    )
    monkeypatch.setattr("src.agents.sdk_runner.query", fake_query)

    trace_path = tmp_path / "trace.jsonl"
    result, cost, assistant_text, permission_denials = await run_sdk_agent(
        prompt="hello",
        config=HarnessConfig(base_url=""),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=5,
        allow_bash=False,
        trace_path=trace_path,
    )

    assert result.result == "done"
    assert cost == 0.0
    assert assistant_text == ""
    assert permission_denials == []
    assert captured["trace_path"] == tmp_path / "trace.http.jsonl"
    assert captured["target_url"] == DEFAULT_ANTHROPIC_BASE_URL

    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    run_start = next(record for record in records if record["event"] == "run_start")
    assert run_start["http_trace_path"] == str(tmp_path / "trace.http.jsonl")
    assert run_start["upstream_base_url"] == DEFAULT_ANTHROPIC_BASE_URL
    assert run_start["proxy_base_url"] == "http://127.0.0.1:43123"
    assert (tmp_path / "trace.http.html").exists()
    html_event = next(
        record for record in records if record["event"] == "http_trace_html_generated"
    )
    assert html_event["http_trace_path"] == str(tmp_path / "trace.http.jsonl")
    assert html_event["http_trace_html_path"] == str(tmp_path / "trace.http.html")


@pytest.mark.anyio
async def test_run_sdk_agent_warns_when_http_trace_html_generation_fails(
    monkeypatch,
    caplog,
    tmp_path: Path,
):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_capture_claude_http_traffic(*, trace_path, target_url):
        del target_url
        assert trace_path == tmp_path / "trace.http.jsonl"
        yield SimpleNamespace(base_url="http://127.0.0.1:43123")

    async def fake_query(*, prompt, options):
        _ = [message async for message in prompt]
        assert options.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:43123"
        yield ResultMessage(
            subtype="result",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=0.0,
            result="done",
        )

    def fail_generate(_trace_path):
        raise RuntimeError("html boom")

    monkeypatch.setattr(
        "src.agents.sdk_runner.capture_claude_http_traffic",
        fake_capture_claude_http_traffic,
    )
    monkeypatch.setattr("src.agents.sdk_runner.query", fake_query)
    monkeypatch.setattr(
        "src.agents.sdk_runner.generate_claude_http_trace_html",
        fail_generate,
    )

    trace_path = tmp_path / "trace.jsonl"
    with caplog.at_level("WARNING"):
        result, cost, assistant_text, permission_denials = await run_sdk_agent(
            prompt="hello",
            config=HarnessConfig(base_url=""),
            workdir=tmp_path,
            model="glm-5.1",
            system_prompt="system",
            max_turns=5,
            allow_bash=False,
            trace_path=trace_path,
        )

    assert result.result == "done"
    assert cost == 0.0
    assert assistant_text == ""
    assert permission_denials == []
    assert "failed to generate Claude HTTP trace HTML: html boom" in caplog.text


@pytest.mark.anyio
async def test_run_sdk_agent_returns_permission_denials_without_failing(monkeypatch, tmp_path: Path):
    async def fake_query(*, prompt, options):
        _ = [message async for message in prompt]
        del options
        yield ResultMessage(
            subtype="result",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=0.0,
            result="done",
            permission_denials=["Write denied"],
        )

    monkeypatch.setattr("src.agents.sdk_runner.query", fake_query)

    result, cost, assistant_text, permission_denials = await run_sdk_agent(
        prompt="hello",
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=5,
        allow_bash=False,
    )
    assert result.result == "done"
    assert cost == 0.0
    assert assistant_text == ""
    assert permission_denials == ["Write denied"]


@pytest.mark.anyio
async def test_run_sdk_agent_stops_after_result_message_even_if_stream_hangs(
    monkeypatch,
    tmp_path: Path,
):
    closed = {"value": False}

    async def fake_query(*, prompt, options):
        _ = [message async for message in prompt]
        del options
        try:
            yield ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.0,
                result="done",
            )
            await asyncio.Event().wait()
        finally:
            closed["value"] = True

    monkeypatch.setattr("src.agents.sdk_runner.query", fake_query)

    result, cost, assistant_text, permission_denials = await asyncio.wait_for(
        run_sdk_agent(
            prompt="hello",
            config=HarnessConfig(),
            workdir=tmp_path,
            model="glm-5.1",
            system_prompt="system",
            max_turns=5,
            allow_bash=False,
        ),
        timeout=1.0,
    )

    assert result.result == "done"
    assert cost == 0.0
    assert assistant_text == ""
    assert permission_denials == []
    assert closed["value"] is True


@pytest.mark.anyio
async def test_run_sdk_agent_isolates_cancellation_during_stream_close(
    monkeypatch,
    tmp_path: Path,
):
    class FakeStream:
        def __init__(self) -> None:
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.0,
                result="done",
            )

        async def aclose(self):
            # Simulate an anyio-backed shutdown path that cancels the task
            # performing stream close. The parent harness task must not retain
            # that cancellation after `run_sdk_agent()` returns.
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)

    def fake_query(*, prompt, options):
        del prompt, options
        return FakeStream()

    monkeypatch.setattr("src.agents.sdk_runner.query", fake_query)

    result, cost, assistant_text, permission_denials = await run_sdk_agent(
        prompt="hello",
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=5,
        allow_bash=False,
    )

    assert result.result == "done"
    assert cost == 0.0
    assert assistant_text == ""
    assert permission_denials == []
    # If the parent task were still marked cancelled, this await would raise.
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_run_sdk_agent_clears_delayed_parent_cancellation_after_success(
    monkeypatch,
    tmp_path: Path,
):
    parent_task: asyncio.Task | None = None

    async def fake_query(*, prompt, options):
        nonlocal parent_task
        _ = [message async for message in prompt]
        del options
        parent_task = asyncio.current_task()
        try:
            yield ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.0,
                result="done",
            )
        finally:
            assert parent_task is not None
            asyncio.get_running_loop().call_soon(parent_task.cancel)

    monkeypatch.setattr("src.agents.sdk_runner.query", fake_query)

    result, cost, assistant_text, permission_denials = await run_sdk_agent(
        prompt="hello",
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=5,
        allow_bash=False,
    )

    assert result.result == "done"
    assert cost == 0.0
    assert assistant_text == ""
    assert permission_denials == []
    # The delayed parent-task cancellation fired after the stream had already
    # produced its result. The SDK boundary must clear it before returning.
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_run_sdk_agent_waits_for_stream_close_despite_parent_cancellation(
    monkeypatch,
    tmp_path: Path,
):
    parent_task: asyncio.Task | None = None
    close_finished = {"value": False}

    class FakeStream:
        def __init__(self) -> None:
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            nonlocal parent_task
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            parent_task = asyncio.current_task()
            return ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.0,
                result="done",
            )

        async def aclose(self):
            assert parent_task is not None
            parent_task.cancel()
            await asyncio.sleep(0)
            close_finished["value"] = True

    def fake_query(*, prompt, options):
        del prompt, options
        return FakeStream()

    monkeypatch.setattr("src.agents.sdk_runner.query", fake_query)

    result, cost, assistant_text, permission_denials = await run_sdk_agent(
        prompt="hello",
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=5,
        allow_bash=False,
    )

    assert result.result == "done"
    assert cost == 0.0
    assert assistant_text == ""
    assert permission_denials == []
    assert close_finished["value"] is True
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_run_sdk_agent_survives_stale_parent_cancellation_before_start(
    monkeypatch,
    tmp_path: Path,
):
    async def fake_query(*, prompt, options):
        _ = [message async for message in prompt]
        del options
        yield ResultMessage(
            subtype="result",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=0.0,
            result="done",
        )

    monkeypatch.setattr("src.agents.sdk_runner.query", fake_query)

    parent_task = asyncio.current_task()
    assert parent_task is not None
    parent_task.cancel()

    result, cost, assistant_text, permission_denials = await run_sdk_agent(
        prompt="hello",
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=5,
        allow_bash=False,
    )

    assert result.result == "done"
    assert cost == 0.0
    assert assistant_text == ""
    assert permission_denials == []
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_permission_trace_is_written(tmp_path: Path):
    trace_path = tmp_path / "permissions.jsonl"
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
        trace_writer=SdkTraceWriter(trace_path),
    )
    result = await callback("Bash", {"command": "pwd"}, None)
    assert result.behavior == "allow"
    records = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert records[-1]["event"] == "permission_check"
    assert records[-1]["tool_name"] == "Bash"
    assert records[-1]["decision"] == "allow"


# --- read-only Bash profile (evaluator) ---


@pytest.mark.anyio
async def test_readonly_bash_denies_file_mutation(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        bash_profile="read_only",
        allow_playwright=False,
    )
    for cmd in (
        "cp src/a.py src/b.py",
        "mv a b",
        "touch x",
        "mkdir foo",
        "sed -i s/a/b/ file",
    ):
        result = await callback("Bash", {"command": cmd}, None)
        assert result.behavior == "deny", f"expected deny for {cmd!r}"


@pytest.mark.anyio
async def test_readonly_bash_denies_inline_interpreter_code(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        bash_profile="read_only",
        allow_playwright=False,
    )
    for cmd in (
        'python3 -c "print(1)"',
        'python -c "print(1)"',
        'node -e "console.log(1)"',
    ):
        result = await callback("Bash", {"command": cmd}, None)
        assert result.behavior == "deny", f"expected deny for {cmd!r}"


@pytest.mark.anyio
async def test_readonly_bash_denies_git_mutators(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        bash_profile="read_only",
        allow_playwright=False,
    )
    for cmd in (
        "git add file",
        'git commit -m "x"',
        "git stash",
    ):
        result = await callback("Bash", {"command": cmd}, None)
        assert result.behavior == "deny", f"expected deny for {cmd!r}"


@pytest.mark.anyio
async def test_readonly_bash_denies_package_manager_writes(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        bash_profile="read_only",
        allow_playwright=False,
    )
    for cmd in (
        "npm install",
        "npm test",
        "pnpm add react",
        "yarn build",
        "npx vite build",
        "pytest",
        "vite",
    ):
        result = await callback("Bash", {"command": cmd}, None)
        assert result.behavior == "deny", f"expected deny for {cmd!r}"


@pytest.mark.anyio
async def test_readonly_bash_allows_inspection_commands(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        bash_profile="read_only",
        allow_playwright=False,
    )
    for cmd in (
        "cat .harness/grade_round_1.json",
        "grep -r foo frontend/src",
        "python3 -m json.tool .harness/grade_round_1.json",
        "git log --oneline -20",
        "git diff",
        "git status",
        "npm list --depth=0",
        "find frontend -name *.tsx",
        "ls .harness",
    ):
        result = await callback("Bash", {"command": cmd}, None)
        assert result.behavior == "allow", f"expected allow for {cmd!r}, got {getattr(result, 'message', None)}"


@pytest.mark.anyio
async def test_full_bash_profile_remains_default_for_generator(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=True,
        allow_playwright=False,
    )
    # Generator (default profile=full) must still be able to write code.
    result = await callback("Bash", {"command": "sed -i s/a/b/ file"}, None)
    assert result.behavior == "allow"
    result = await callback("Bash", {"command": "git add file"}, None)
    assert result.behavior == "allow"


# --- PreToolUse Bash gate ---


@pytest.mark.anyio
async def test_bash_pretool_hook_denies_disallowed_in_full_profile(tmp_path: Path):
    from src.agents.sdk_runner import make_bash_pretool_hook

    hook = make_bash_pretool_hook(bash_profile="full")
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls /etc/passwd && cat foo"},
            "tool_use_id": "x",
        },
        None,
        {},
    )
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    assert spec["hookEventName"] == "PreToolUse"
    assert "absolute paths not allowed" in spec["permissionDecisionReason"]


@pytest.mark.anyio
async def test_bash_pretool_hook_denies_background_fork_ampersand(tmp_path: Path):
    from src.agents.sdk_runner import make_bash_pretool_hook

    hook = make_bash_pretool_hook(bash_profile="full")
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {
                "command": "npx vite --host 127.0.0.1 --port 3000 &\nsleep 3\ncurl http://x"
            },
            "tool_use_id": "x",
        },
        None,
        {},
    )
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"
    # Either the bare `&` or the `\n` triggers, both must be forbidden snippets.
    assert "shell control" in spec["permissionDecisionReason"]


@pytest.mark.anyio
async def test_bash_pretool_hook_allows_clean_command(tmp_path: Path):
    from src.agents.sdk_runner import make_bash_pretool_hook

    hook = make_bash_pretool_hook(bash_profile="full")
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_use_id": "x",
        },
        None,
        {},
    )
    spec = out.get("hookSpecificOutput", {})
    # Either explicit allow or no decision (which the SDK treats as "no opinion").
    assert spec.get("permissionDecision", "allow") == "allow"


@pytest.mark.anyio
async def test_bash_pretool_hook_uses_readonly_profile_when_set(tmp_path: Path):
    from src.agents.sdk_runner import make_bash_pretool_hook

    hook = make_bash_pretool_hook(bash_profile="read_only")
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "sed -i s/a/b/ foo"},
            "tool_use_id": "x",
        },
        None,
        {},
    )
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny"


@pytest.mark.anyio
async def test_bash_pretool_hook_passes_non_bash_tools_through(tmp_path: Path):
    from src.agents.sdk_runner import make_bash_pretool_hook

    hook = make_bash_pretool_hook(bash_profile="full")
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "frontend/src/x.tsx"},
            "tool_use_id": "x",
        },
        None,
        {},
    )
    # Non-Bash tools must not be denied by the bash hook.
    spec = out.get("hookSpecificOutput", {})
    assert spec.get("permissionDecision", "allow") == "allow"


def test_disallowed_shell_snippets_includes_ampersand():
    from src.utils.bash_policy import _DISALLOWED_SHELL_SNIPPETS

    # Background-fork `&` must be in the deny set so `npx vite ... &` is
    # rejected by both the can_use_tool path and the PreToolUse hook.
    assert "&" in _DISALLOWED_SHELL_SNIPPETS


def test_build_agent_options_installs_pretooluse_bash_hook(tmp_path: Path):
    """Generator agents must have a PreToolUse hook on Bash; the previous
    setup gated only via can_use_tool, which the CLI bypasses for tools in
    --allowedTools (Bash was always in there for the generator, so the
    validator never ran)."""
    from src.agents.sdk_runner import build_agent_options

    options = build_agent_options(
        config=HarnessConfig(),
        workdir=tmp_path,
        model="claude-sonnet-4-6",
        system_prompt="x",
        max_turns=1,
        allow_bash=True,
    )
    pretool = options.hooks.get("PreToolUse") or []
    assert pretool, "expected a PreToolUse hook list"
    matchers = [m.matcher for m in pretool]
    assert "Bash" in matchers
