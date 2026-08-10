from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from claude_agent_sdk import query
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookCallback,
    HookMatcher,
    McpStdioServerConfig,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    ToolPermissionContext,
)
from src.config import HarnessConfig
from src.orchestration.pricing import estimate_cost_usd
from src.utils.bash_policy import (
    validate_bash_command,
    validate_bash_command_readonly,
)
from src.utils.claude_http_trace import (
    capture_claude_http_traffic,
    generate_claude_http_trace_html,
    resolve_claude_upstream_base_url,
)
from src.utils.sdk_session import _clear_current_task_cancellation

LOCAL_AGENT_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS"}
LOCAL_AGENT_TOOLS_WITH_BASH = LOCAL_AGENT_TOOLS | {"Bash"}
PLAYWRIGHT_TOOL_PREFIX = "mcp__playwright__"
_CLAUDE_SDK_SKIP_VERSION_CHECK = "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK"
log = logging.getLogger(__name__)
_DEFAULT_CLAUDE_CODE_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
    "DISABLE_INSTALLATION_CHECKS": "1",
    "DISABLE_TELEMETRY": "1",
}

# Playwright MCP 浏览器只允许访问本机回环地址，避免被提示词注入后
# 探测 file://、云元数据地址或其他本地端口。
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
    """保持 SDK 输入流存活到首个结果到达，规避单轮流式请求提前关流。"""
    return {"continue_": True}


def make_bash_pretool_hook(*, bash_profile: str = "full"):
    """通过 PreToolUse 校验每一次 Bash 调用，避免 allowedTools 旁路。"""
    if bash_profile not in {"full", "read_only"}:
        raise ValueError(f"unsupported bash_profile: {bash_profile!r}")

    async def _hook(
        input_data: Any,
        _tool_use_id: str | None,
        _context: Any,
    ) -> dict[str, Any]:
        tool_name = ""
        tool_input: dict[str, Any] = {}
        if isinstance(input_data, dict):
            tool_name = str(input_data.get("tool_name", ""))
            raw_input = input_data.get("tool_input", {})
            if isinstance(raw_input, dict):
                tool_input = raw_input

        if tool_name != "Bash":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            }

        command = str(tool_input.get("command", ""))
        try:
            if bash_profile == "read_only":
                validate_bash_command_readonly(command)
            else:
                validate_bash_command(command)
        except ValueError as exc:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": str(exc),
                }
            }

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }

    return _hook


class SdkTraceWriter:
    """顺序写入 SDK 运行轨迹的轻量封装。"""

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


def _write_permission_trace(
    trace_writer: SdkTraceWriter | None,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    decision: str,
    message: str | None = None,
) -> None:
    """统一记录工具权限判定，避免多处重复拼装 payload。"""
    if trace_writer is None:
        return

    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "decision": decision,
    }
    if message is not None:
        payload["message"] = message
    trace_writer.write("permission_check", payload)


def build_playwright_mcp_args(config: HarnessConfig) -> list[str]:
    """按配置拼装 Playwright MCP 的启动参数。"""
    args = ["@playwright/mcp@latest", "--isolated"]
    if config.playwright_headless:
        args.append("--headless")
    return args


def _extract_text_from_assistant_message(message: AssistantMessage) -> str:
    """提取 assistant 消息中的纯文本内容。"""
    parts: list[str] = []
    for block in message.content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


@contextmanager
def _temporary_env_var(name: str, value: str):
    """临时覆盖环境变量，并在退出时恢复原值。"""
    original = os.environ.get(name)
    had_original = name in os.environ
    os.environ[name] = value
    try:
        yield
    finally:
        if had_original:
            os.environ[name] = original or ""
        else:
            os.environ.pop(name, None)


def _summarize_content_blocks(blocks: list[Any]) -> list[dict[str, Any]]:
    """将 SDK content blocks 压缩为适合写 trace 的摘要结构。"""
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
    """将不同类型的 SDK 消息转为可 JSON 序列化的字典。"""
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


def build_agent_run_stats(
    result_message: ResultMessage, *, model: str
) -> AgentRunStats:
    """基于 CLI 的 `ResultMessage` 构建统一统计结构。

    成本统一按本地价格表计算，避免依赖 CLI 自带的 Claude 专用估算。
    """
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
        cost_usd=estimate_cost_usd(model, token_usage),
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
    """解析并校验路径，确保目标仍位于当前 workdir 内。"""
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


