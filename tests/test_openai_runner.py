import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents.openai_runner import (
    EvaluationToolPolicy,
    OpenAIHTTPClient,
    OpenAIRunLimits,
    ResponsesStreamReadError,
    _is_finalization_command,
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


def test_finalization_command_allowlist_rejects_exploration():
    assert _is_finalization_command("git add --all")
    assert _is_finalization_command("git commit -m 'finish'")
    assert not _is_finalization_command("rg -n TODO .")


def test_browser_screenshot_schema_exposes_distinct_page_positions():
    from src.agents.openai_tools import openai_tool_schemas

    screenshot = next(
        item["function"] for item in openai_tool_schemas(allow_bash=False, allow_playwright=True)
        if item["function"]["name"] == "browser_screenshot"
    )

    assert screenshot["parameters"]["properties"]["position"]["enum"] == ["top", "middle", "bottom"]


@pytest.mark.anyio
@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize("qwen", [True, False])
async def test_tokenwave_stream_preserves_text_tools_usage_and_rejects_truncation(monkeypatch, complete, qwen):
    import httpx
    original = httpx.AsyncClient
    requests = []
    chunks = [
        {"choices":[{"index":0,"delta":{"content":"Ready ","tool_calls":[{"index":0,"id":"call_1",
            "function":{"name":"read_file","arguments":'{"path":'}}]}}]},
        {"choices":[{"index":0,"delta":{"content":"now","tool_calls":[{"index":0,
            "function":{"arguments":'"app.js"}'}}]},"finish_reason":"tool_calls"}]},
        {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}},
    ]
    def respond(request):
        requests.append(json.loads(request.content))
        body = "".join("data: " + json.dumps(item) + "\n\n" for item in chunks)
        return httpx.Response(200, text=body + ("data: [DONE]\n\n" if complete else ""),
                              headers={"content-type":"text/event-stream"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    client = OpenAIHTTPClient(HarnessConfig(openai_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1" if qwen else "https://api.tokenwave.us/v1",
                                           openai_api_key="test-key"), 20)
    options = {'model':'qwen3.7-max','_stream':True,'enable_thinking':False} if qwen else {'model':'gpt-5.5'}
    if not complete:
        with pytest.raises(RuntimeError, match="DONE marker"):
            await client.complete(**options, messages=[])
    else:
        result = await client.complete(**options, messages=[])
        message = result["choices"][0]["message"]
        assert message["content"] == "Ready now"
        assert message["tool_calls"][0]["function"] == {"name":"read_file", "arguments":'{"path":"app.js"}'}
        assert result["usage"] == {"prompt_tokens":7,"completion_tokens":3}
    assert len(requests) == 1
    assert requests[0]["stream"] is True
    assert '_stream' not in requests[0]
    if qwen:
        assert requests[0]['enable_thinking'] is False
        assert requests[0]['model'] == 'qwen3.7-max'


@pytest.mark.anyio
async def test_openai_http_client_never_retries_a_transport_error(monkeypatch):
    import httpx

    attempts = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError("transient proxy failure")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    config = HarnessConfig(openai_base_url="https://example.test/v1", openai_api_key="test-key")

    with pytest.raises(httpx.ConnectError, match="transient proxy failure"):
        await OpenAIHTTPClient(config, 20).complete(model="qwen-test", messages=[])
    assert attempts == 1


@pytest.mark.anyio
async def test_openai_http_client_never_retries_qwen_burst_limit(monkeypatch):
    import httpx

    attempts = 0
    class Response:
        def __init__(self, status_code: int, text: str):
            self.status_code = status_code
            self.text = text
            self.is_error = status_code >= 400
            self.request = httpx.Request("POST", "https://example.test/v1/chat/completions")

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
            return Response(429, '{"code":"limit_burst_rate"}')

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    config = HarnessConfig(openai_base_url="https://example.test/v1", openai_api_key="test-key")

    with pytest.raises(httpx.HTTPStatusError, match="429"):
        await OpenAIHTTPClient(config, 20).complete(model="qwen-test", messages=[])
    assert attempts == 1


@pytest.mark.anyio
async def test_openai_http_client_reports_non_json_success_without_retry(monkeypatch):
    import httpx

    attempts = 0

    class Response:
        status_code = 200
        text = "upstream protocol mismatch"
        is_error = False
        headers = {"content-type": "text/plain"}
        request = httpx.Request("POST", "https://example.test/chat/completions")

        def json(self):
            raise json.JSONDecodeError("Expecting value", self.text, 0)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    config = HarnessConfig(
        openai_base_url="https://example.test", openai_api_key="test-key"
    )

    with pytest.raises(
        RuntimeError,
        match="non-JSON.*status=200.*content-type=text/plain",
    ):
        await OpenAIHTTPClient(config, 20).complete(
            model="qwen-test", messages=[]
        )
    assert attempts == 1


@pytest.mark.anyio
async def test_openai_http_client_uses_one_bounded_request(monkeypatch):
    import httpx

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("proxy unavailable")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    config = HarnessConfig(openai_base_url="https://example.test/v1", openai_api_key="test-key")

    with pytest.raises(httpx.ConnectError, match="proxy unavailable"):
        await OpenAIHTTPClient(config, 1).complete(model="qwen-test", messages=[])


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
async def test_native_loop_records_usage_and_stops_before_phase_overspend(tmp_path: Path):
    trace_path = tmp_path / "planner.jsonl"
    client = CapturingFakeClient([
        reply(tool_calls=[{
            "id": "1",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": '{"path":"partial.txt","content":"real progress"}',
            },
        }]),
        reply(content="must not be requested"),
    ])

    async def incomplete_hook(*_args):
        return {"decision": "block", "reason": "required artifact missing"}

    with pytest.raises(RuntimeError, match="planner cost budget exhausted"):
        await run_openai_agent(
            prompt="plan",
            config=HarnessConfig(planner_budget_usd=0.00001),
            workdir=tmp_path,
            model="qwen3.6-plus",
            system_prompt="system",
            max_turns=10,
            allow_bash=False,
            client=client,
            stop_hooks=[incomplete_hook],
            trace_path=trace_path,
        )

    assert len(client.requests) == 1
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    usage_event = next(event for event in events if event["event"] == "usage")
    assert usage_event["cumulative_usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert usage_event["estimated_cost_usd"] > usage_event["phase_budget_usd"]


@pytest.mark.anyio
async def test_native_loop_carries_failed_attempt_spend_across_resume(tmp_path: Path):
    trace_path = tmp_path / "planner.jsonl"
    trace_path.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                {"event": "run_start", "model": "deepseek-chat"},
                {
                    "event": "usage",
                    "cumulative_usage": {"input_tokens": 4, "output_tokens": 3},
                },
                {
                    "event": "run_error",
                    "cumulative_usage": {"input_tokens": 4, "output_tokens": 3},
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client = CapturingFakeClient([reply(content="done")])

    result, _cost, _text, _denials = await run_openai_agent(
        prompt="resume", config=HarnessConfig(planner_budget_usd=1),
        workdir=tmp_path, model="deepseek-chat", system_prompt="system",
        max_turns=2, allow_bash=False, client=client, trace_path=trace_path,
    )

    assert result.usage == {"input_tokens": 7, "output_tokens": 5}
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert events[-2]["attempt_usage"] == {"input_tokens": 3, "output_tokens": 2}
    assert events[-2]["cumulative_usage"] == {"input_tokens": 7, "output_tokens": 5}


@pytest.mark.anyio
async def test_native_loop_refuses_new_request_when_prior_attempt_spent_phase_budget(
    tmp_path: Path,
):
    trace_path = tmp_path / "planner.jsonl"
    trace_path.write_text(
        json.dumps({"event": "run_start", "model": "qwen3.6-plus"}) + "\n"
        + json.dumps(
            {
                "event": "run_error",
                "cumulative_usage": {"input_tokens": 3, "output_tokens": 2},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    client = CapturingFakeClient([reply(content="must not be requested")])

    with pytest.raises(RuntimeError, match="planner cost budget exhausted"):
        await run_openai_agent(
            prompt="resume", config=HarnessConfig(planner_budget_usd=0.000001),
            workdir=tmp_path, model="qwen3.6-plus", system_prompt="system",
            max_turns=2, allow_bash=False, client=client, trace_path=trace_path,
        )

    assert client.requests == []


@pytest.mark.anyio
async def test_native_loop_sends_user_reference_images_as_multimodal_content(tmp_path: Path):
    image = tmp_path / "reference.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nreference")
    client = CapturingFakeClient([reply(content="done")])

    await run_openai_agent(
        prompt="edit to match", image_paths=[image], config=HarnessConfig(),
        workdir=tmp_path, model="deepseek-chat", system_prompt="system",
        max_turns=2, allow_bash=False, client=client,
    )

    content = client.requests[0]["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "edit to match"}
    assert content[1]["type"] == "image_url"


@pytest.mark.anyio
async def test_turn_limit_grants_one_finalization_window(tmp_path: Path):
    client = CapturingFakeClient([
        reply(tool_calls=[{
            "id": "0", "type": "function",
            "function": {"name": "write_file", "arguments": '{"path":"x.txt","content":"x"}'},
        }]),
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
    assert result.usage["input_tokens"] == 9
    final_messages = client.requests[1]["messages"]
    assert any(
        message.get("role") == "user"
        and "FINAL CHANCE" in message.get("content", "")
        for message in final_messages
    )


@pytest.mark.anyio
async def test_generator_final_chance_allows_required_artifact_write(tmp_path: Path):
    client = FakeClient([
        reply(tool_calls=[{
            "id": "0", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"missing.txt"}'},
        }]),
        reply(tool_calls=[{
            "id": "1", "type": "function",
            "function": {"name": "write_file", "arguments": '{"path":".harness/final.json","content":"{}"}'},
        }]),
        reply(content="done"),
    ])

    _result, _, text, _ = await run_openai_agent(
        prompt="build", config=HarnessConfig(), workdir=tmp_path,
        model="deepseek-chat", system_prompt="system", max_turns=1,
        allow_bash=True, client=client,
    )

    assert text == "done"
    assert tmp_path.joinpath(".harness/final.json").read_text() == "{}"


@pytest.mark.anyio
async def test_generator_allows_diagnosis_until_true_final_chance(tmp_path: Path):
    client = CapturingFakeClient([
        reply(tool_calls=[{
            "id": "0", "type": "function",
            "function": {"name": "write_file", "arguments": '{"path":"x.txt","content":"x"}'},
        }]),
        reply(tool_calls=[{
            "id": "1", "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"missing.txt"}'},
        }]),
        reply(content="done"),
    ])

    _result, _, text, _ = await run_openai_agent(
        prompt="build", config=HarnessConfig(), workdir=tmp_path,
        model="deepseek-chat", system_prompt="system", max_turns=2,
        allow_bash=False, client=client,
    )

    assert text == "done"
    assert any(
        "FINAL CHANCE" in str(message.get("content", ""))
        for message in client.requests[2]["messages"]
    )


@pytest.mark.anyio
async def test_frontend_git_location_is_explicit_in_native_guidance(tmp_path):
    (tmp_path/'frontend/.git').mkdir(parents=True)
    client = CapturingFakeClient([reply(content='done')])
    await run_openai_agent(prompt='build', config=HarnessConfig(), workdir=tmp_path,
        model='gpt-5.5', system_prompt='system', max_turns=2, allow_bash=True, client=client)
    system = client.requests[0]['messages'][0]['content']
    assert 'cd frontend && git status --short' in system
    assert 'does not persist between tool calls' in system
    assert 'does not support git -C' in system


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
async def test_unchanged_visible_read_is_suppressed_before_repeat_breaker(tmp_path: Path):
    (tmp_path / "source.js").write_text("const value = 1;\n")
    repeated = lambda call_id: [{
        "id": call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"source.js"}'},
    }]
    client = CapturingFakeClient([
        reply(tool_calls=repeated("r1")),
        reply(tool_calls=repeated("r2")),
        reply(content="done"),
    ])

    await run_openai_agent(
        prompt="inspect", config=HarnessConfig(), workdir=tmp_path,
        model="deepseek-chat", system_prompt="system", max_turns=4,
        allow_bash=False, client=client,
    )

    tool_messages = [
        message for message in client.requests[-1]["messages"]
        if message.get("role") == "tool"
    ]
    assert "const value = 1" in tool_messages[0]["content"]
    assert tool_messages[1]["content"].startswith("UNCHANGED_READ_SUPPRESSED")


@pytest.mark.anyio
async def test_phase_timeout_is_hard(tmp_path: Path):
    class SlowClient:
        async def complete(self, **kwargs):
            import asyncio
            await asyncio.sleep(60)

    trace_path = tmp_path / "timeout.jsonl"
    with pytest.raises(RuntimeError, match="phase timed out"):
        await run_openai_agent(
            prompt="build", config=HarnessConfig(), workdir=tmp_path, model="deepseek-chat",
            system_prompt="system", max_turns=10, allow_bash=False, client=SlowClient(),
            limits=OpenAIRunLimits(phase_timeout=0.02, request_timeout=60),
            trace_path=trace_path,
        )

    error_event = [
        json.loads(line) for line in trace_path.read_text().splitlines()
        if json.loads(line)["event"] == "run_error"
    ][0]
    assert error_event["error_type"] == "CancelledError"
    assert error_event["cumulative_usage"] == {"input_tokens": 0, "output_tokens": 0}


@pytest.mark.anyio
async def test_completion_validation_is_traced_and_warns_against_rewrite(tmp_path: Path):
    client = CapturingFakeClient([reply(content="done"), reply(content="done")])
    hook_calls = 0

    async def commit_hook(*_args):
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 1:
            return {"decision": "block", "reason": "No `feat` commit was created."}
        return {"decision": "complete"}

    trace_path = tmp_path / "completion.jsonl"
    await run_openai_agent(
        prompt="build", config=HarnessConfig(), workdir=tmp_path, model="deepseek-chat",
        system_prompt="system", max_turns=3, allow_bash=True, client=client,
        stop_hooks=[commit_hook], trace_path=trace_path,
    )

    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    validation = next(event for event in events if event["event"] == "completion_validation")
    assert validation["reason"] == "No `feat` commit was created."
    retry_messages = client.requests[1]["messages"]
    assert any(
        "Do not rewrite it" in str(message.get("content", ""))
        for message in retry_messages
    )


@pytest.mark.anyio
async def test_planner_receives_validation_at_progress_bundle_boundary(tmp_path: Path):
    client = CapturingFakeClient([
        reply(tool_calls=[{
            "id": "progress",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": '{"path":".harness/progress.md","content":"done"}',
            },
        }]),
        reply(tool_calls=[{
            "id": "fix",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": '{"path":".harness/ui_verification_plan.json","content":"{}"}',
            },
        }]),
    ])
    hook_calls = 0

    async def planner_hook(*_args):
        nonlocal hook_calls
        hook_calls += 1
        if hook_calls == 1:
            return {"decision": "block", "reason": "UI-001 has two assertions"}
        return {"decision": "complete"}

    await run_openai_agent(
        prompt="plan", config=HarnessConfig(), workdir=tmp_path,
        model="deepseek-chat", system_prompt="system", max_turns=4,
        allow_bash=False, client=client, stop_hooks=[planner_hook],
    )

    next_messages = client.requests[1]["messages"]
    assert any(
        "Edit only the named invalid artifacts" in str(message.get("content", ""))
        and "UI-001 has two assertions" in str(message.get("content", ""))
        for message in next_messages
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

    assert policy.check("read_file") is None
    assert "exploration budget" in policy.check("search_files")
    assert policy.check("browser_screenshot") is None
    assert policy.check("write_file") is None
    assert policy.check("apply_patch") is None
    assert policy.check("browser_click") is None
    assert policy.check("browser_fill") is None


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


@pytest.mark.anyio
async def test_evaluator_repeated_budget_guidance_does_not_trip_error_breaker(
    monkeypatch, tmp_path: Path
):
    class FakeTools:
        def __init__(self, **kwargs):
            pass

        async def execute(self, _name, _args):
            return SimpleNamespace(ok=True, output="ok", changed=False)

        async def close(self):
            pass

    monkeypatch.setattr("src.agents.openai_runner.OpenAIToolExecutor", FakeTools)
    evaluate = lambda call_id: [{
        "id": call_id, "type": "function",
        "function": {"name": "browser_evaluate", "arguments": '{"expression":"x' + call_id + '"}'},
    }]
    write = [{
        "id": "write", "type": "function",
        "function": {"name": "write_file", "arguments": '{"path":"grade.json","content":"{}"}'},
    }]
    client = FakeClient([
        reply(tool_calls=evaluate("1")),
        reply(tool_calls=evaluate("2")),
        reply(tool_calls=evaluate("3")),
        reply(tool_calls=evaluate("4")),
        reply(tool_calls=write),
        reply(content="done"),
    ])

    _result, _cost, text, _denials = await run_openai_agent(
        prompt="evaluate", config=HarnessConfig(), workdir=tmp_path,
        model="deepseek-chat", system_prompt="system", max_turns=10,
        allow_bash=False, allow_playwright=True, client=client,
        limits=OpenAIRunLimits(evaluation_exploration_limit=20, evaluation_browser_evaluate_limit=2),
    )

    assert text == "done"


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["completed", "incomplete"])
async def test_tokenwave_responses_recovery_preserves_prompt_and_usage(monkeypatch, status):
    import httpx
    original = httpx.AsyncClient
    monkeypatch.delenv("TOKENWAVE_API_PROXY", raising=False)
    def respond(request):
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        assert body["model"] == "gpt-5.5"
        assert body["instructions"] == "Return JSON"
        assert body["input"] == [{"role":"user","content":"Current source"}]
        assert body["max_output_tokens"] == 12000 and body["store"] is False
        event = {"type":"response." + status, "response": {"status":status,
            "output":[{"type":"message","content":[{"type":"output_text","text":"{}"}]}],
            "usage":{"input_tokens":19,"output_tokens":7,"total_tokens":26}}}
        return httpx.Response(200,text="data: " + json.dumps(event) + "\n\n")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    client = OpenAIHTTPClient(HarnessConfig(openai_base_url="https://api.tokenwave.us/v1", openai_api_key="test"),20)
    call = dict(model="gpt-5.5",messages=[{"role":"system","content":"Return JSON"},
                                        {"role":"user","content":"Current source"}],
                max_tokens=12000,_protocol="responses")
    if status == "incomplete":
        with pytest.raises(RuntimeError,match="without a completed response"):
            await client.complete(**call)
    else:
        result = await client.complete(**call)
        assert result["choices"][0]["message"]["content"] == "{}"
        assert result["usage"]["prompt_tokens"] == 19
        assert result["usage"]["completion_tokens"] == 7


@pytest.mark.anyio
async def test_responses_converts_function_tools_and_tool_turns(monkeypatch):
    import httpx
    original = httpx.AsyncClient
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            assert body["tools"] == [{
                "type": "function",
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }]
            assert body["tool_choice"] == "auto"
            output = [{
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "read_file",
                "arguments": '{"path":"index.html"}',
            }]
        else:
            assert body["input"][-2:] == [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": '{"path":"index.html"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "<main>Current</main>",
                },
            ]
            output = [{
                "type": "message",
                "content": [{"type": "output_text", "text": "done"}],
            }]
        event = {"type": "response.completed", "response": {
            "status": "completed", "output": output,
            "usage": {"input_tokens": 8, "output_tokens": 3},
        }}
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\n")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    client = OpenAIHTTPClient(HarnessConfig(
        openai_base_url="https://example.test/v1",
        openai_api_key="test",
        openai_wire_api="responses",
    ), 20)
    tools = [{"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }}]
    first = await client.complete(
        model="gpt-5.5",
        messages=[{"role": "user", "content": "Inspect the page"}],
        tools=tools,
        tool_choice="auto",
    )
    tool_call = first["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["id"] == "call_1"
    assert tool_call["function"] == {
        "name": "read_file",
        "arguments": '{"path":"index.html"}',
    }

    second = await client.complete(
        model="gpt-5.5",
        messages=[
            {"role": "user", "content": "Inspect the page"},
            first["choices"][0]["message"],
            {"role": "tool", "tool_call_id": "call_1", "content": "<main>Current</main>"},
        ],
        tools=tools,
        tool_choice="auto",
    )
    assert second["choices"][0]["message"]["content"] == "done"


@pytest.mark.anyio
async def test_responses_profile_preserves_exact_endpoint_model_and_headers(monkeypatch):
    import httpx
    original = httpx.AsyncClient
    def respond(request):
        assert str(request.url) == 'https://api.nju-link.com/responses'
        assert request.headers['x-openai-actor-authorization'] == 'local-image-extension'
        assert request.headers['authorization'] == 'Bearer test-only'
        body=json.loads(request.content)
        assert body['model']=='gpt-5.6-luna' and body['store'] is False
        assert body['text']=={'format':{'type':'json_object'}}
        assert body['reasoning'] == {'effort': 'low'}
        event={'type':'response.completed','response':{'status':'completed','output':[
            {'type':'message','content':[{'type':'output_text','text':'{"ok":true}'}]}],
            'usage':{'input_tokens':9,'output_tokens':4}}}
        return httpx.Response(200,text='data: '+json.dumps(event)+'\n\n')
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original(transport=httpx.MockTransport(respond),**kwargs))
    config=HarnessConfig(openai_base_url='https://api.nju-link.com',openai_api_key='test-only',
        openai_wire_api='responses',openai_extra_headers={'x-openai-actor-authorization':'local-image-extension'})
    result=await OpenAIHTTPClient(config,10).complete(model='gpt-5.6-luna',messages=[{'role':'user','content':'Test configuration'}],response_format={'type':'json_object'},reasoning_effort='low')
    assert result['choices'][0]['message']['content']=='{"ok":true}'
    assert result['usage']['prompt_tokens']==9


@pytest.mark.anyio
@pytest.mark.parametrize('status',['completed','failed','incomplete'])
async def test_responses_terminal_event_does_not_wait_for_connection_close(monkeypatch,tmp_path,status):
    import httpx
    original=httpx.AsyncClient
    closed=[]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield ('data: '+json.dumps({'type':'response.output_text.delta','delta':'secret generated text'})+'\n\n').encode()
            yield ('data: '+json.dumps({'type':'response.'+status,'response':{'status':status,
                'error':{'code':'server_error','message':'backend failed test-secret'} if status=='failed' else None,
                'output':[{'content':[{'type':'output_text','text':'done'}]}],
                'usage':{'input_tokens':3,'output_tokens':1}}})+'\n\n').encode()
            raise AssertionError('Client read beyond the terminal event')
        async def aclose(self):closed.append(True)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=Stream())),**kwargs))
    log=tmp_path/'stream.jsonl';monkeypatch.setenv('OPENAI_STREAM_LOG',str(log))
    client=OpenAIHTTPClient(HarnessConfig(openai_base_url='https://example.test',openai_api_key='test-secret',openai_wire_api='responses'),1)
    if status=='completed':
        result=await client.complete(model='gpt-5.6-luna',messages=[{'role':'user','content':'secret prompt'}])
        assert result['choices'][0]['message']['content']=='done'
    else:
        expected = ResponsesStreamReadError if status == 'failed' else RuntimeError
        with pytest.raises(expected):
            await client.complete(model='gpt-5.6-luna',messages=[])
    assert closed
    records=[json.loads(line) for line in log.read_text().splitlines()]
    terminal=next(r for r in records if r['event']=='terminal_event')
    assert terminal['status']==status and terminal['output_chars']==21
    assert terminal['usage']['input_tokens']==3
    if status=='failed':
        assert 'server_error' in terminal['error'] and '[redacted]' in terminal['error']
    assert 'secret' not in log.read_text()


