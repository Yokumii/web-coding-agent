from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.agents.openai_tools import OpenAIToolExecutor, openai_tool_schemas
from src.config import HarnessConfig
from src.prompts.final_chance import FINAL_CHANCE_TURNS, final_chance_prompt
from src.orchestration.pricing import estimate_cost_usd


@dataclass
class OpenAIRunLimits:
    phase_timeout: float = 600
    request_timeout: float = 120
    command_timeout: float = 120
    max_tool_calls: int = 120
    repeat_limit: int = 3
    error_repeat_limit: int = 3
    no_progress_limit: int = 8
    evaluation_exploration_limit: int = 40
    # A complete edit acceptance commonly needs: locate a page, record the
    # initial state, exercise each critical interaction, then inspect the
    # resulting state. Eight DOM probes is routinely exhausted before that.
    evaluation_browser_evaluate_limit: int = 16


@dataclass
class EvaluationToolPolicy:
    """Bound evaluator exploration and reserve the end of a run for artifacts."""

    exploration_limit: int = 40
    browser_evaluate_limit: int = 8
    exploration_calls: int = 0
    browser_evaluate_calls: int = 0
    finalizing: bool = False

    _FINALIZATION_TOOLS = frozenset({"write_file", "apply_patch", "browser_screenshot"})
    _BROWSER_VERIFICATION_TOOLS = frozenset({
        "browser_snapshot", "browser_click", "browser_fill", "browser_screenshot",
    })

    def check(self, tool_name: str) -> str | None:
        if self.finalizing:
            if tool_name in self._FINALIZATION_TOOLS | self._BROWSER_VERIFICATION_TOOLS:
                return None
            return (
                "Evaluation is in finalization mode. Stop investigating; only capture the "
                "required screenshot and write the grades, feedback, and visual manifest files."
            )

        if tool_name == "browser_evaluate":
            if self.browser_evaluate_calls >= self.browser_evaluate_limit:
                self.finalizing = True
                return (
                    "Browser diagnostic budget exhausted. Treat the evidence already collected "
                    "as sufficient and finalize the evaluation artifacts now."
                )
            self.browser_evaluate_calls += 1

        # Source exploration and browser interaction have different scarcity:
        # reserve browser actions for the declared UI checks even after the
        # model has consumed its inspection budget reading project files.
        if tool_name not in self._FINALIZATION_TOOLS | self._BROWSER_VERIFICATION_TOOLS:
            if self.exploration_calls >= self.exploration_limit:
                self.finalizing = True
                return (
                    "Evaluation exploration budget exhausted. Stop investigating and finalize "
                    "the grades, feedback, screenshots, and visual manifest now."
                )
            self.exploration_calls += 1
        return None


@dataclass
class OpenAIResult:
    result: str = ""
    content: list[Any] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    model_usage: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    duration_api_ms: int = 0
    permission_denials: list[Any] = field(default_factory=list)
    is_error: bool = False
    errors: list[str] = field(default_factory=list)


def _compact_messages(messages: list[dict[str, Any]], recent: int) -> list[dict[str, Any]]:
    """Fold completed old tool turns into a small deterministic progress summary."""
    if recent < 6 or len(messages) <= recent + 2:
        return messages
    cut = max(2, len(messages) - recent)
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut += 1
    old = messages[2:cut]
    if not old:
        return messages
    events: list[str] = []
    for message in old:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                target = args.get("path") or args.get("command") or ""
                events.append(f"{fn.get('name', 'tool')}({str(target)[:120]})")
        elif role == "tool":
            content = str(message.get("content") or "")
            events.append("result: " + content.replace("\n", " ")[:180])
        elif role in {"user", "system"}:
            content = str(message.get("content") or "")
            if "Earlier completed work" in content:
                events.append(content[-1500:])
    summary = "\n".join(events[-40:])
    return messages[:2] + [{
        "role": "user",
        "content": (
            "Earlier completed work was compacted to control context cost. Filesystem changes "
            "remain present; reread only a specific file if needed. Recent events:\n" + summary
        ),
    }] + messages[cut:]


def _is_finalization_command(command: str) -> bool:
    """Allow only commit-oriented commands once a generator is out of turns."""
    normalized = " ".join(str(command).strip().split())
    if normalized.startswith("cd frontend && "):
        normalized = normalized.removeprefix("cd frontend && ")
    return normalized.startswith((
        "git add ", "git commit ", "git status --short", "git diff --check",
        "node --check ", "npm run build",
    ))