def _collect_candidate_paths(value: Any) -> Iterable[str]:
    """递归收集工具参数中疑似路径的字符串字段。"""
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
    """递归收集 Playwright 工具参数中的 URL 字段。"""
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
    """限制 Playwright 仅访问约定端口上的回环地址。"""
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
    bash_profile: str = "full",
    trace_writer: SdkTraceWriter | None = None,
    frontend_port: int = 5173,
    mutation_policy: Any | None = None,
):
    if bash_profile not in {"full", "read_only"}:
        raise ValueError(f"unsupported bash_profile: {bash_profile!r}")

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
                _write_permission_trace(
                    trace_writer,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    decision="deny",
                    message=str(exc),
                )
                return PermissionResultDeny(message=str(exc))
            _write_permission_trace(
                trace_writer,
                tool_name=tool_name,
                tool_input=tool_input,
                decision="allow",
            )
            return PermissionResultAllow()

        allowed_tools = LOCAL_AGENT_TOOLS_WITH_BASH if allow_bash else LOCAL_AGENT_TOOLS
        if tool_name not in allowed_tools:
            message = f"Tool not allowed in harness: {tool_name}"
            _write_permission_trace(
                trace_writer,
                tool_name=tool_name,
                tool_input=tool_input,
                decision="deny",
                message=message,
            )
            return PermissionResultDeny(message=message)

        if tool_name == "Bash":
            try:
                if bash_profile == "read_only":
                    validate_bash_command_readonly(tool_input.get("command", ""))
                else:
                    validate_bash_command(tool_input.get("command", ""))
            except ValueError as exc:
                _write_permission_trace(
                    trace_writer,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    decision="deny",
                    message=str(exc),
                )
                return PermissionResultDeny(message=str(exc))
            if mutation_policy is not None:
                denial = mutation_policy.check(tool_name, tool_input)
                if denial:
                    _write_permission_trace(
                        trace_writer,
                        tool_name=tool_name,
                        tool_input=tool_input,
                        decision="deny",
                        message=str(denial),
                    )
                    return PermissionResultDeny(message=str(denial))
            _write_permission_trace(
                trace_writer,
                tool_name=tool_name,
                tool_input=tool_input,
                decision="allow",
            )
            return PermissionResultAllow()

        try:
            for candidate in _collect_candidate_paths(tool_input):
                _resolve_path(candidate, workdir)
        except ValueError as exc:
            _write_permission_trace(
                trace_writer,
                tool_name=tool_name,
                tool_input=tool_input,
                decision="deny",
                message=str(exc),
            )
            return PermissionResultDeny(message=str(exc))

        if mutation_policy is not None:
            denial = mutation_policy.check(tool_name, tool_input)
            if denial:
                _write_permission_trace(
                    trace_writer,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    decision="deny",
                    message=str(denial),
                )
                return PermissionResultDeny(message=str(denial))

        _write_permission_trace(
            trace_writer,
            tool_name=tool_name,
            tool_input=tool_input,
            decision="allow",
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
    bash_profile: str = "full",
    stop_hooks: list[HookCallback] | None = None,
    trace_writer: SdkTraceWriter | None = None,
    anthropic_base_url_override: str | None = None,
    mutation_policy: Any | None = None,
) -> ClaudeAgentOptions:
    """按 harness 约束组装单个 agent 的 ClaudeAgentOptions。"""
    mcp_servers: dict[str, McpStdioServerConfig] = {}
    if allow_playwright:
        mcp_servers["playwright"] = {
            "command": "npx",
            "args": build_playwright_mcp_args(config),
        }

    allowed_tools = sorted(LOCAL_AGENT_TOOLS_WITH_BASH if allow_bash else LOCAL_AGENT_TOOLS)
    resolved_stop_hooks = [_keepalive_hook, *(stop_hooks or [])]
    hooks: dict[str, list[HookMatcher]] = {
        "Stop": [HookMatcher(hooks=resolved_stop_hooks)],
    }
    if allow_bash:
        # PreToolUse 对所有工具调用都会触发，适合拦截 Bash 白名单内的实际执行。
        hooks["PreToolUse"] = [
            HookMatcher(
                matcher="Bash",
                hooks=[make_bash_pretool_hook(bash_profile=bash_profile)],
            )
        ]

    env = dict(_DEFAULT_CLAUDE_CODE_ENV)
    if config.api_key:
        env["ANTHROPIC_API_KEY"] = config.api_key
    target_base_url = anthropic_base_url_override
    if target_base_url is None:
        target_base_url = config.base_url
    if target_base_url:
        env["ANTHROPIC_BASE_URL"] = target_base_url

    normalized_model = model.strip().lower()
    sdk_system_prompt: str | dict[str, str]
    if not normalized_model.startswith("qwen"):
        sdk_system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": system_prompt,
        }
    else:
        # OpenAI-compatible models should not inherit Claude Code-specific
        # worktree, attribution, or product-behavior instructions.
        sdk_system_prompt = system_prompt

    return ClaudeAgentOptions(
        model=model,
        system_prompt=sdk_system_prompt,
        cwd=workdir,
        max_turns=max_turns,
        allowed_tools=allowed_tools,
        permission_mode="default",
        can_use_tool=make_tool_permission_callback(
            workdir=workdir,
            allow_bash=allow_bash,
            allow_playwright=allow_playwright,
            bash_profile=bash_profile,
            trace_writer=trace_writer,
            frontend_port=config.frontend_port,
            mutation_policy=mutation_policy,
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
    bash_profile: str = "full",
    stop_hooks: list[HookCallback] | None = None,
    trace_path: Path | None = None,
    mutation_policy: Any | None = None,
) -> tuple[ResultMessage, float, str, list[Any]]:
    """运行单个 SDK agent，并统一收集文本、权限拒绝与成本信息。"""
    runtime = config.agent_runtime.strip().lower()
    if runtime not in {"auto", "claude", "openai"}:
        raise ValueError(f"unsupported AGENT_RUNTIME: {config.agent_runtime!r}")
    normalized_model = model.strip().lower()
    openai_model_prefixes = ("deepseek", "qwen", "gpt-", "o1", "o3", "o4")
    if runtime == "openai" or (
        runtime == "auto" and normalized_model.startswith(openai_model_prefixes)
    ):
        from src.agents.openai_runner import run_openai_agent

        return await run_openai_agent(
            prompt=prompt,
            config=config,
            workdir=workdir,
            model=model,
            system_prompt=system_prompt,
            max_turns=max_turns,
            allow_bash=allow_bash,
            allow_playwright=allow_playwright,
            bash_profile=bash_profile,
            stop_hooks=stop_hooks,
            trace_path=trace_path,
            mutation_policy=mutation_policy,
        )
    trace_writer = SdkTraceWriter(trace_path) if trace_path else None
    http_trace_path = trace_path.with_suffix(".http.jsonl") if trace_path else None
    upstream_base_url = resolve_claude_upstream_base_url(config.base_url)

    async def _run_once(
        *,
        anthropic_base_url_override: str | None,
    ) -> tuple[ResultMessage, float, str, list[Any]]:
        options = build_agent_options(
            config=config,
            workdir=workdir,
            model=model,
            system_prompt=system_prompt,
            max_turns=max_turns,
            allow_bash=allow_bash,
            allow_playwright=allow_playwright,
            bash_profile=bash_profile,
            stop_hooks=stop_hooks,
            trace_writer=trace_writer,
            anthropic_base_url_override=anthropic_base_url_override,
            mutation_policy=mutation_policy,
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
                    "upstream_base_url": upstream_base_url,
                    "http_trace_path": str(http_trace_path) if http_trace_path else None,
                    "proxy_base_url": anthropic_base_url_override,
                },
            )

        result_message: ResultMessage | None = None
        last_assistant_text = ""
        permission_denials: list[Any] = []

        # claude-agent-sdk 0.1.65 的 `_check_claude_version()` 可能在 anyio
        # 版本检查分支泄漏 CancelledError，导致 generator/build 被误判失败。
        # 版本要求已由依赖与本地 CLI 管理，这里跳过该额外检查，避免连接前中断。
        with _temporary_env_var(_CLAUDE_SDK_SKIP_VERSION_CHECK, "1"):
            stream = query(prompt=_single_prompt_stream(prompt), options=options)
            try:
                async for message in stream:
                    if trace_writer:
                        trace_writer.write("sdk_message", _serialize_sdk_message(message))
                    if isinstance(message, ResultMessage):
                        result_message = message
                        permission_denials = list(message.permission_denials or [])
                        # 部分 SDK 后端在发出最终 ResultMessage 后仍会短暂保留流。
                        # 这里以 ResultMessage 为准，避免 harness 误以为仍在运行。
                        break
                    if isinstance(message, AssistantMessage):
                        text = _extract_text_from_assistant_message(message)
                        if text:
                            last_assistant_text = text
            finally:
                aclose = getattr(stream, "aclose", None)
                if callable(aclose):
                    # 关闭动作留在子任务边界内，避免 athrow 取消持续污染 harness 主协程。
                    close_task = asyncio.create_task(aclose(), name="sdk_stream_close")
                    try:
                        suppressed = 0
                        while True:
                            try:
                                await asyncio.shield(close_task)
                                break
                            except asyncio.CancelledError:
                                cleared = _clear_current_task_cancellation()
                                suppressed += max(cleared, 1)
                                if result_message is None:
                                    raise
                                if close_task.done():
                                    break
                        if close_task.cancelled():
                            if result_message is None:
                                raise RuntimeError("Agent SDK stream close task was cancelled")
                            if trace_writer:
                                trace_writer.write(
                                    "stream_close_cancelled",
                                    {
                                        "suppressed": True,
                                        "task_cancelled": True,
                                        "cleared": suppressed,
                                    },
                                )
                        else:
                            close_exc = close_task.exception()
                            if close_exc is not None:
                                raise close_exc
                            if suppressed > 0 and trace_writer:
                                trace_writer.write(
                                    "stream_close_cancelled",
                                    {
                                        "suppressed": True,
                                        "task_cancelled": False,
                                        "cleared": suppressed,
                                    },
                                )
                    finally:
                        if not close_task.done():
                            close_task.cancel()

        if result_message is None:
            raise RuntimeError("Agent SDK returned no result message")
        if result_message.is_error:
            details = "; ".join(result_message.errors or [])
            raise RuntimeError(result_message.result or details or "Agent SDK run failed")

        stats = build_agent_run_stats(result_message, model=model)
        cost_usd = stats.cost_usd

        if trace_writer:
            trace_writer.write(
                "run_complete",
                {
                    "total_cost_usd": cost_usd,
                    "result": result_message.result,
                    "last_assistant_text": last_assistant_text,
                    "permission_denials": permission_denials,
                },
            )

        return (
            result_message,
            cost_usd,
            last_assistant_text,
            permission_denials,
        )

    async with capture_claude_http_traffic(
        trace_path=http_trace_path,
        target_url=upstream_base_url,
    ) as http_trace_proxy:
        proxy_base_url = (
            http_trace_proxy.base_url if http_trace_proxy is not None else None
        )
        agent_task = asyncio.create_task(
            _run_once(anthropic_base_url_override=proxy_base_url),
            name="run_sdk_agent",
        )
        run_result: tuple[ResultMessage, float, str, list[Any]] | None = None
        try:
            while True:
                try:
                    run_result = await asyncio.shield(agent_task)
                    break
                except asyncio.CancelledError:
                    cleared = _clear_current_task_cancellation()
                    if cleared == 0:
                        raise
                    if trace_writer:
                        trace_writer.write(
                            "run_cancelled_parent",
                            {
                                "cleared": cleared,
                                "task_done": agent_task.done(),
                            },
                        )
                    if agent_task.done():
                        break

            if run_result is None:
                if agent_task.cancelled():
                    raise RuntimeError("Agent SDK task was cancelled before completion")

                task_exc = agent_task.exception()
                if task_exc is not None:
                    raise task_exc
                run_result = agent_task.result()
        finally:
            if not agent_task.done():
                agent_task.cancel()

    if http_trace_path is not None:
        try:
            http_trace_html_path = generate_claude_http_trace_html(http_trace_path)
        except Exception as exc:
            log.warning("failed to generate Claude HTTP trace HTML: %s", exc)
        else:
            if trace_writer:
                trace_writer.write(
                    "http_trace_html_generated",
                    {
                        "http_trace_path": str(http_trace_path),
                        "http_trace_html_path": str(http_trace_html_path),
                    },
                )

    if run_result is None:
        raise RuntimeError("Agent SDK returned no result")
    return run_result
