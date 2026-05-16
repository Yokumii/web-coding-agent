from __future__ import annotations

import asyncio
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
from src.orchestration.pricing import estimate_cost_usd

LOCAL_AGENT_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS"}
LOCAL_AGENT_TOOLS_WITH_BASH = LOCAL_AGENT_TOOLS | {"Bash"}
PLAYWRIGHT_TOOL_PREFIX = "mcp__playwright__"
_DISALLOWED_SHELL_SNIPPETS = ("&&", "||", "|", ";", ">", "<", "$(", "`", "&", "\n", "\r")
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
# config, or pulls foreign code is blocked.
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

# Subset of git subcommands available to the read-only bash profile
# (the evaluator). Mutators removed: add, commit, stash.
_GIT_READONLY_SUBCOMMANDS = frozenset({
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "branch",
    "ls-files",
})

# Bash executables permitted in the read-only profile. Excludes anything
# that can mutate files (cp/mv/touch/mkdir/sed) or run arbitrary code via
# build/test entrypoints (pytest/vite/uv/uvicorn/tsc).
_READONLY_ALLOWED_BASH_COMMANDS = frozenset({
    "cat",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "node",
    "npm",
    "npx",
    "pnpm",
    "pwd",
    "python",
    "python3",
    "rg",
    "tail",
    "wc",
    "which",
    "yarn",
})

# Interpreter flags that smuggle inline executable code in the read-only
# profile. The evaluator may invoke ``python3 -m json.tool path.json`` but
# not ``python3 -c "open('x','w').write('owned')"``.
_INTERPRETER_INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--eval", "-i"})

# Subcommands accepted for npm/pnpm/yarn/npx in the read-only profile.
# All install/build/test/run flows are denied because they execute
# arbitrary scripts and write to node_modules / dist.
_PKG_MANAGER_READONLY_SUBCOMMANDS = frozenset({
    "list",
    "ls",
    "view",
    "info",
    "outdated",
})

# find flags that turn the binary into an arbitrary executor or a
# destructive bulk delete. `-print0` and `-fls` also stream
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
# a prompt-injected evaluator from doing SSRF or local file reads.
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


def make_bash_pretool_hook(*, bash_profile: str = "full"):
    """Validate every Bash invocation via PreToolUse, regardless of allowedTools.

    The Claude CLI auto-allows tools listed in ``--allowedTools`` and never asks
    ``can_use_tool``. The generator runs with Bash in that allowlist, so the
    legacy ``_validate_bash_command`` gate never fired for it (verified by
    counting ``permission_check`` events in real run traces — always 0).
    PreToolUse hooks fire for every tool call in any mode, so this is the
    only place the harness can actually deny a Bash command.

    Returns a hook callback. Non-Bash tool events pass through with
    ``permissionDecision="allow"``. Bash events run through
    :func:`_validate_bash_command` (or :func:`_validate_bash_command_readonly`
    when ``bash_profile="read_only"``) and emit ``deny`` with the validator's
    error message on failure.
    """
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
                _validate_bash_command_readonly(command)
            else:
                _validate_bash_command(command)
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


