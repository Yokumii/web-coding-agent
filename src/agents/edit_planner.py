"""One-artifact Planner path for explicit atomic Edit tasks."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src.agents.openai_runner import OpenAIHTTPClient
from src.agents.sdk_runner import AgentRunStats, build_agent_run_stats, run_sdk_agent
from src.config import HarnessConfig
from src.orchestration.accepted_tapes import accepted_obligation_summary
from src.orchestration.atomic_edit_plan import (
    ATOMIC_EDIT_PLAN_NAME,
    materialize_atomic_edit_compatibility_bundle,
    read_atomic_edit_plan,
    write_atomic_edit_plan,
)
from src.orchestration.edit_task_contract import read_edit_task_contract
from src.orchestration.file_comm import FileComm
from src.orchestration.pricing import estimate_cost_usd
from src.orchestration.task_inputs import (
    openai_user_content,
    task_input_image_paths,
    task_input_prompt_context,
)
from src.prompts.edit_planner import ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT
from src.utils.llm_json import extract_json_object


class AtomicEditPlannerToolPolicy:
    """Allow the SDK fallback to touch only its single semantic artifact."""

    _WRITE_TOOLS = {"write", "write_file", "edit", "apply_patch", "multiedit"}
    _READ_TOOLS = {"read", "read_file"}

    def check(self, tool: str, tool_input: dict[str, Any]) -> str | None:
        normalized = tool.lower()
        raw_path = tool_input.get("path") or tool_input.get("file_path")
        path = str(raw_path or "").replace("\\", "/")
        allowed = f".harness/{ATOMIC_EDIT_PLAN_NAME}"
        if normalized in self._WRITE_TOOLS and path != allowed:
            return f"Atomic Edit Planner may write only {allowed}."
        if normalized in self._READ_TOOLS and path != allowed:
            return f"Atomic Edit Planner may read only {allowed}."
        if normalized not in self._WRITE_TOOLS | self._READ_TOOLS:
            return "Atomic Edit Planner has no repository exploration tools."
        return None

    def observe_result(
        self, _tool: str, _tool_input: dict[str, Any], *, ok: bool, output: Any
    ) -> None:
        del ok, output


def _atomic_edit_prompt(
    *, user_prompt: str, workdir: Path, contract: dict[str, Any]
) -> str:
    obligations = accepted_obligation_summary(workdir / ".harness")
    input_context = task_input_prompt_context(workdir)
    prompt = (
        "Create the atomic Edit plan for this instruction:\n\n"
        f"{user_prompt.strip()}\n\n"
        "Requested target routes: "
        + json.dumps(contract.get("requested_target_routes") or [], ensure_ascii=False)
        + "\nExisting accepted obligations are preservation metadata only:\n"
        + json.dumps(obligations, ensure_ascii=False)
    )
    if input_context:
        prompt += "\n\n" + input_context
    return prompt


def _validate_and_materialize(
    *, file_comm: FileComm, user_prompt: str, config: HarnessConfig
) -> dict[str, Any]:
    from src.agents.planner import _initialize_accepted_sprints, _validate_planning_bundle

    plan_path = file_comm.dir / ATOMIC_EDIT_PLAN_NAME
    if not plan_path.is_file():
        raise ValueError(f"Atomic Edit Planner must write .harness/{ATOMIC_EDIT_PLAN_NAME}.")
    raw_plan = json.loads(plan_path.read_text(encoding="utf-8"))
    write_atomic_edit_plan(
        file_comm.dir,
        raw_plan,
        instruction_delta=user_prompt,
    )
    plan = read_atomic_edit_plan(file_comm.dir)
    if plan is None:  # pragma: no cover - write above validates the schema
        raise ValueError("Atomic Edit Planner produced an unreadable normalized plan.")
    materialize_atomic_edit_compatibility_bundle(
        file_comm=file_comm,
        instruction_delta=user_prompt,
        plan=plan,
    )
    _validate_planning_bundle(file_comm, config)
    _initialize_accepted_sprints(file_comm)
    return plan


def _make_atomic_stop_hook(
    *, file_comm: FileComm, user_prompt: str, config: HarnessConfig
):
    async def _hook(_input: Any, _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
        try:
            _validate_and_materialize(
                file_comm=file_comm, user_prompt=user_prompt, config=config
            )
        except Exception as exc:
            reason = f"Atomic Edit plan validation failed: {exc}"
            return {"decision": "block", "reason": reason, "stopReason": reason}
        return {"decision": "complete"}

    return _hook


def _is_openai_runtime(config: HarnessConfig) -> bool:
    runtime = config.agent_runtime.strip().lower()
    model = config.planner_model.strip().lower()
    return runtime == "openai" or (
        runtime == "auto"
        and model.startswith(("deepseek", "qwen", "gpt-", "o1", "o3", "o4"))
    )


async def run_atomic_edit_planner(
    config: HarnessConfig,
    user_prompt: str,
    file_comm: FileComm,
    workdir: Path,
) -> AgentRunStats:
    """Plan one Edit with one JSON response on native OpenAI runtimes."""
    contract = read_edit_task_contract(workdir)
    if contract is None:
        raise ValueError("Atomic Edit Planner requires edit_task_contract.json")
    prompt = _atomic_edit_prompt(
        user_prompt=user_prompt, workdir=workdir, contract=contract
    )
    trace_path = file_comm.dir / "traces" / "planner.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    if _is_openai_runtime(config):
        content: str | list[dict[str, Any]] = prompt
        images = task_input_image_paths(workdir)
        if images:
            content = openai_user_content(prompt, images)
        client = OpenAIHTTPClient(config, config.agent_request_timeout_seconds)
        with trace_path.open("a", encoding="utf-8") as trace:
            trace.write(json.dumps({
                "event": "run_start",
                "model": config.planner_model,
                "phase": "atomic_edit_planner",
                "prompt": prompt,
                "image_paths": [str(path) for path in images],
            }, ensure_ascii=False) + "\n")
            trace.flush()
            response = await client.complete(
                model=config.planner_model,
                messages=[
                    {"role": "system", "content": ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                temperature=0,
                max_tokens=4096,
            )
            message = response.get("choices", [{}])[0].get("message", {})
            text = str(message.get("content") or "")
            usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
            input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
            output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
            trace.write(json.dumps({
                "event": "assistant_response",
                "content": text,
            }, ensure_ascii=False) + "\n")
            trace.write(json.dumps({
                "event": "usage",
                "request_usage": usage,
                "attempt_usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                "cumulative_usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
                "estimated_cost_usd": estimate_cost_usd(config.planner_model, usage),
            }, ensure_ascii=False) + "\n")
            trace.flush()
            payload = extract_json_object(text)
            write_atomic_edit_plan(
                file_comm.dir,
                payload,
                instruction_delta=user_prompt,
            )
            _validate_and_materialize(
                file_comm=file_comm, user_prompt=user_prompt, config=config
            )
            trace.write(json.dumps({
                "event": "atomic_edit_plan",
                "artifact": f".harness/{ATOMIC_EDIT_PLAN_NAME}",
            }, ensure_ascii=False) + "\n")
            trace.flush()
        return AgentRunStats(
            cost_usd=estimate_cost_usd(config.planner_model, usage),
            duration_ms=int((time.monotonic() - started) * 1000),
            duration_api_ms=None,
            token_usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
            usage=usage,
            model_usage={},
        )

    plan_path = file_comm.dir / ATOMIC_EDIT_PLAN_NAME
    if not plan_path.exists():
        plan_path.write_text("{}\n", encoding="utf-8")
    sdk_prompt = (
        prompt
        + f"\n\nWrite only `.harness/{ATOMIC_EDIT_PLAN_NAME}` with the JSON object."
    )
    result, _cost, _text, _denials = await run_sdk_agent(
        prompt=sdk_prompt,
        config=config,
        workdir=workdir,
        model=config.planner_model,
        system_prompt=ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT,
        max_turns=min(config.planner_max_turns, 4),
        allow_bash=False,
        stop_hooks=[_make_atomic_stop_hook(
            file_comm=file_comm, user_prompt=user_prompt, config=config
        )],
        trace_path=trace_path,
        mutation_policy=AtomicEditPlannerToolPolicy(),
        image_paths=task_input_image_paths(workdir),
    )
    _validate_and_materialize(
        file_comm=file_comm, user_prompt=user_prompt, config=config
    )
    return build_agent_run_stats(result, model=config.planner_model)


__all__ = [
    "AtomicEditPlannerToolPolicy",
    "run_atomic_edit_planner",
]
