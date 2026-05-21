"""对 Anthropic SDK 的轻量封装，替代原 LiteLLM 路由层。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import anthropic


class LLMClientError(RuntimeError):
    """调用失败时抛出，错误信息会先做脱敏。"""


@dataclass(frozen=True)
class CompletionResult:
    """封装文本结果、usage 与原始响应对象。"""

    text: str
    usage: dict[str, int]
    raw: Any


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9_.\-]{8,}"),
]


def _scrub(text: str, *, limit: int = 512) -> str:
    scrubbed = text
    for pattern in _SECRET_PATTERNS:
        scrubbed = pattern.sub("<redacted>", scrubbed)
    if len(scrubbed) > limit:
        scrubbed = scrubbed[: limit - 3] + "..."
    return scrubbed


def _normalize_usage(usage_obj: Any) -> dict[str, int]:
    if usage_obj is None:
        return {}
    if hasattr(usage_obj, "model_dump"):
        return {
            k: int(v)
            for k, v in usage_obj.model_dump().items()
            if isinstance(v, (int, float))
        }
    if isinstance(usage_obj, dict):
        return {
            k: int(v)
            for k, v in usage_obj.items()
            if isinstance(v, (int, float))
        }
    return {}


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
    """执行一次 completion 调用，直接使用 Anthropic SDK。"""
    client_kwargs: dict[str, Any] = {}
    if api_key:
        client_kwargs["api_key"] = api_key
    if api_base:
        client_kwargs["base_url"] = api_base
    if timeout is not None:
        client_kwargs["timeout"] = timeout
    client_kwargs["max_retries"] = num_retries

    client = anthropic.Anthropic(**client_kwargs)

    system_text = None
    api_messages = []
    for msg in messages:
        if msg["role"] == "system":
            system_text = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
        else:
            content = msg["content"]
            if isinstance(content, list):
                converted = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image_url":
                        url = block["image_url"]["url"]
                        if url.startswith("data:"):
                            media_type, _, b64_data = url.partition(";base64,")
                            media_type = media_type.replace("data:", "")
                            converted.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64_data,
                                },
                            })
                        else:
                            converted.append({
                                "type": "image",
                                "source": {"type": "url", "url": url},
                            })
                    else:
                        converted.append(block)
                content = converted
            api_messages.append({"role": msg["role"], "content": content})

    create_kwargs: dict[str, Any] = {
        "model": model,
        "messages": api_messages,
        "max_tokens": max_tokens or 4096,
    }
    if temperature > 0:
        create_kwargs["temperature"] = temperature
    if system_text:
        create_kwargs["system"] = system_text
    create_kwargs.update(extra)

    try:
        response = client.messages.create(**create_kwargs)
    except Exception as exc:
        raise LLMClientError(f"completion failed: {_scrub(str(exc))}") from exc

    text_parts = []
    for block in response.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)

    return CompletionResult(
        text="\n".join(text_parts),
        usage=_normalize_usage(response.usage),
        raw=response,
    )
