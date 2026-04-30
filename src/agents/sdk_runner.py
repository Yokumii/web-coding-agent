from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from claude_agent_sdk import query
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    McpStdioServerConfig,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    ToolPermissionContext,
)
from src.config import HarnessConfig

LOCAL_AGENT_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS"}
LOCAL_AGENT_TOOLS_WITH_BASH = LOCAL_AGENT_TOOLS | {"Bash"}
PLAYWRIGHT_TOOL_PREFIX = "mcp__playwright__"
_DISALLOWED_SHELL_SNIPPETS = ("&&", "||", "|", ";", ">", "<", "$(", "`", "\n", "\r")
# Token check kept for documentation; the real gate is the allowlist below.
_DISALLOWED_BASH_COMMANDS = {
    "rm",
    "rmdir",
    "sudo",
    "chmod",
    "chown",
    "kill",
    "pkill",
    "open",
    "xdg-open",
    "curl",
    "wget",
    "ssh",
    "scp",
}
_ALLOWED_BASH_COMMANDS = {
    "cat",
    "cp",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "mkdir",
    "mv",
    "node",
    "npm",
    "npx",
    "pnpm",
    "pwd",
    "pytest",
    "python",
    "python3",
    "rg",
    "sed",
    "tail",
    "touch",
    "tsc",
    "uv",
    "uvicorn",
    "vite",
    "wc",
    "which",
    "yarn",
}

# git subcommands the harness accepts. Anything that mutates remotes,
# config, or pulls foreign code is blocked (audit M7).
_GIT_ALLOWED_SUBCOMMANDS = frozenset({
    "status",
    "diff",
    "log",
    "show",
    "add",
    "commit",
    "rev-parse",
    "branch",
    "ls-files",
    "stash",
})

# find flags that turn the binary into an arbitrary executor or a
# destructive bulk delete (audit M8). `-print0` and `-fls` also stream
# arbitrary content into outputs we don't want to expose.
_FORBIDDEN_FIND_FLAGS = frozenset({
    "-exec",
    "-execdir",
    "-delete",
    "-print0",
    "-fprint",
    "-fprintf",
    "-fprint0",
    "-fls",
    "-ok",
    "-okdir",
})

# Hosts that the playwright MCP browser may navigate to. Anything else
# (file://, private network ranges, cloud metadata) is denied to keep
# a prompt-injected evaluator from doing SSRF or local file reads
# (audit H3).
_PLAYWRIGHT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_PLAYWRIGHT_URL_KEYS = frozenset({"url", "urls"})


@dataclass(frozen=True)
class AgentRunStats:
    cost_usd: float
    duration_ms: int | None
    duration_api_ms: int | None
    token_usage: dict[str, int]
    usage: dict[str, Any]
    model_usage: dict[str, Any]
    wall_duration_ms: int | None = None

    def with_wall_duration(self, wall_duration_ms: int) -> "AgentRunStats":
        return replace(self, wall_duration_ms=wall_duration_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "duration_api_ms": self.duration_api_ms,
            "wall_duration_ms": self.wall_duration_ms,
            "token_usage": self.token_usage,
            "usage": self.usage,
            "model_usage": self.model_usage,
        }


async def _keepalive_hook(_input: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
    """No-op hook to keep the SDK stdin open until the first result arrives.

    The current Python SDK only delays `end_input()` when hooks or SDK MCP servers
    are configured. `can_use_tool` alone is not enough, which causes permission
    requests to fail with `Stream closed` on single-shot streamed prompts.
    """
    return {"continue_": True}


class SdkTraceWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, payload: dict[str, Any]) -> None:
        record = {"event": event, **payload}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _make_stderr_callback(trace_writer: SdkTraceWriter | None):
    if trace_writer is None:
        return None

    def _stderr(line: str) -> None:
        trace_writer.write("sdk_stderr", {"line": line})

    return _stderr


def build_playwright_mcp_args(config: HarnessConfig) -> list[str]:
    args = ["@playwright/mcp@latest", "--isolated"]
    if config.playwright_headless:
        args.append("--headless")
    return args