class OpenAIHTTPClient:
    def __init__(self, config: HarnessConfig, timeout: float): self.config, self.timeout = config, timeout
    async def complete(self, **payload):
        import httpx
        base = (self.config.openai_base_url or self.config.base_url).rstrip("/")
        key = self.config.openai_api_key or self.config.api_key
        if not base or not key:
            raise ValueError("native OpenAI runtime requires OPENAI_AGENT_BASE_URL and OPENAI_AGENT_API_KEY")
        # Qwen's compatible endpoint otherwise enables long reasoning by default.
        # Tool-oriented harness phases need fast, bounded action turns; expose an
        # opt-in environment switch without coupling generic OpenAI providers to it.
        if os.getenv("OPENAI_ENABLE_THINKING") == "0" and str(payload.get("model", "")).lower().startswith("qwen"):
            payload["enable_thinking"] = False
        # The request budget is end-to-end.  In particular, do not let five
        # individually timed-out proxy retries turn a 120s calibration request
        # into a ten-minute cost/control failure.
        deadline = time.monotonic() + self.timeout
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            verify=os.getenv("SSL_NO_VERIFY") != "1",
            trust_env=True,
        ) as client:
            for attempt in range(5):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"chat completion exhausted its {self.timeout:.0f}s total request budget"
                    )
                try:
                    response = await client.post(
                        base + "/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json=payload,
                        timeout=httpx.Timeout(remaining),
                    )
                except httpx.TransportError:
                    # SOCKS/office-network connections occasionally fail before
                    # an HTTP response exists. Treat this exactly like a 5xx:
                    # retry the same idempotent chat request with bounded backoff.
                    if attempt < 4:
                        await asyncio.sleep(min(2 ** (attempt + 1), 16, max(0, deadline - time.monotonic())))
                        continue
                    raise
                body = response.text[:2000]
                provider_throttled = (
                    "MPE-429" in body
                    or "Throttling.BurstRate" in body
                    or "limit_burst_rate" in body
                )
                retryable = response.status_code == 429 or response.status_code >= 500 or provider_throttled
                if response.is_error and retryable and attempt < 4:
                    delay = min(10 * (2 ** attempt), 60) if provider_throttled else min(2 ** (attempt + 1), 16)
                    await asyncio.sleep(min(delay, max(0, deadline - time.monotonic())))
                    continue
                if response.is_error:
                    raise httpx.HTTPStatusError(
                        f"{response.status_code} from chat completions: {body}",
                        request=response.request,
                        response=response,
                    )
                return response.json()
            raise RuntimeError("unreachable chat completion retry state")


