from scripts.deepseek_anthropic_adapter import (
    _merge_stream_chunks,
    _messages,
    _ssl_verification_enabled,
    _upstream_max_tokens,
)


def test_upstream_max_tokens_clamps_to_qwen_limit():
    assert _upstream_max_tokens({"max_tokens": 32_000}) == 16_384
    assert _upstream_max_tokens({"max_tokens": 1_200}) == 1_200


def test_ssl_no_verify_environment_disables_verification(monkeypatch):
    monkeypatch.setenv("SSL_NO_VERIFY", "1")
    assert _ssl_verification_enabled() is False
    monkeypatch.setenv("SSL_NO_VERIFY", "0")
    assert _ssl_verification_enabled() is True


def test_tool_results_precede_extra_user_text():
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call_1", "name": "Read", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "system reminder"},
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "file body"},
                ],
            },
        ]
    }

    converted = _messages(payload)

    assert [item["role"] for item in converted] == ["assistant", "tool", "user"]


def test_merge_stream_chunks_collects_text_tools_and_usage():
    chunks = [
        {"choices": [{"delta": {"content": "done "}, "finish_reason": ""}]},
        {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "Write", "arguments": '{"file_'},
                    }]
                },
                "finish_reason": "",
            }]
        },
        {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": 'path":"index.html"}'},
                    }]
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7},
        },
    ]

    message, usage = _merge_stream_chunks(chunks)

    assert message == {
        "content": "done ",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "Write", "arguments": '{"file_path":"index.html"}'},
        }],
    }
    assert usage == {"prompt_tokens": 12, "completion_tokens": 7}