def _extract_text_from_assistant_message(message: AssistantMessage) -> str:
    parts: list[str] = []
    for block in message.content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


def _summarize_content_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for block in blocks:
        summary: dict[str, Any] = {"block_type": type(block).__name__}
        for attr in ("text", "name", "id", "input", "content"):
            value = getattr(block, attr, None)
            if value is not None:
                summary[attr] = value
        summaries.append(summary)
    return summaries


def _serialize_sdk_message(message: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"message_type": type(message).__name__}
    for attr in (
        "subtype",
        "session_id",
        "uuid",
        "stop_reason",
        "result",
        "is_error",
        "total_cost_usd",
        "permission_denials",
        "errors",
        "event",
        "data",
        "rate_limit_info",
    ):
        value = getattr(message, attr, None)
        if value is not None:
            payload[attr] = value

    content = getattr(message, "content", None)
    if content is not None:
        payload["content"] = _summarize_content_blocks(content)
    return payload


def _collect_token_usage(source: dict[str, Any], output: dict[str, int]) -> None:
    for key, value in source.items():
        if isinstance(value, int) and "token" in key:
            output[key] = value
        elif isinstance(value, dict):
            _collect_token_usage(value, output)


def build_agent_run_stats(result_message: ResultMessage) -> AgentRunStats:
    usage = result_message.usage if isinstance(result_message.usage, dict) else {}
    model_usage = result_message.model_usage if isinstance(result_message.model_usage, dict) else {}
    token_usage: dict[str, int] = {}

    for key in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        for source in (usage, model_usage):
            value = source.get(key)
            if isinstance(value, int):
                token_usage[key] = value
                break

    for source in (usage, model_usage):
        _collect_token_usage(source, token_usage)

    return AgentRunStats(
        cost_usd=float(result_message.total_cost_usd or 0.0),
        duration_ms=result_message.duration_ms,
        duration_api_ms=result_message.duration_api_ms,
        token_usage=token_usage,
        usage=usage.copy(),
        model_usage=model_usage.copy(),
    )


async def _single_prompt_stream(prompt: str):
    yield {
        "type": "user",
        "session_id": "",
        "message": {
            "role": "user",
            "content": prompt,
        },
        "parent_tool_use_id": None,
    }


