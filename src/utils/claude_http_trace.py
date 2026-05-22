from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"


@dataclass(frozen=True)
class ClaudeHttpTraceProxy:
    base_url: str
    upstream_base_url: str
    trace_path: Path


def resolve_claude_upstream_base_url(base_url: str | None) -> str:
    normalized = (base_url or "").strip()
    return normalized or DEFAULT_ANTHROPIC_BASE_URL


@lru_cache(maxsize=1)
def _load_claude_tap_symbols() -> tuple[Any, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    vendored_root = repo_root / "claude-tap"
    if not vendored_root.exists():
        raise RuntimeError(
            f"vendored claude-tap directory not found: {vendored_root}"
        )

    vendored_root_str = str(vendored_root)
    if vendored_root_str not in sys.path:
        sys.path.insert(0, vendored_root_str)

    from claude_tap.proxy import proxy_handler
    from claude_tap.trace import TraceWriter

    return proxy_handler, TraceWriter


@asynccontextmanager
async def capture_claude_http_traffic(
    *,
    trace_path: Path | None,
    target_url: str | None,
):
    if trace_path is None:
        yield None
        return

    proxy_handler, trace_writer_type = _load_claude_tap_symbols()
    upstream_base_url = resolve_claude_upstream_base_url(target_url)
    trace_writer = trace_writer_type(trace_path)
    session = aiohttp.ClientSession(auto_decompress=False, trust_env=True)
    app = web.Application(client_max_size=0)
    app["trace_ctx"] = {
        "target_url": upstream_base_url,
        "writer": trace_writer,
        "session": session,
        "turn_counter": 0,
        "extra_allowed_path_prefixes": (),
        "strip_path_prefix": "",
        "force_http": False,
    }
    app.router.add_route("*", "/{path_info:.*}", proxy_handler)

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