@pytest.mark.anyio
@pytest.mark.parametrize('partial_text', ['', 'partial model output'])
async def test_responses_timeout_keeps_stream_progress(monkeypatch,tmp_path,partial_text):
    import asyncio,httpx
    original=httpx.AsyncClient
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b': keep-alive\n\ndata: {"type":"response.created"}\n\n'
            if partial_text:
                yield ('data: '+json.dumps({'type':'response.output_text.delta','delta':partial_text})+'\n\n').encode()
            await asyncio.sleep(60)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(lambda request:httpx.Response(200,stream=Stream())),**kwargs))
    log=tmp_path/'stream.jsonl';monkeypatch.setenv('OPENAI_STREAM_LOG',str(log))
    client=OpenAIHTTPClient(HarnessConfig(openai_base_url='https://example.test',openai_api_key='test',openai_wire_api='responses'),.05)
    with pytest.raises(TimeoutError):await client.complete(model='gpt-5.6-luna',messages=[])
    records=[json.loads(line) for line in log.read_text().splitlines()]
    assert records[-1]['event']=='request_interrupted'
    assert records[-1]['event_count']==1+bool(partial_text) and records[-1]['output_chars']==len(partial_text)
    if partial_text:
        saved = next(item for item in records if item['event']=='partial_response_saved')
        assert Path(saved['path']).read_text() == partial_text
        assert saved['status']=='incomplete'
    assert any(r['event']=='response_headers' and r['status']==200 for r in records)