def _resolve_path(path: str, workdir: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workdir / candidate
    resolved = candidate.resolve()
    workdir_resolved = workdir.resolve()
    try:
        resolved.relative_to(workdir_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes workdir: {path}") from exc
    return resolved


def _validate_bash_command(command: str) -> list[str]:
    stripped = command.strip()
    if not stripped:
        raise ValueError("empty command")

    for snippet in _DISALLOWED_SHELL_SNIPPETS:
        if snippet in stripped:
            raise ValueError(f"shell control operator not allowed: {snippet}")

    argv = shlex.split(stripped)
    if not argv:
        raise ValueError("empty command")

    executable = argv[0]
    if executable in _DISALLOWED_BASH_COMMANDS:
        raise ValueError(f"command not allowed: {executable}")
    if executable not in _ALLOWED_BASH_COMMANDS:
        raise ValueError(f"command not in allowlist: {executable}")

    if executable == "git":
        _validate_git_argv(argv)
    elif executable == "find":
        _validate_find_argv(argv)

    for token in argv[1:]:
        if token.startswith("~"):
            raise ValueError(f"path shortcuts not allowed in bash command: {token}")
        token_path = Path(token)
        if token_path.is_absolute():
            raise ValueError(f"absolute paths not allowed in bash command: {token}")
        if ".." in token_path.parts:
            raise ValueError(f"path escapes workdir in bash command: {token}")

    return argv


def _validate_git_argv(argv: list[str]) -> None:
    if len(argv) < 2:
        raise ValueError("git requires a subcommand")
    # Reject leading flags like `git -c http.extraheader=...` that smuggle
    # configuration past the subcommand check.
    if argv[1].startswith("-"):
        raise ValueError(f"git flags before the subcommand are not allowed: {argv[1]}")
    subcommand = argv[1]
    if subcommand not in _GIT_ALLOWED_SUBCOMMANDS:
        raise ValueError(f"git subcommand not allowed: {subcommand}")


def _validate_find_argv(argv: list[str]) -> None:
    for token in argv[1:]:
        if token in _FORBIDDEN_FIND_FLAGS:
            raise ValueError(f"find flag not allowed: {token}")
        # `-fprint*` family — catch any variant.
        if token.startswith("-fprint"):
            raise ValueError(f"find flag not allowed: {token}")


def _collect_candidate_paths(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and ("path" in key.lower() or key.lower() == "file"):
                yield item
            else:
                yield from _collect_candidate_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from _collect_candidate_paths(item)


def _collect_playwright_urls(value: Any, key_path: tuple[str, ...] = ()) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            new_path = key_path + (str(key).lower(),)
            if isinstance(item, str) and key.lower() in _PLAYWRIGHT_URL_KEYS:
                yield item
            elif isinstance(item, list) and key.lower() in _PLAYWRIGHT_URL_KEYS:
                for entry in item:
                    if isinstance(entry, str):
                        yield entry
            else:
                yield from _collect_playwright_urls(item, new_path)
    elif isinstance(value, list):
        for item in value:
            yield from _collect_playwright_urls(item, key_path)


def _validate_playwright_url(url: str, frontend_port: int) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            f"playwright tool denied: only http(s) URLs allowed, got scheme {parsed.scheme!r}"
        )
    host = parsed.hostname
    if host not in _PLAYWRIGHT_ALLOWED_HOSTS:
        raise ValueError(
            f"playwright tool denied: host {host!r} is not on the loopback allowlist"
        )
    if parsed.port is not None and parsed.port != frontend_port:
        raise ValueError(
            f"playwright tool denied: port {parsed.port} is not the frontend dev server port "
            f"({frontend_port})"
        )


def make_tool_permission_callback(
    *,
    workdir: Path,
    allow_bash: bool,
    allow_playwright: bool,
    trace_writer: SdkTraceWriter | None = None,
    frontend_port: int = 5173,
):
    async def _can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        del context

        if allow_playwright and tool_name.startswith(PLAYWRIGHT_TOOL_PREFIX):
            try:
                for candidate_url in _collect_playwright_urls(tool_input):
                    _validate_playwright_url(candidate_url, frontend_port)
            except ValueError as exc:
                if trace_writer:
                    trace_writer.write(
                        "permission_check",
                        {
                            "tool_name": tool_name,
                            "tool_input": tool_input,
                            "decision": "deny",
                            "message": str(exc),
                        },
                    )
                return PermissionResultDeny(message=str(exc))
            if trace_writer:
                trace_writer.write(
                    "permission_check",
                    {"tool_name": tool_name, "tool_input": tool_input, "decision": "allow"},
                )
            return PermissionResultAllow()

        allowed_tools = LOCAL_AGENT_TOOLS_WITH_BASH if allow_bash else LOCAL_AGENT_TOOLS
        if tool_name not in allowed_tools:
            message = f"Tool not allowed in harness: {tool_name}"
            if trace_writer:
                trace_writer.write(
                    "permission_check",
                    {
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "decision": "deny",
                        "message": message,
                    },
                )
            return PermissionResultDeny(message=message)

        if tool_name == "Bash":
            try:
                _validate_bash_command(tool_input.get("command", ""))
            except ValueError as exc:
                if trace_writer:
                    trace_writer.write(
                        "permission_check",
                        {
                            "tool_name": tool_name,
                            "tool_input": tool_input,
                            "decision": "deny",
                            "message": str(exc),
                        },
                    )
                return PermissionResultDeny(message=str(exc))
            if trace_writer:
                trace_writer.write(
                    "permission_check",
                    {"tool_name": tool_name, "tool_input": tool_input, "decision": "allow"},
                )
            return PermissionResultAllow()

        try:
            for candidate in _collect_candidate_paths(tool_input):
                _resolve_path(candidate, workdir)
        except ValueError as exc:
            if trace_writer:
                trace_writer.write(
                    "permission_check",
                    {
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "decision": "deny",
                        "message": str(exc),
                    },
                )
            return PermissionResultDeny(message=str(exc))

        if trace_writer:
            trace_writer.write(
                "permission_check",
                {"tool_name": tool_name, "tool_input": tool_input, "decision": "allow"},
            )
        return PermissionResultAllow()

    return _can_use_tool


def build_agent_options(
    *,
    config: HarnessConfig,
    workdir: Path,
    model: str,
    system_prompt: str,
    max_turns: int,
    allow_bash: bool,
    allow_playwright: bool = False,
    trace_writer: SdkTraceWriter | None = None,
) -> ClaudeAgentOptions:
    mcp_servers: dict[str, McpStdioServerConfig] = {}
    if allow_playwright:
        mcp_servers["playwright"] = {
            "command": "npx",
            "args": build_playwright_mcp_args(config),
        }

    allowed_tools = sorted(LOCAL_AGENT_TOOLS_WITH_BASH if allow_bash else LOCAL_AGENT_TOOLS)
    hooks = {"Stop": [HookMatcher(hooks=[_keepalive_hook])]}

    env = {}
    if config.api_key:
        env["ANTHROPIC_API_KEY"] = config.api_key
    if config.base_url:
        env["ANTHROPIC_BASE_URL"] = config.base_url

    return ClaudeAgentOptions(
        model=model,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        },
        cwd=workdir,
        max_turns=max_turns,
        allowed_tools=allowed_tools,
        permission_mode="default",
        can_use_tool=make_tool_permission_callback(
            workdir=workdir,
            allow_bash=allow_bash,
            allow_playwright=allow_playwright,
            trace_writer=trace_writer,
            frontend_port=config.frontend_port,
        ),
        hooks=hooks,
        mcp_servers=mcp_servers,
        setting_sources=[],
        env=env,
        max_buffer_size=config.sdk_max_buffer_size,
        stderr=_make_stderr_callback(trace_writer),
    )


async def run_sdk_agent(
    *,
    prompt: str,
    config: HarnessConfig,
    workdir: Path,
    model: str,
    system_prompt: str,
    max_turns: int,
    allow_bash: bool,
    allow_playwright: bool = False,
    trace_path: Path | None = None,
) -> tuple[ResultMessage, float, str, list[Any]]:
    trace_writer = SdkTraceWriter(trace_path) if trace_path else None
    options = build_agent_options(
        config=config,
        workdir=workdir,
        model=model,
        system_prompt=system_prompt,
        max_turns=max_turns,
        allow_bash=allow_bash,
        allow_playwright=allow_playwright,
        trace_writer=trace_writer,
    )
    if trace_writer:
        trace_writer.write(
            "run_start",
            {
                "model": model,
                "cwd": str(workdir),
                "max_turns": max_turns,
                "allow_bash": allow_bash,
                "allow_playwright": allow_playwright,
                "allowed_tools": options.allowed_tools,
                "prompt": prompt,
            },
    )

    result_message: ResultMessage | None = None
    last_assistant_text = ""
    permission_denials: list[Any] = []
    async for message in query(prompt=_single_prompt_stream(prompt), options=options):
        if trace_writer:
            trace_writer.write("sdk_message", _serialize_sdk_message(message))
        if isinstance(message, ResultMessage):
            result_message = message
            permission_denials = list(message.permission_denials or [])
        elif isinstance(message, AssistantMessage):
            text = _extract_text_from_assistant_message(message)
            if text:
                last_assistant_text = text

    if result_message is None:
        raise RuntimeError("Agent SDK returned no result message")
    if result_message.is_error:
        details = "; ".join(result_message.errors or [])
        raise RuntimeError(result_message.result or details or "Agent SDK run failed")

    if trace_writer:
        trace_writer.write(
            "run_complete",
            {
                "total_cost_usd": float(result_message.total_cost_usd or 0.0),
                "result": result_message.result,
                "last_assistant_text": last_assistant_text,
                "permission_denials": permission_denials,
            },
        )

    return (
        result_message,
        float(result_message.total_cost_usd or 0.0),
        last_assistant_text,
        permission_denials,
    )
