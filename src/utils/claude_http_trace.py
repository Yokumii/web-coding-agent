from __future__ import annotations

import asyncio
import copy
import gzip
import json
import logging
import time
import uuid
import zlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ALLOWED_CLAUDE_API_PATHS = ("/v1/messages", "/v1/complete")
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
SENSITIVE_HEADER_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "set-cookie2",
        "x-api-key",
        "anthropic-api-key",
    }
)
PREFIX_REDACTED_HEADER_KEYS = frozenset(
    {"authorization", "x-api-key", "anthropic-api-key"}
)

log = logging.getLogger(__name__)
TRACE_CTX_KEY = web.AppKey("trace_ctx", dict[str, Any])


@dataclass(frozen=True)
class ClaudeHttpTraceProxy:
    base_url: str
    upstream_base_url: str
    trace_path: Path


def resolve_claude_upstream_base_url(base_url: str | None) -> str:
    normalized = (base_url or "").strip()
    return normalized or DEFAULT_ANTHROPIC_BASE_URL


def _uses_loopback_host(url: str) -> bool:
    """Never send a local tracing upstream through an ambient HTTP proxy."""
    host = (urlsplit(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def normalize_usage(usage: object) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}

    normalized = dict(usage)
    if "input_tokens" not in normalized and "prompt_tokens" in usage:
        normalized["input_tokens"] = usage["prompt_tokens"]
    if "output_tokens" not in normalized and "completion_tokens" in usage:
        normalized["output_tokens"] = usage["completion_tokens"]

    if "cache_read_input_tokens" not in normalized:
        cached = usage.get("cached_tokens")
        if cached is None:
            for details_key in ("input_tokens_details", "prompt_tokens_details"):
                details = usage.get(details_key)
                if isinstance(details, dict):
                    cached = details.get("cached_tokens")
                    if cached is not None:
                        break
        if cached is not None:
            normalized["cache_read_input_tokens"] = cached

    return normalized


def _is_allowed_claude_api_path(path: str) -> bool:
    clean = path.split("?", 1)[0].rstrip("/")
    return any(
        clean == prefix or clean.startswith(prefix + "/")
        for prefix in ALLOWED_CLAUDE_API_PATHS
    )


def _filter_headers(headers: Any, *, redact_keys: bool = False) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        normalized_key = key.lower()
        if normalized_key in HOP_BY_HOP_HEADERS:
            continue
        if redact_keys and normalized_key in SENSITIVE_HEADER_KEYS:
            out[key] = (
                str(value)[:12] + "..."
                if normalized_key in PREFIX_REDACTED_HEADER_KEYS
                and len(str(value)) > 12
                else "***"
            )
        else:
            out[key] = str(value)
    return out


class ClaudeHttpTraceWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._file = self.path.open("a", encoding="utf-8")
        self.count = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cache_read_tokens = 0
        self.total_cache_create_tokens = 0

    async def write(self, record: dict[str, Any]) -> None:
        async with self._lock:
            self._file.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            self._file.flush()
            self.count += 1
            self._update_stats(record)

    def close(self) -> None:
        if not self._file.closed:
            self._file.flush()
            self._file.close()

    def _update_stats(self, record: dict[str, Any]) -> None:
        response_body = record.get("response", {}).get("body", {})
        usage = response_body.get("usage", {}) if isinstance(response_body, dict) else {}
        usage = normalize_usage(usage)
        self.total_input_tokens += int(usage.get("input_tokens") or 0)
        self.total_output_tokens += int(usage.get("output_tokens") or 0)
        self.total_cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
        self.total_cache_create_tokens += int(
            usage.get("cache_creation_input_tokens") or 0
        )


class SSEReassembler:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._buf = b""
        self._current_event: str | None = None
        self._current_data_lines: list[str] = []
        self._snapshot: dict[str, Any] | None = None

    def feed_bytes(self, chunk: bytes) -> None:
        self._buf += chunk
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            self._feed_line(line.decode("utf-8", errors="replace"))

    def _feed_line(self, line: str) -> None:
        line = line.rstrip("\r")
        if line.startswith("event:"):
            self._current_event = line[len("event:") :].strip()
            self._current_data_lines = []
        elif line.startswith("data:"):
            self._current_data_lines.append(line[len("data:") :].strip())
        elif line == "" and (
            self._current_event is not None or self._current_data_lines
        ):
            raw_data = "\n".join(self._current_data_lines)
            try:
                data: Any = json.loads(raw_data)
            except (json.JSONDecodeError, ValueError):
                data = raw_data
            self.add_event(self._current_event or "message", data)
            self._current_event = None
            self._current_data_lines = []

    def add_event(self, event_type: str, data: Any) -> None:
        self.events.append({"event": event_type, "data": data})
        self._accumulate(event_type, data)

    def _accumulate(self, event_type: str, data: Any) -> None:
        if not isinstance(data, dict):
            return

        try:
            if event_type == "message_start":
                self._snapshot = copy.deepcopy(data.get("message", {}))
            elif self._snapshot is None:
                return
            elif event_type == "content_block_start":
                block = copy.deepcopy(data.get("content_block", {}))
                content = self._snapshot.setdefault("content", [])
                index = int(data.get("index", len(content)))
                while len(content) <= index:
                    content.append({})
                content[index] = block
            elif event_type == "content_block_delta":
                index = int(data.get("index", 0))
                content = self._snapshot.get("content", [])
                if index >= len(content):
                    return
                block = content[index]
                delta = data.get("delta", {})
                if delta.get("type") == "text_delta":
                    block["text"] = block.get("text", "") + delta.get("text", "")
                elif delta.get("type") == "thinking_delta":
                    block["thinking"] = block.get("thinking", "") + delta.get(
                        "thinking", ""
                    )
                elif delta.get("type") == "input_json_delta":
                    block["_partial_json"] = block.get("_partial_json", "") + delta.get(
                        "partial_json", ""
                    )
            elif event_type == "content_block_stop":
                index = int(data.get("index", 0))
                content = self._snapshot.get("content", [])
                if index >= len(content):
                    return
                block = content[index]
                partial_json = block.pop("_partial_json", None)
                if isinstance(partial_json, str):
                    try:
                        block["input"] = json.loads(partial_json)
                    except (json.JSONDecodeError, ValueError):
                        block["input"] = partial_json
            elif event_type == "message_delta":
                delta = data.get("delta", {})
                if isinstance(delta, dict):
                    self._snapshot.update(delta)
                usage = data.get("usage", {})
                if isinstance(usage, dict):
                    self._snapshot.setdefault("usage", {}).update(usage)
        except (TypeError, ValueError):
            return

    def reconstruct(self) -> dict[str, Any] | None:
        return self._snapshot


def _parse_body_bytes(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body.decode("utf-8", errors="replace")


def _decode_response_for_trace(resp_bytes: bytes, content_encoding: str) -> bytes:
    if not resp_bytes:
        return resp_bytes
    try:
        if content_encoding == "gzip":
            return gzip.decompress(resp_bytes)
        if content_encoding == "deflate":
            return zlib.decompress(resp_bytes)
    except (OSError, zlib.error):
        return resp_bytes
    return resp_bytes


def _build_trace_record(
    *,
    request_id: str,
    turn: int,
    duration_ms: int,
    method: str,
    path_qs: str,
    request_headers: Any,
    request_body: Any,
    status: int,
    response_headers: Any,
    response_body: Any,
    sse_events: list[dict[str, Any]] | None = None,
    upstream_base_url: str,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "status": status,
        "headers": _filter_headers(response_headers, redact_keys=True),
        "body": response_body,
    }
    if sse_events:
        response["sse_events"] = sse_events

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
        "turn": turn,
        "duration_ms": duration_ms,
        "upstream_base_url": upstream_base_url,
        "request": {
            "method": method,
            "path": path_qs,
            "headers": _filter_headers(request_headers, redact_keys=True),
            "body": request_body,
        },
        "response": response,
    }


async def _proxy_handler(request: web.Request) -> web.StreamResponse:
    if not _is_allowed_claude_api_path(request.path):
        return web.Response(status=404, text="Not Found")

    ctx = request.app[TRACE_CTX_KEY]
    upstream_base_url: str = ctx["upstream_base_url"]
    writer: ClaudeHttpTraceWriter = ctx["writer"]
    session: aiohttp.ClientSession = ctx["session"]
    upstream_url = upstream_base_url.rstrip("/") + "/" + request.path_qs.lstrip("/")
    request_body_bytes = await request.read()
    request_body = _parse_body_bytes(request_body_bytes)
    is_streaming = bool(
        isinstance(request_body, dict) and request_body.get("stream") is True
    )

    request_headers = _filter_headers(request.headers)
    request_headers.pop("Host", None)
    request_headers["Accept-Encoding"] = "identity"
    for key in list(request_headers):
        if key.lower() in {"content-length", "content-encoding"}:
            del request_headers[key]

    ctx["turn_counter"] = int(ctx.get("turn_counter", 0)) + 1
    turn = ctx["turn_counter"]
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    started_at = time.monotonic()

    try:
        upstream_response = await session.request(
            method=request.method,
            url=upstream_url,
            headers=request_headers,
            data=request_body_bytes,
            timeout=aiohttp.ClientTimeout(total=600, sock_read=300),
        )
    except Exception as exc:
        log.warning("claude HTTP trace upstream request failed: %s", exc)
        return web.Response(status=502, text=str(exc))

    if is_streaming and upstream_response.status == 200:
        return await _handle_streaming_response(
            request=request,
            upstream_response=upstream_response,
            writer=writer,
            request_id=request_id,
            turn=turn,
            started_at=started_at,
            request_body=request_body,
            upstream_base_url=upstream_base_url,
        )

    return await _handle_non_streaming_response(
        request=request,
        upstream_response=upstream_response,
        writer=writer,
        request_id=request_id,
        turn=turn,
        started_at=started_at,
        request_body=request_body,
        upstream_base_url=upstream_base_url,
    )


async def _handle_streaming_response(
    *,
    request: web.Request,
    upstream_response: aiohttp.ClientResponse,
    writer: ClaudeHttpTraceWriter,
    request_id: str,
    turn: int,
    started_at: float,
    request_body: Any,
    upstream_base_url: str,
) -> web.StreamResponse:
    response = web.StreamResponse(
        status=upstream_response.status,
        headers=_filter_headers(upstream_response.headers),
    )
    await response.prepare(request)
    reassembler = SSEReassembler()

    try:
        async for chunk in upstream_response.content.iter_any():
            await response.write(chunk)
            reassembler.feed_bytes(chunk)
    except (ConnectionError, asyncio.CancelledError):
        pass

    try:
        await response.write_eof()
    except (ConnectionError, ConnectionResetError):
        pass

    duration_ms = int((time.monotonic() - started_at) * 1000)
    record = _build_trace_record(
        request_id=request_id,
        turn=turn,
        duration_ms=duration_ms,
        method=request.method,
        path_qs=request.path_qs,
        request_headers=request.headers,
        request_body=request_body,
        status=upstream_response.status,
        response_headers=upstream_response.headers,
        response_body=reassembler.reconstruct(),
        sse_events=reassembler.events,
        upstream_base_url=upstream_base_url,
    )
    await writer.write(record)
    return response


async def _handle_non_streaming_response(
    *,
    request: web.Request,
    upstream_response: aiohttp.ClientResponse,
    writer: ClaudeHttpTraceWriter,
    request_id: str,
    turn: int,
    started_at: float,
    request_body: Any,
    upstream_base_url: str,
) -> web.Response:
    response_bytes = await upstream_response.read()
    duration_ms = int((time.monotonic() - started_at) * 1000)
    decoded_bytes = _decode_response_for_trace(
        response_bytes,
        upstream_response.headers.get("Content-Encoding", "").lower(),
    )
    response_body = _parse_body_bytes(decoded_bytes)

    record = _build_trace_record(
        request_id=request_id,
        turn=turn,
        duration_ms=duration_ms,
        method=request.method,
        path_qs=request.path_qs,
        request_headers=request.headers,
        request_body=request_body,
        status=upstream_response.status,
        response_headers=upstream_response.headers,
        response_body=response_body,
        upstream_base_url=upstream_base_url,
    )
    await writer.write(record)

    return web.Response(
        status=upstream_response.status,
        headers=_filter_headers(upstream_response.headers),
        body=response_bytes,
    )


@asynccontextmanager
async def capture_claude_http_traffic(
    *,
    trace_path: Path | None,
    target_url: str | None,
):
    if trace_path is None:
        yield None
        return

    upstream_base_url = resolve_claude_upstream_base_url(target_url)
    trace_writer = ClaudeHttpTraceWriter(trace_path)
    # `trust_env=True` is needed for external model endpoints in this office
    # environment, but it can route loopback test/app servers into HTTP_PROXY.
    session = aiohttp.ClientSession(
        auto_decompress=False,
        trust_env=not _uses_loopback_host(upstream_base_url),
    )
    app = web.Application(client_max_size=0)
    app[TRACE_CTX_KEY] = {
        "upstream_base_url": upstream_base_url,
        "writer": trace_writer,
        "session": session,
        "turn_counter": 0,
    }
    app.router.add_route("*", "/{path_info:.*}", _proxy_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    try:
        actual_port = site._server.sockets[0].getsockname()[1]
    except (AttributeError, IndexError, OSError) as exc:
        await runner.cleanup()
        await session.close()
        trace_writer.close()
        raise RuntimeError("failed to determine claude HTTP trace proxy port") from exc

    try:
        yield ClaudeHttpTraceProxy(
            base_url=f"http://127.0.0.1:{actual_port}",
            upstream_base_url=upstream_base_url,
            trace_path=trace_path,
        )
    finally:
        await runner.cleanup()
        await session.close()
        trace_writer.close()


def http_trace_html_path(trace_path: Path) -> Path:
    if trace_path.name.endswith(".http.jsonl"):
        return trace_path.with_name(trace_path.name[: -len(".jsonl")] + ".html")
    return trace_path.with_suffix(".html")


def generate_claude_http_trace_html(trace_path: Path, html_path: Path | None = None) -> Path:
    target_path = html_path or http_trace_html_path(trace_path)
    from src.utils.claude_http_viewer import _generate_html_viewer

    _generate_html_viewer(trace_path, target_path)
    return target_path