@pytest.mark.anyio
@pytest.mark.parametrize('failures,expected_calls', [(1, 2), (3, 3)])
async def test_responses_only_retries_diagnosed_stream_read_failures(monkeypatch, tmp_path, failures, expected_calls):
    import httpx
    from src.agents.openai_runner import ResponsesStreamReadError
    original = httpx.AsyncClient
    calls = []
    sleeps = []
    def respond(request):
        calls.append(json.loads(request.content))
        if len(calls) <= failures:
            event = {'type':'error','error':{'code':'stream_read_error'}}
        else:
            event = {'type':'response.completed','response':{'status':'completed',
                'output':[{'type':'message','content':[{'type':'output_text','text':'{}'}]}],
                'usage':{'input_tokens':5,'output_tokens':2}}}
        return httpx.Response(200,text='data: '+json.dumps(event)+'\n\n')
    async def sleep(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr('src.agents.openai_runner.asyncio.sleep', sleep)
    log = tmp_path/'stream.jsonl'
    monkeypatch.setenv('OPENAI_STREAM_LOG',str(log))
    config = HarnessConfig(openai_base_url='https://api.nju-link.com',openai_api_key='test',
        openai_wire_api='responses',openai_stream_read_retries=2)
    client = OpenAIHTTPClient(config, 10)
    if failures == 3:
        with pytest.raises(ResponsesStreamReadError):
            await client.complete(model='gpt-5.6-luna',messages=[])
    else:
        result = await client.complete(model='gpt-5.6-luna',messages=[])
        assert result['usage']['input_tokens'] == 5
    assert len(calls) == expected_calls
    assert all(call == calls[0] for call in calls)
    assert sleeps == [5, 10][:expected_calls-1]
    events = [json.loads(line) for line in log.read_text().splitlines()]
    retries = [e for e in events if e['event']=='request_retry']
    assert len(retries) == expected_calls-1
    assert all(e['failed_request_usage']=='unavailable' for e in retries)


@pytest.mark.anyio
async def test_responses_retries_incomplete_chunked_read(monkeypatch, tmp_path):
    import httpx
    original = httpx.AsyncClient
    calls = []
    sleeps = []

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.RemoteProtocolError("incomplete chunked read")
            yield b""  # pragma: no cover

    def respond(_request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, stream=Interrupted())
        event = {'type':'response.completed','response':{'status':'completed','output':[
            {'type':'message','content':[{'type':'output_text','text':'{}'}]}],
            'usage':{'input_tokens':1,'output_tokens':1}}}
        return httpx.Response(200, text='data: '+json.dumps(event)+'\n\n')

    async def no_wait(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    monkeypatch.setattr('src.agents.openai_runner.asyncio.sleep', no_wait)
    monkeypatch.setenv('OPENAI_STREAM_LOG', str(tmp_path/'stream.jsonl'))
    config = HarnessConfig(openai_base_url='https://api.nju-link.com', openai_api_key='test',
        openai_wire_api='responses', openai_stream_read_retries=2)

    result = await OpenAIHTTPClient(config, 10).complete(model='gpt-5.6-luna', messages=[])

    assert result['usage']['input_tokens'] == 1
    assert len(calls) == 2
    assert sleeps == [5]


@pytest.mark.anyio
async def test_responses_retries_a_bounded_request_timeout(monkeypatch):
    calls = []
    sleeps = []

    async def stream(self, *_args):
        calls.append(1)
        if len(calls) == 1:
            raise asyncio.TimeoutError
        return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

    async def no_wait(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(OpenAIHTTPClient, "_stream_responses", stream)
    monkeypatch.setattr("src.agents.openai_runner.asyncio.sleep", no_wait)
    config = HarnessConfig(
        openai_base_url="https://api.nju-link.com", openai_api_key="test",
        openai_wire_api="responses", openai_stream_read_retries=2,
    )

    result = await OpenAIHTTPClient(config, 10).complete(
        model="gpt-5.6-luna", messages=[]
    )

    assert result["choices"][0]["message"]["content"] == "{}"
    assert len(calls) == 2
    assert sleeps == [5]


@pytest.mark.anyio
async def test_responses_retries_upstream_http2_stream_error(monkeypatch):
    import httpx
    original = httpx.AsyncClient
    calls = []
    def respond(request):
        calls.append(1)
        event = ({'type':'error','sequence_number':0,'code':'upstream_http2_stream_error',
            'message':'Upstream HTTP/2 stream failed','param':None} if len(calls)==1 else
            {'type':'response.completed','response':{'status':'completed','output':[
                {'type':'message','content':[{'type':'output_text','text':'{}'}]}],
                'usage':{'input_tokens':1,'output_tokens':1}}})
        return httpx.Response(200,text='data: '+json.dumps(event)+'\n\n')
    async def no_wait(*_):
        return None
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw: original(transport=httpx.MockTransport(respond),**kw))
    monkeypatch.setattr('src.agents.openai_runner.asyncio.sleep',no_wait)
    config=HarnessConfig(openai_base_url='https://api.nju-link.com',openai_api_key='test',
        openai_wire_api='responses',openai_stream_read_retries=2)
    result=await OpenAIHTTPClient(config,10).complete(model='gpt-5.6-luna',messages=[])
    assert result['usage']['input_tokens']==1
    assert len(calls)==2
