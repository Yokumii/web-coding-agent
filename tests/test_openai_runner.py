import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents.openai_runner import (
    EvaluationToolPolicy,
    OpenAIHTTPClient,
    OpenAIRunLimits,
    _compact_messages,
    run_openai_agent,
)
from src.config import HarnessConfig


class FakeClient:
    def __init__(self, replies):
        self.replies = iter(replies)

    async def complete(self, **kwargs):
        return next(self.replies)


class CapturingFakeClient(FakeClient):
    def __init__(self, replies):
        super().__init__(replies)
        self.requests = []

    async def complete(self, **kwargs):
        self.requests.append(kwargs)
        return await super().complete(**kwargs)


def reply(*, content="", tool_calls=None):
    return {
        "choices": [{"message": {"role": "assistant", "content": content, "tool_calls": tool_calls or []}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }


def test_old_completed_turns_are_compacted_but_recent_turns_remain():
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for index in range(12):
        messages.extend([
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": str(index), "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{index}"})},
            }]},
            {"role": "tool", "tool_call_id": str(index), "content": "content " * 100},
        ])
    compacted = _compact_messages(messages, recent=8)
    assert len(compacted) < len(messages)
    assert "Earlier completed work" in compacted[2]["content"]
    assert compacted[-1] == messages[-1]


def test_browser_screenshot_schema_exposes_distinct_page_positions():
    from src.agents.openai_tools import openai_tool_schemas

    screenshot = next(
        item["function"] for item in openai_tool_schemas(allow_bash=False, allow_playwright=True)
        if item["function"]["name"] == "browser_screenshot"
    )

    assert screenshot["parameters"]["properties"]["position"]["enum"] == ["top", "middle", "bottom"]


@pytest.mark.anyio
async def test_openai_http_client_retries_a_transient_transport_error(monkeypatch):
    import httpx

    attempts = 0

    class Response:
        is_error = False
        status_code = 200
        text = ""

        def json(self):
            return {"choices": []}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("transient proxy failure")
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    async def no_sleep(_seconds):
        return None
    monkeypatch.setattr("src.agents.openai_runner.asyncio.sleep", no_sleep)
    config = HarnessConfig(openai_base_url="https://example.test/v1", openai_api_key="test-key")

    assert await OpenAIHTTPClient(config, 5).complete(model="qwen-test", messages=[]) == {"choices": []}
    assert attempts == 2


@pytest.mark.anyio
async def test_native_loop_executes_tool_and_returns_compatible_result(tmp_path: Path):
    client = FakeClient([
        reply(tool_calls=[{"id": "1", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"x.txt","content":"ok"}'}}]),
        reply(content="done"),
    ])
    result, _, text, denials = await run_openai_agent(
        prompt="build", config=HarnessConfig(), workdir=tmp_path, model="deepseek-chat",
        system_prompt="system", max_turns=10, allow_bash=False, client=client,
    )
    assert tmp_path.joinpath("x.txt").read_text() == "ok"
    assert text == "done" and not denials
    assert result.usage["input_tokens"] == 6


@pytest.mark.anyio
async def test_turn_limit_grants_one_finalization_window(tmp_path: Path):
    client = CapturingFakeClient([
        reply(tool_calls=[{
            "id": "1",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": '{"path":"x.txt","content":"ok"}',
            },
        }]),
        reply(content="done"),
    ])

    result, _, text, _ = await run_openai_agent(
        prompt="build", config=HarnessConfig(), workdir=tmp_path,
        model="deepseek-chat", system_prompt="system", max_turns=1,
        allow_bash=False, client=client,
    )

    assert text == "done"
    assert result.usage["input_tokens"] == 6
    final_messages = client.requests[1]["messages"]
    assert any(
        message.get("role") == "user"
        and "FINAL CHANCE" in message.get("content", "")
        for message in final_messages
    )


@pytest.mark.anyio
async def test_repeated_identical_tool_call_is_stopped(tmp_path: Path):
    call = [{"id": "1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"missing"}'}}]
    client = FakeClient([reply(tool_calls=call), reply(tool_calls=call), reply(tool_calls=call)])
    with pytest.raises(RuntimeError, match="repeated identical tool call"):
        await run_openai_agent(
            prompt="build", config=HarnessConfig(), workdir=tmp_path, model="deepseek-chat",
            system_prompt="system", max_turns=10, allow_bash=False, client=client,
            limits=OpenAIRunLimits(repeat_limit=3),
        )


@pytest.mark.anyio
async def test_phase_timeout_is_hard(tmp_path: Path):
    class SlowClient:
        async def complete(self, **kwargs):
            import asyncio
            await asyncio.sleep(60)

    with pytest.raises(RuntimeError, match="phase timed out"):
        await run_openai_agent(
            prompt="build", config=HarnessConfig(), workdir=tmp_path, model="deepseek-chat",
            system_prompt="system", max_turns=10, allow_bash=False, client=SlowClient(),
            limits=OpenAIRunLimits(phase_timeout=0.02, request_timeout=60),
        )


def test_evaluation_policy_enters_finalization_after_browser_diagnostic_budget():
    policy = EvaluationToolPolicy(exploration_limit=20, browser_evaluate_limit=2)

    assert policy.check("browser_evaluate") is None
    assert policy.check("browser_evaluate") is None
    denial = policy.check("browser_evaluate")

    assert "diagnostic budget" in denial
    assert policy.finalizing is True


def test_evaluation_policy_only_allows_artifacts_after_exploration_budget():
    policy = EvaluationToolPolicy(exploration_limit=1, browser_evaluate_limit=10)

    assert policy.check("browser_click") is None
    assert "exploration budget" in policy.check("browser_snapshot")
    assert policy.check("browser_screenshot") is None
    assert policy.check("write_file") is None
    assert policy.check("apply_patch") is None
    assert "finalization mode" in policy.check("browser_click")


@pytest.mark.anyio
async def test_native_evaluator_enforces_diagnostic_budget_before_tool_execution(
    monkeypatch, tmp_path: Path
):
    executed: list[str] = []

    class FakeTools:
        def __init__(self, **kwargs):
            pass

        async def execute(self, name, args):
            executed.append(name)
            return SimpleNamespace(ok=True, output="ok", changed=False)

        async def close(self):
            pass

    monkeypatch.setattr("src.agents.openai_runner.OpenAIToolExecutor", FakeTools)
    evaluate = lambda call_id: [{
        "id": call_id,
        "type": "function",
        "function": {"name": "browser_evaluate", "arguments": '{"expression":"state' + call_id + '"}'},
    }]
    write = [{
        "id": "write",
        "type": "function",
        "function": {"name": "write_file", "arguments": '{"path":"grade.json","content":"{}"}'},
    }]
    client = FakeClient([
        reply(tool_calls=evaluate("1")),
        reply(tool_calls=evaluate("2")),
        reply(tool_calls=evaluate("3")),
        reply(tool_calls=write),
        reply(content="done"),
    ])

    await run_openai_agent(
        prompt="evaluate", config=HarnessConfig(), workdir=tmp_path,
        model="deepseek-chat", system_prompt="system", max_turns=10,
        allow_bash=False, allow_playwright=True, client=client,
        limits=OpenAIRunLimits(
            evaluation_exploration_limit=20,
            evaluation_browser_evaluate_limit=2,
        ),
    )

    assert executed == ["browser_evaluate", "browser_evaluate", "write_file"]