def make_repair_completion_hook(
    *,
    targets_path: Path,
    report_path: Path,
    max_block_attempts: int,
    file_comm: Any,
    round_num: int,
    trace_writer: "SdkTraceWriter | None" = None,
):
    """Build a Stop-hook callback that enforces a complete repair report.

    Replaces :func:`_keepalive_hook` for repair-mode generator runs. On Stop:

    1. Reads ``targets_path`` (written by the harness from the prior grade).
    2. Reads ``report_path`` (the generator's claimed completion report).
    3. If the report is missing or any target is unaddressed, returns
       ``decision="block"`` with a feedback ``reason`` listing the gaps and
       decrements ``block_attempts_remaining``.
    4. Once attempts are exhausted, writes ``repair_incomplete_round_N.json``
       and lets the Stop through so the harness can roll into the next round
       (where the same items will reappear in the next grade and re-trigger
       repair). This bounds the loop at ``max_block_attempts`` extra turns
       even when the model can't make progress.

    The state lives in the closure so the SDK can call the hook repeatedly
    on the same generator session. The hook is also a no-op when
    ``stop_hook_active`` is True (avoids interacting with another stop hook
    in the chain) or when no targets were declared.
    """
    state = {"attempts_used": 0}

    async def _hook(input_data: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        # Defensive: respect chained stop hooks; the SDK sets this true on
        # the second invocation in a row to prevent recursion.
        if isinstance(input_data, dict) and input_data.get("stop_hook_active"):
            return {"continue_": True}

        try:
            targets_payload = json.loads(targets_path.read_text())
        except (OSError, json.JSONDecodeError):
            # No targets file → nothing to enforce. Behave like keepalive.
            return {"continue_": True}

        targets = targets_payload.get("targets") or []
        if not targets:
            return {"continue_": True}

        target_ids = [str(t.get("id", "")).strip() for t in targets if isinstance(t, dict)]
        target_ids = [tid for tid in target_ids if tid]

        report_payload: dict[str, Any] | None = None
        if report_path.exists():
            try:
                report_payload = json.loads(report_path.read_text())
            except (OSError, json.JSONDecodeError):
                report_payload = None

        unaddressed: list[str] = []
        if report_payload is None:
            unaddressed = list(target_ids)
            missing_report = True
        else:
            missing_report = False
            addressed_map: dict[str, dict[str, Any]] = {}
            for entry in report_payload.get("addressed", []) or []:
                if not isinstance(entry, dict):
                    continue
                tid = str(entry.get("target_id", "")).strip()
                if tid:
                    addressed_map[tid] = entry
            for tid in target_ids:
                entry = addressed_map.get(tid)
                if entry is None or entry.get("addressed") is not True:
                    unaddressed.append(tid)

        if not unaddressed:
            return {"continue_": True}

        if state["attempts_used"] >= max_block_attempts:
            # Give up and let the harness move on. Persist the leftover
            # for the next round's evaluator to surface.
            file_comm.write_repair_incomplete(
                round_num,
                {
                    "round": round_num,
                    "unaddressed_target_ids": unaddressed,
                    "block_attempts_used": state["attempts_used"],
                },
            )
            if trace_writer:
                trace_writer.write(
                    "repair_block_exhausted",
                    {"round": round_num, "unaddressed": unaddressed},
                )
            return {"continue_": True}

        state["attempts_used"] += 1
        attempts_left = max_block_attempts - state["attempts_used"]

        if missing_report:
            reason = (
                f"You must write .harness/repair_report_round_{round_num}.json before ending. "
                f"List one entry per target from .harness/repair_targets_round_{round_num}.json "
                f"with target_id, addressed (true/false), files_modified (frontend/* paths you "
                f"actually edited), and notes. Targets to address: {', '.join(target_ids)}. "
                f"You have {attempts_left} more attempts before the harness moves on."
            )
        else:
            reason = (
                f"The following repair targets are still unaddressed: {', '.join(unaddressed)}. "
                f"Re-read .harness/feedback_round_{round_num - 1}.md and "
                f".harness/grade_round_{round_num - 1}.json, implement the fixes, then update "
                f".harness/repair_report_round_{round_num}.json setting addressed=true for each. "
                f"If a target is genuinely unfixable in this sprint, set addressed=false and "
                f"explain why in `reason`. You have {attempts_left} more attempts."
            )

        if trace_writer:
            trace_writer.write(
                "repair_block",
                {
                    "round": round_num,
                    "attempts_used": state["attempts_used"],
                    "attempts_left": attempts_left,
                    "missing_report": missing_report,
                    "unaddressed": unaddressed,
                },
            )

        return {"decision": "block", "reason": reason}

    return _hook


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


def build_agent_run_stats(
    result_message: ResultMessage, *, model: str
) -> AgentRunStats:
    """Build :class:`AgentRunStats` from a CLI ``ResultMessage``.

    ``cost_usd`` is computed from the local pricing table (see
    :mod:`src.orchestration.pricing`) rather than read from
    ``result_message.total_cost_usd``. The CLI's own estimate uses a
    Claude-only price book and silently produces 0 / wrong values when
    the harness proxies to GLM, OpenAI, or any other backend, which
    would let the harness budget gate be bypassed.
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


def _clear_current_task_cancellation() -> int:
    task = asyncio.current_task()
    if task is None:
        return 0

    cleared = 0
    while task.cancelling():
        task.uncancel()
        cleared += 1
    return cleared


async def _drain_leaked_cancellation_after_success(
    *,
    result_message: ResultMessage | None,
    trace_writer: SdkTraceWriter | None,
) -> None:
    if result_message is None:
        return

    try:
        # Yield one event-loop turn so delayed cancellations scheduled by
        # async-generator finalizers land here, within the SDK boundary,
        # instead of poisoning the caller's next unrelated await.
        await asyncio.sleep(0)
    except asyncio.CancelledError:
        cleared = _clear_current_task_cancellation()
        if trace_writer:
            trace_writer.write(
                "post_success_cancellation_suppressed",
                {"cleared": cleared},
            )


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


def _validate_bash_command_readonly(command: str) -> list[str]:
    """Stricter Bash validator for agents that must not mutate the project.

    Layered on top of :func:`_validate_bash_command` (which already gates
    shell control operators, path traversal, and the global allowlist).
    Adds:

    * a smaller executable allowlist (``cp``/``mv``/``touch``/``mkdir``/
      ``sed``/``pytest``/``vite``/``uv``/``uvicorn``/``tsc`` removed)
    * git restricted to read-only subcommands
    * ``python``/``python3``/``node`` reject ``-c``/``-e``/``--eval``/``-i``
      so the agent cannot smuggle ``open('src/x','w').write(...)``
    * ``npm``/``pnpm``/``yarn``/``npx`` restricted to read subcommands
      (no ``install``/``test``/``build`` that would execute scripts and
      write to ``node_modules``/``dist``)
    """
    argv = _validate_bash_command(command)
    executable = argv[0]
    if executable not in _READONLY_ALLOWED_BASH_COMMANDS:
        raise ValueError(
            f"command not allowed in read-only bash profile: {executable}"
        )

    if executable == "git":
        if len(argv) < 2:
            raise ValueError("git requires a subcommand")
        if argv[1] not in _GIT_READONLY_SUBCOMMANDS:
            raise ValueError(
                f"git subcommand not allowed in read-only bash profile: {argv[1]}"
            )
    elif executable in {"python", "python3", "node"}:
        for token in argv[1:]:
            if token in _INTERPRETER_INLINE_CODE_FLAGS:
                raise ValueError(
                    f"inline code flag not allowed in read-only bash profile: {token}"
                )
    elif executable in {"npm", "pnpm", "yarn", "npx"}:
        # Skip leading flags like `npm --silent list`. The first non-flag
        # token after the executable is the subcommand.
        subcommand = next((tok for tok in argv[1:] if not tok.startswith("-")), None)
        if subcommand is None:
            # Bare `npm` / `npx` does too much by default, deny.
            raise ValueError(
                f"{executable} requires a read-only subcommand "
                f"({sorted(_PKG_MANAGER_READONLY_SUBCOMMANDS)})"
            )
        if subcommand not in _PKG_MANAGER_READONLY_SUBCOMMANDS:
            raise ValueError(
                f"{executable} subcommand not allowed in read-only bash profile: "
                f"{subcommand}"
            )

    return argv


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
    bash_profile: str = "full",
    trace_writer: SdkTraceWriter | None = None,
    frontend_port: int = 5173,
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
                if bash_profile == "read_only":
                    _validate_bash_command_readonly(tool_input.get("command", ""))
                else:
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
    bash_profile: str = "full",
    stop_hook: Any = None,
    trace_writer: SdkTraceWriter | None = None,
) -> ClaudeAgentOptions:
    mcp_servers: dict[str, McpStdioServerConfig] = {}
    if allow_playwright:
        mcp_servers["playwright"] = {
            "command": "npx",
            "args": build_playwright_mcp_args(config),
        }

    allowed_tools = sorted(LOCAL_AGENT_TOOLS_WITH_BASH if allow_bash else LOCAL_AGENT_TOOLS)
    hooks: dict[str, list[HookMatcher]] = {
        "Stop": [HookMatcher(hooks=[stop_hook or _keepalive_hook])],
    }
    if allow_bash:
        # PreToolUse fires for every tool call regardless of allowedTools, so
        # this is the gate that actually catches `npx vite … &`, `cd /abs && …`,
        # `sed -i`, etc. before the CLI executes them. can_use_tool is bypassed
        # for tools in --allowedTools, so we cannot rely on it for Bash.
        hooks["PreToolUse"] = [
            HookMatcher(
                matcher="Bash",
                hooks=[make_bash_pretool_hook(bash_profile=bash_profile)],
            )
        ]

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
            bash_profile=bash_profile,
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
    bash_profile: str = "full",
    stop_hook: Any = None,
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
        bash_profile=bash_profile,
        stop_hook=stop_hook,
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
    stream = query(prompt=_single_prompt_stream(prompt), options=options)
    try:
        async for message in stream:
            if trace_writer:
                trace_writer.write("sdk_message", _serialize_sdk_message(message))
            if isinstance(message, ResultMessage):
                result_message = message
                permission_denials = list(message.permission_denials or [])
                # Some SDK backends emit the terminal ResultMessage and then
                # keep the stream open instead of closing it promptly. Treat
                # ResultMessage as authoritative and stop consuming here so
                # the harness does not appear stuck in Generator/Evaluator.
                break
            if isinstance(message, AssistantMessage):
                text = _extract_text_from_assistant_message(message)
                if text:
                    last_assistant_text = text
    finally:
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            # Some SDK / anyio-backed stream implementations cancel the task
            # executing `aclose()` as part of their internal shutdown path.
            # If we close on the harness task directly, that cancellation can
            # leak past a successful ResultMessage and poison the *next* await
            # (e.g. generator commit via `git init`). Run close on a dedicated
            # child task, shield it from parent-task cancellation, and keep
            # draining leaked cancellations until shutdown has actually ended.
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
        await _drain_leaked_cancellation_after_success(
            result_message=result_message,
            trace_writer=trace_writer,
        )

    if result_message is None:
        raise RuntimeError("Agent SDK returned no result message")
    if result_message.is_error:
        details = "; ".join(result_message.errors or [])
        raise RuntimeError(result_message.result or details or "Agent SDK run failed")

    # Compute the cost via the local pricing table to keep this consistent
    # with build_agent_run_stats; result_message.total_cost_usd is the
    # CLI's Claude-only estimate and can disagree with the table when the
    # base URL is proxied to a non-Claude backend.
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
