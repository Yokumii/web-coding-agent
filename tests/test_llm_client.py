import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.utils.llm_client import CompletionResult, completion, LLMClientError, _load_litellm


@pytest.fixture(autouse=True)
def reset_litellm_cache():
    import src.utils.llm_client as llm_client

    llm_client._litellm_module = None
    yield
    llm_client._litellm_module = None


@patch("src.utils.llm_client._load_litellm")
def test_returns_text_and_usage(mock_load_litellm):
    mock_completion = MagicMock()
    mock_load_litellm.return_value = SimpleNamespace(completion=mock_completion)
    mock_completion.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="hello"))],
        usage=MagicMock(model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 5}),
    )
    result = completion(
        messages=[{"role": "user", "content": "hi"}],
        model="anthropic/claude-sonnet-4-6",
        api_key="sk-test",
    )
    assert isinstance(result, CompletionResult)
    assert result.text == "hello"
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5}


@patch("src.utils.llm_client._load_litellm")
def test_passes_api_base(mock_load_litellm):
    mock_completion = MagicMock()
    mock_load_litellm.return_value = SimpleNamespace(completion=mock_completion)
    mock_completion.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="x"))],
        usage=MagicMock(model_dump=lambda: {}),
    )
    completion(
        messages=[{"role": "user", "content": "x"}],
        model="anthropic/claude-sonnet-4-6",
        api_key="sk-test",
        api_base="https://proxy.example.com/v1",
    )
    kwargs = mock_completion.call_args.kwargs
    assert kwargs["api_base"] == "https://proxy.example.com/v1"


@patch("src.utils.llm_client._load_litellm")
def test_wraps_provider_error_with_scrubbed_message(mock_load_litellm):
    mock_completion = MagicMock()
    mock_load_litellm.return_value = SimpleNamespace(completion=mock_completion)
    mock_completion.side_effect = RuntimeError("auth failed for sk-secret123abc")
    with pytest.raises(LLMClientError) as exc:
        completion(
            messages=[{"role": "user", "content": "x"}],
            model="anthropic/claude-sonnet-4-6",
            api_key="sk-secret123abc",
        )
    # Secret should not appear verbatim in the message.
    assert "sk-secret123abc" not in str(exc.value)


@patch("src.utils.llm_client._load_litellm")
def test_handles_list_content_response(mock_load_litellm):
    mock_completion = MagicMock()
    mock_load_litellm.return_value = SimpleNamespace(completion=mock_completion)
    """OpenAI-compatible providers sometimes return list content blocks."""
    msg_mock = MagicMock()
    msg_mock.content = [
        {"type": "text", "text": "part1"},
        {"type": "text", "text": "part2"},
    ]
    mock_completion.return_value = MagicMock(
        choices=[MagicMock(message=msg_mock)],
        usage=MagicMock(model_dump=lambda: {}),
    )
    result = completion(
        messages=[{"role": "user", "content": "x"}],
        model="anthropic/claude-sonnet-4-6",
        api_key="sk-test",
    )
    assert result.text == "part1\npart2"


def test_load_litellm_suppresses_optional_botocore_warnings(monkeypatch, caplog):
    import src.utils.llm_client as llm_client

    logger = logging.getLogger("LiteLLM")
    imported_module = SimpleNamespace(completion=lambda **_: None)

    def fake_import_module(name: str):
        assert name == "litellm"
        logger.warning(
            "litellm: could not pre-load bedrock-runtime response stream shape "
            "— Bedrock event-stream decoding will be unavailable. Error: No module named 'botocore'"
        )
        logger.warning(
            "litellm: could not pre-load sagemaker-runtime response stream shape "
            "— SageMaker event-stream decoding will be unavailable. Error: No module named 'botocore'"
        )
        logger.warning("litellm: unrelated warning should still be visible")
        return imported_module

    monkeypatch.setattr("src.utils.llm_client.importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr("src.utils.llm_client.importlib.import_module", fake_import_module)

    with caplog.at_level(logging.WARNING, logger="LiteLLM"):
        loaded = _load_litellm()

    assert loaded is imported_module
    assert "bedrock-runtime response stream shape" not in caplog.text
    assert "sagemaker-runtime response stream shape" not in caplog.text
    assert "unrelated warning should still be visible" in caplog.text
