from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.utils.llm_client import CompletionResult, completion, LLMClientError


@patch("src.utils.llm_client.anthropic")
def test_returns_text_and_usage(mock_anthropic):
    mock_client = MagicMock()
    mock_anthropic.Anthropic.return_value = mock_client

    text_block = SimpleNamespace(text="hello")
    usage = SimpleNamespace(
        model_dump=lambda: {"input_tokens": 10, "output_tokens": 5}
    )
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[text_block], usage=usage
    )

    result = completion(
        messages=[{"role": "user", "content": "hi"}],
        model="test-model",
        api_key="sk-test",
    )
    assert isinstance(result, CompletionResult)
    assert result.text == "hello"
    assert result.usage == {"input_tokens": 10, "output_tokens": 5}


@patch("src.utils.llm_client.anthropic")
def test_passes_base_url(mock_anthropic):
    mock_client = MagicMock()
    mock_anthropic.Anthropic.return_value = mock_client

    text_block = SimpleNamespace(text="x")
    usage = SimpleNamespace(model_dump=lambda: {})
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[text_block], usage=usage
    )

    completion(
        messages=[{"role": "user", "content": "x"}],
        model="test-model",
        api_key="sk-test",
        api_base="https://proxy.example.com/v1",
    )
    kwargs = mock_anthropic.Anthropic.call_args.kwargs
    assert kwargs["base_url"] == "https://proxy.example.com/v1"


@patch("src.utils.llm_client.anthropic")
def test_wraps_provider_error_with_scrubbed_message(mock_anthropic):
    mock_client = MagicMock()
    mock_anthropic.Anthropic.return_value = mock_client
    mock_client.messages.create.side_effect = RuntimeError(
        "auth failed for sk-secret123abc"
    )
    with pytest.raises(LLMClientError) as exc:
        completion(
            messages=[{"role": "user", "content": "x"}],
            model="test-model",
            api_key="sk-secret123abc",
        )
    assert "sk-secret123abc" not in str(exc.value)


@patch("src.utils.llm_client.anthropic")
def test_extracts_system_message(mock_anthropic):
    mock_client = MagicMock()
    mock_anthropic.Anthropic.return_value = mock_client

    text_block = SimpleNamespace(text="result")
    usage = SimpleNamespace(model_dump=lambda: {})
    mock_client.messages.create.return_value = SimpleNamespace(
        content=[text_block], usage=usage
    )

    completion(
        messages=[
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
        ],
        model="test-model",
        api_key="sk-test",
    )
    create_kwargs = mock_client.messages.create.call_args.kwargs
    assert create_kwargs["system"] == "You are helpful."
    assert len(create_kwargs["messages"]) == 1
    assert create_kwargs["messages"][0]["role"] == "user"
