"""对 LiteLLM `completion()` 的轻量封装。"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


class LLMClientError(RuntimeError):
    """LiteLLM 调用失败时抛出，错误信息会先做脱敏。"""


@dataclass(frozen=True)
class CompletionResult:
    """封装文本结果、usage 与原始响应对象。"""

    text: str
    usage: dict[str, int]
    raw: Any  # LiteLLM 的原始响应对象，供上层读取扩展字段。


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_.\-]{8,}"),
]
_LITELLM_IMPORT_WARNING_SNIPPETS = (
    "could not pre-load bedrock-runtime response stream shape",
    "could not pre-load sagemaker-runtime response stream shape",
)
_litellm_module: Any | None = None


def _scrub(text: str, *, limit: int = 512) -> str:
    """脱敏常见密钥形态，并限制错误文案长度。"""
    scrubbed = text
    for pattern in _SECRET_PATTERNS:
        scrubbed = pattern.sub("<redacted>", scrubbed)
    if len(scrubbed) > limit:
        scrubbed = scrubbed[: limit - 3] + "..."
    return scrubbed


def _extract_text(response: Any) -> str:
    """从 LiteLLM 响应中提取首个 choice 的文本内容。"""
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise LLMClientError("provider returned no choices")
    message = getattr(choices[0], "message", None)
    if message is None:
        raise LLMClientError("provider returned a choice with no message")
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    raise LLMClientError(f"unsupported message content shape: {type(content).__name__}")


def _normalize_usage(usage_obj: Any) -> dict[str, int]:
    """将 LiteLLM usage 结构统一整理成 `dict[str, int]`。"""
    if usage_obj is None:
        return {}
    if hasattr(usage_obj, "model_dump"):
        usage_obj = usage_obj.model_dump()
    if not isinstance(usage_obj, dict):
        return {}
    return {
        key: int(value)
        for key, value in usage_obj.items()
        if isinstance(value, (int, float))
    }


class _LiteLLMImportWarningFilter(logging.Filter):
    """仅过滤 LiteLLM 导入期的可选 AWS 依赖告警。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not any(snippet in message for snippet in _LITELLM_IMPORT_WARNING_SNIPPETS)


@contextmanager
def _suppress_litellm_import_warnings():
    logger = logging.getLogger("LiteLLM")
    warning_filter = _LiteLLMImportWarningFilter()
    logger.addFilter(warning_filter)
    try:
        yield
    finally:
        logger.removeFilter(warning_filter)


def _load_litellm() -> Any:
    """延迟导入 LiteLLM，并压制 botocore 缺失带来的无关启动告警。"""
    global _litellm_module
    if _litellm_module is not None:
        return _litellm_module

    if importlib.util.find_spec("botocore") is None:
        with _suppress_litellm_import_warnings():
            _litellm_module = importlib.import_module("litellm")
    else:
        _litellm_module = importlib.import_module("litellm")
    return _litellm_module


def completion(
    messages: list[dict[str, Any]],
    *,
    model: str,
    api_key: str | None = None,
    api_base: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    num_retries: int = 3,
    timeout: float | None = 90.0,
    **extra: Any,
) -> CompletionResult:
    """执行一次 completion 调用，并返回统一结果结构。"""
    litellm = _load_litellm()
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "num_retries": num_retries,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if timeout is not None:
        kwargs["timeout"] = timeout
    kwargs.update(extra)

    try:
        response = litellm.completion(**kwargs)
    except Exception as exc:  # LiteLLM uses a wide exception hierarchy
        raise LLMClientError(f"llm completion failed: {_scrub(str(exc))}") from exc

    return CompletionResult(
        text=_extract_text(response),
        usage=_normalize_usage(getattr(response, "usage", None)),
        raw=response,
    )
