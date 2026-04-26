from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

from src.agents.sdk_runner import (
    AgentRunStats,
    SdkTraceWriter,
    build_agent_run_stats,
    build_agent_options,
    make_tool_permission_callback,
    run_sdk_agent,
)
from src.config import HarnessConfig


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
    )
    result = await callback("Bash", {"command": "pwd && ls"}, None)
    assert result.behavior == "deny"
    assert "shell control operator not allowed" in result.message


@pytest.mark.anyio
async def test_permission_callback_allows_playwright_mcp(tmp_path: Path):
    callback = make_tool_permission_callback(
        workdir=tmp_path,
        allow_bash=False,
        allow_playwright=True,
    )
    result = await callback("mcp__playwright__browser_navigate", {"url": "http://localhost"}, None)
    assert result.behavior == "allow"


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
        )
    ).with_wall_duration(1800)

    assert stats == AgentRunStats(
        cost_usd=1.5,
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


@pytest.mark.anyio
async def test_run_sdk_agent_returns_cost(monkeypatch, tmp_path: Path):
    async def fake_query(*, prompt, options):
        messages = [message async for message in prompt]
        del options
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
            total_cost_usd=1.5,
            result="done",
        )

    monkeypatch.setattr("src.agents.sdk_runner.query", fake_query)

    trace_path = tmp_path / "trace.jsonl"
    result, cost, assistant_text, permission_denials = await run_sdk_agent(
        prompt="hello",
        config=HarnessConfig(),
        workdir=tmp_path,
        model="glm-5.1",
        system_prompt="system",
        max_turns=5,
        allow_bash=False,
        trace_path=trace_path,
    )

    assert result.result == "done"
    assert cost == 1.5
    assert assistant_text == "assistant text"
    assert permission_denials == []
    lines = trace_path.read_text().strip().splitlines()
    assert any(json.loads(line)["event"] == "run_start" for line in lines)
    assert any(json.loads(line)["event"] == "sdk_message" for line in lines)
    assert any(json.loads(line)["event"] == "run_complete" for line in lines)


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