async def run_openai_agent(*, prompt: str, config: HarnessConfig, workdir: Path, model: str,
    system_prompt: str, max_turns: int, allow_bash: bool, allow_playwright: bool = False,
    bash_profile: str = "full", stop_hooks=None, trace_path: Path | None = None,
    client=None, limits: OpenAIRunLimits | None = None, mutation_policy=None):
    limits = limits or OpenAIRunLimits(phase_timeout=config.agent_phase_timeout_seconds,
        request_timeout=config.agent_request_timeout_seconds, max_tool_calls=config.agent_max_tool_calls)
    client = client or OpenAIHTTPClient(config, limits.request_timeout)
    tools = OpenAIToolExecutor(workdir=workdir, allow_bash=allow_bash, allow_playwright=allow_playwright,
        bash_profile=bash_profile, frontend_port=config.frontend_port, command_timeout=limits.command_timeout,
        mutation_policy=mutation_policy)
    evaluation_policy = EvaluationToolPolicy(
        exploration_limit=limits.evaluation_exploration_limit,
        browser_evaluate_limit=limits.evaluation_browser_evaluate_limit,
    ) if allow_playwright else None

    async def loop():
        started = time.monotonic(); api_ms = 0; calls = 0; no_progress = 0
        usage = {"input_tokens": 0, "output_tokens": 0}; last_text = ""
        last_signature = None; consecutive_calls = 0; last_error = None; consecutive_errors = 0
        native_guidance = (
            "\n\nNative harness tools: use write_file to create or overwrite files and apply_patch "
            "for exact replacements. Never write files with shell redirection, heredocs, echo, cat, "
            "or inline interpreter code. run_command permits only foreground allowlisted commands "
            "without shell control operators. Paths must remain inside the workdir."
        )
        if allow_playwright:
            native_guidance += (
                " You are evaluating an already-running app. Do not inspect processes, ports, "
                "or server internals and do not attempt to start or debug the server. Use browser_snapshot, "
                "browser_click/fill/evaluate, and browser_screenshot directly on the supplied localhost URL. "
                "Test only the declared criteria, then promptly write the required grades and visual manifest files. "
                "Browser exploration and deep browser_evaluate diagnostics have hard budgets. When the harness "
                "announces finalization mode, stop investigating immediately and write the required artifacts."
            )
        messages = [{"role": "system", "content": system_prompt + native_guidance}, {"role": "user", "content": prompt}]
        validation_retries = 0
        if trace_path:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace = trace_path.open("a") if trace_path else None
        try:
            total_turn_limit = min(
                max_turns + FINAL_CHANCE_TURNS,
                limits.max_tool_calls + 20,
            )
            for _ in range(total_turn_limit):
                messages = _compact_messages(messages, config.openai_recent_messages)
                turns_used = _ + 1
                final_chance = turns_used > max_turns
                if turns_used == max_turns + 1:
                    messages.append({
                        "role": "user",
                        "content": final_chance_prompt(
                            allow_bash=allow_bash,
                            allow_playwright=allow_playwright,
                        ),
                    })
                if not allow_playwright and turns_used == max(2, max_turns - 9):
                    messages.append({
                        "role": "user",
                        "content": (
                            "You have 10 model turns remaining. Stop broad exploration. Complete only "
                            "missing required files, perform one focused validation, commit the work, "
                            "and finish. Do not reread files you just wrote or repeat git/list checks."
                        ),
                    })
                t = time.monotonic()
                response = await asyncio.wait_for(client.complete(model=model, messages=messages,
                    tools=openai_tool_schemas(allow_bash=allow_bash, allow_playwright=allow_playwright), tool_choice="auto"), limits.request_timeout)
                api_ms += int((time.monotonic()-t)*1000)
                raw_usage = response.get("usage", {}); usage["input_tokens"] += raw_usage.get("prompt_tokens", 0); usage["output_tokens"] += raw_usage.get("completion_tokens", 0)
                msg = response["choices"][0]["message"]
                last_text = msg.get("content") or last_text
                messages.append(msg)
                if trace: trace.write(json.dumps({"event":"assistant", "message":msg}, ensure_ascii=False)+"\n"); trace.flush()
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    blocked_reason = None
                    for hook in stop_hooks or []:
                        verdict = await hook({}, None, {})
                        if verdict.get("decision") == "block":
                            blocked_reason = verdict.get("reason") or verdict.get("stopReason")
                            break
                    if blocked_reason:
                        validation_retries += 1
                        if validation_retries > 3:
                            raise RuntimeError("completion validation failed after 3 corrections: " + blocked_reason)
                        messages.append({"role":"user", "content":"Harness completion validation failed. Fix the files, then finish again:\n" + blocked_reason})
                        continue
                    result = OpenAIResult(last_text, [{"type":"text","text":last_text}], usage, {}, int((time.monotonic()-started)*1000), api_ms)
                    return result, estimate_cost_usd(model, usage), last_text, []
                for call in tool_calls:
                    calls += 1
                    if calls > limits.max_tool_calls: raise RuntimeError("maximum tool calls exceeded")
                    fn = call["function"]; signature = fn["name"] + ":" + fn.get("arguments", "")
                    if signature == last_signature: consecutive_calls += 1
                    else: last_signature, consecutive_calls = signature, 1
                    if consecutive_calls >= limits.repeat_limit: raise RuntimeError("repeated identical tool call circuit breaker")
                    try: args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError as exc: result = type("R", (), {"ok":False,"output":f"invalid JSON: {exc}","changed":False})()
                    else:
                        # A generator must be able to persist its final
                        # progress/scope artifacts as well as commit.  The old
                        # non-Playwright branch allowed only run_command, so a
                        # model entering FINAL CHANCE could be trapped in
                        # denied write_file retries immediately before commit.
                        final_allowed = (
                            {"write_file", "apply_patch", "browser_screenshot"}
                            if allow_playwright
                            else {"write_file", "apply_patch", "run_command"}
                        )
                        # Generator turns are expensive. Reserve the final ten
                        # model turns for making the last file edits, validating,
                        # committing, and stopping; otherwise a model can keep
                        # rereading files until the turn limit expires after it
                        # has already implemented the requested change.
                        # Do not force a generator that is still diagnosing a
                        # real repair into whole-file writes.  Before its true
                        # final chance it keeps normal tools; final chance is
                        # commit/validation-only so incomplete diagnosis cannot
                        # turn into an unrelated template rewrite.
                        turn_finalization = final_chance
                        final_denial = turn_finalization and (
                            fn["name"] not in final_allowed
                            or (fn["name"] == "run_command" and not _is_finalization_command(args.get("command", "")))
                        )
                        policy_denial = evaluation_policy.check(fn["name"]) if evaluation_policy else None
                        # Reserve the last ten calls in every phase for a
                        # useful terminal action. Generators otherwise spend
                        # their whole budget rereading adjacent source ranges
                        # and never reach apply_patch/commit.
                        finalize_only = calls > limits.max_tool_calls - 10
                        if final_denial:
                            result = type("R", (), {
                                "ok": False,
                                "output": "FINAL CHANCE permits finalization tools only. Stop exploring and write the required artifacts now.",
                                "changed": False,
                            })()
                        elif policy_denial:
                            result = type("R", (), {
                                "ok": False,
                                "output": policy_denial,
                                "changed": False,
                            })()
                        elif finalize_only and fn["name"] not in final_allowed:
                            result = type("R", (), {
                                "ok": False,
                                "output": (
                                    "Tool-call budget is nearly exhausted. Stop investigating now. "
                                    "If you are implementing or repairing, apply the scoped source "
                                    "changes you have already identified, validate, write the required "
                                    "scope artifact, commit, and finish. If you are evaluating, capture "
                                    "the required screenshot and write the grades and visual manifest files."
                                ),
                                "changed": False,
                            })()
                        else:
                            result = await tools.execute(fn["name"], args)
                    # Successful reads/searches advance the model's information state even
                    # when they do not mutate the filesystem. Count only failed tool turns
                    # as no progress; the global tool-call cap still bounds read-only loops.
                    if final_denial or policy_denial or finalize_only:
                        # A planned finalization denial is control guidance, not
                        # a failed tool invocation. Do not trip the generic
                        # repeated-error circuit breaker before the model can
                        # submit its required artifacts and finish. The same
                        # applies to evaluator diagnostic-budget guidance.
                        no_progress = 0
                        last_error, consecutive_errors = None, 0
                    elif result.ok: no_progress = 0
                    else: no_progress += 1
                    if no_progress >= limits.no_progress_limit: raise RuntimeError("no-progress circuit breaker")
                    if not result.ok:
                        normalized = result.output[:500]
                        if normalized == last_error: consecutive_errors += 1
                        else: last_error, consecutive_errors = normalized, 1
                        if consecutive_errors >= limits.error_repeat_limit: raise RuntimeError("repeated tool error circuit breaker: " + normalized)
                    else:
                        last_error, consecutive_errors = None, 0
                    tool_content = result.output
                    if len(tool_content) > config.openai_tool_result_chars:
                        tool_content = (
                            tool_content[:config.openai_tool_result_chars]
                            + f"\n<tool output truncated: {len(result.output)} total chars>"
                        )
                    messages.append({"role":"tool", "tool_call_id":call["id"], "content":tool_content})
                    if trace: trace.write(json.dumps({"event":"tool", "name":fn["name"], "ok":result.ok, "output":result.output[:2000]}, ensure_ascii=False)+"\n"); trace.flush()
                    # Artifact-producing agents may finish through their
                    # files alone. Let an approving hook stop here instead of
                    # paying for a redundant read/rewrite cycle before a
                    # natural-language epilogue.
                    if result.ok:
                        for hook in stop_hooks or []:
                            verdict = await hook({}, None, {})
                            if verdict.get("decision") == "complete":
                                completed = OpenAIResult(last_text, [{"type":"text","text":last_text}], usage, {}, int((time.monotonic()-started)*1000), api_ms)
                                return completed, estimate_cost_usd(model, usage), last_text, []
            # A tool-using model sometimes completes the filesystem work and
            # commit but never emits a final text turn. Preserve that valid
            # trajectory when the same completion hooks approve it.
            blocked_reason = None
            for hook in stop_hooks or []:
                verdict = await hook({}, None, {})
                if verdict.get("decision") == "block":
                    blocked_reason = verdict.get("reason") or verdict.get("stopReason")
                    break
            if blocked_reason is None:
                result = OpenAIResult(last_text, [{"type":"text","text":last_text}], usage, {}, int((time.monotonic()-started)*1000), api_ms)
                return result, estimate_cost_usd(model, usage), last_text, []
            raise RuntimeError("maximum agent turns exceeded: " + blocked_reason)
        finally:
            if trace: trace.close()
            try:
                await asyncio.wait_for(tools.close(), 5)
            except (asyncio.TimeoutError, Exception):
                pass
    try: return await asyncio.wait_for(loop(), limits.phase_timeout)
    except asyncio.TimeoutError as exc: raise RuntimeError(f"agent phase timed out after {limits.phase_timeout}s") from exc
