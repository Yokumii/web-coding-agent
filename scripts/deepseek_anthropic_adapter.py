"""Small local Anthropic Messages -> DeepSeek Chat Completions adapter for trials."""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text", "")) for item in value
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return ""


def _messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    system = _text(payload.get("system"))
    if system:
        converted.append({"role": "system", "content": system})
    for message in payload.get("messages", []):
        role = message.get("role")
        content = message.get("content", "")
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text_parts.append(str(block.get("text", "")))
            elif kind == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })
            elif kind == "tool_result":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _text(block.get("content")) or str(block.get("content", "")),
                })
        if role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if tool_calls:
                item["tool_calls"] = tool_calls
            converted.append(item)
        else:
            converted.extend(tool_results)
            if text_parts:
                converted.append({"role": "user", "content": "\n".join(text_parts)})
    return converted


def _tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "type": "function",
        "function": {
            "name": item.get("name"),
            "description": item.get("description", ""),
            "parameters": item.get("input_schema", {"type": "object", "properties": {}}),
        },
    } for item in payload.get("tools", [])]


def _event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _upstream_max_tokens(payload: dict[str, Any]) -> int:
    limit = int(os.environ.get("ADAPTER_MAX_TOKENS", "16384"))
    requested = int(payload.get("max_tokens") or 8192)
    return max(1, min(requested, limit))


def _ssl_verification_enabled() -> bool:
    return os.environ.get("SSL_NO_VERIFY", "").strip().lower() not in {
        "1", "true", "yes", "on",
    }


def _merge_stream_chunks(chunks: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge OpenAI-compatible streaming deltas into one assistant message."""
    text_parts: list[str] = []
    calls: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    for chunk in chunks:
        if isinstance(chunk.get("usage"), dict):
            usage = dict(chunk["usage"])
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str):
            text_parts.append(content)
        for item in delta.get("tool_calls") or []:
            index = int(item.get("index", 0))
            target = calls.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if item.get("id"):
                target["id"] = item["id"]
            function = item.get("function") or {}
            if function.get("name"):
                target["function"]["name"] += str(function["name"])
            if function.get("arguments"):
                target["function"]["arguments"] += str(function["arguments"])
    return {
        "content": "".join(text_parts),
        "tool_calls": [calls[index] for index in sorted(calls)],
    }, usage


async def _read_openai_stream(response) -> tuple[dict[str, Any], dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    while not response.content.at_eof():
        raw = await response.content.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        chunks.append(json.loads(data))
    return _merge_stream_chunks(chunks)


async def messages(request: web.Request) -> web.StreamResponse:
    payload = await request.json()
    upstream_payload: dict[str, Any] = {
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "messages": _messages(payload),
        "max_tokens": _upstream_max_tokens(payload),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    tools = _tools(payload)
    if tools:
        upstream_payload["tools"] = tools
    timeout = ClientTimeout(total=float(os.environ.get("ADAPTER_TIMEOUT", "300")))
    headers = {"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}"}
    url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
    connector = TCPConnector(ssl=_ssl_verification_enabled())
    async with ClientSession(timeout=timeout, connector=connector, trust_env=True) as session:
        async with session.post(url, json=upstream_payload, headers=headers) as response:
            if response.status >= 400:
                body = await response.text()
                return web.Response(status=response.status, text=body, content_type="application/json")
            message, usage = await _read_openai_stream(response)
    tool_calls = message.get("tool_calls") or []
    stop_reason = "tool_use" if tool_calls else "end_turn"
    message_id = "msg_" + uuid.uuid4().hex

    stream = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await stream.prepare(request)
    await stream.write(_event("message_start", {"type": "message_start", "message": {
        "id": message_id, "type": "message", "role": "assistant", "model": payload.get("model", "deepseek-chat"),
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": 0},
    }}))
    index = 0
    text = message.get("content") or ""
    if text:
        await stream.write(_event("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}))
        await stream.write(_event("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}}))
        await stream.write(_event("content_block_stop", {"type": "content_block_stop", "index": index}))
        index += 1
    for call in tool_calls:
        function = call.get("function") or {}
        await stream.write(_event("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": call.get("id"), "name": function.get("name"), "input": {}}}))
        await stream.write(_event("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": function.get("arguments") or "{}"}}))
        await stream.write(_event("content_block_stop", {"type": "content_block_stop", "index": index}))
        index += 1
    await stream.write(_event("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": usage.get("completion_tokens", 0)}}))
    await stream.write(_event("message_stop", {"type": "message_stop"}))
    await stream.write_eof()
    return stream


app = web.Application(client_max_size=32 * 1024 * 1024)
app.router.add_post("/v1/messages", messages)
app.router.add_post("/messages", messages)
app.router.add_get("/health", lambda _request: web.json_response({"status": "ok"}))


if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=int(os.environ.get("ADAPTER_PORT", "4000")))
