"""Single-call acceptance planner for a frozen compound Edit."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src.agents.openai_runner import OpenAIHTTPClient
from src.config import HarnessConfig
from src.orchestration.frozen_compound_edit import (
    normalize_frozen_compound_plan,
    write_frozen_compound_plan,
)
from src.orchestration.pricing import estimate_cost_usd
from src.utils.llm_json import extract_json_object


COMPOUND_EDIT_PLANNER_SYSTEM_PROMPT = """\
Create the complete frozen acceptance plan for one compound frontend Edit.
The 4--12 supplied subtasks, their order, task types, and instruction text are immutable.
Return one atomic plan per subtask in the same order. Do not implement code.

Each subtask must have exactly one continuous browser check. Its actions must exercise the
requested behavior and end in a typed assertion that distinguishes a working implementation
from an unchanged source. Choose stable selectors from the accepted source UI contract when
possible. For new surfaces, use feature-specific data-testid selectors. target_routes is the
smallest route set whose source may change; every check route must be inside it.
Never invent a data-testid merely to prove arrival on an existing page. After clicking a
grounded same-origin link, use assert_url with the grounded pathname, or continue directly with
the next grounded action. A planned data-testid is allowed only for a new surface created by the
current frozen subtask.

Every action object uses the key `action`, never `type`. There is no navigate action: check.route
opens the starting page. Supported interaction forms include
{"action":"click","selector":"..."},
{"action":"fill","selector":"...","value":"..."},
{"action":"select_option","selector":"...","value":"..."}, and
{"action":"set_input_files","selector":"...","files":[{"name":"sample.txt",
"mime_type":"text/plain","content":"sample"}]}. Supported assertions include
assert_visible, assert_hidden, assert_text with value and match=exact|contains|nonempty,
assert_value, assert_count, assert_attribute, assert_property, assert_focus, assert_url,
assert_hash, assert_storage_value, and assert_no_console_errors. Do not nest an `assertion`
object. Express absence with assert_hidden on a feature-specific selector or assert_count=0.
assert_storage_value uses storage=`local` or `session` and must compare an exact value grounded
in the instruction or earlier actions. Never use `nonempty` as its value or match mode; omit that
storage assertion when an exact value cannot be known.

Return JSON only:
{"subtasks":[{"id":"q1","task_type":"...","instruction":"exact unchanged text",
"target_routes":["/"],"atomic_plan":{"schema_version":"atomic-edit-plan-v1",
"goal":"exact unchanged text","source_anchors":[],"visual_evidence":"conditional",
"checks":[{"id":"q1-flow","route":"/","actions":[...]}]}}]}

Use only Harness-supported actions. Do not use arbitrary JavaScript, XPath, text= selectors,
comma-separated selectors, or private storage as a substitute for a user action. Preserve the
meaning of every instruction; the plan is frozen once and will not be regenerated after code
changes.
"""


def _planner_prompt(
    *, frozen_subtasks: list[dict[str, str]], source_ui_contract: dict[str, Any]
) -> str:
    return (
        "Frozen subtasks:\n"
        + json.dumps(frozen_subtasks, ensure_ascii=False, indent=2)
        + "\n\nAccepted source UI contract:\n"
        + json.dumps(source_ui_contract, ensure_ascii=False, separators=(",", ":"))
        + "\n\nReturn the complete plan now."
    )


async def plan_frozen_compound_edit(
    *,
    config: HarnessConfig,
    case_id: str,
    source_code_sha256: str,
    frozen_subtasks: list[dict[str, str]],
    source_ui_contract: dict[str, Any],
    output_path: Path,
) -> dict[str, Any]:
    """Make exactly one paid planning call, then freeze and hash the result."""
    if not 4 <= len(frozen_subtasks) <= 12:
        raise ValueError("compound Edit requires 4--12 frozen subtasks")
    client = OpenAIHTTPClient(config, config.agent_request_timeout_seconds)
    prompt = _planner_prompt(
        frozen_subtasks=frozen_subtasks,
        source_ui_contract=source_ui_contract,
    )
    started = time.monotonic()
    response_path = output_path.parent / "frozen_compound_planner_response.json"
    if response_path.is_file():
        saved = json.loads(response_path.read_text(encoding="utf-8"))
        if (
            saved.get("case_id") != case_id
            or saved.get("source_code_sha256") != source_code_sha256
            or saved.get("planner_model") != config.planner_model
        ):
            raise ValueError("saved compound planner response belongs to another request")
        text = str(saved["content"])
        usage = saved.get("usage") if isinstance(saved.get("usage"), dict) else {}
    else:
        response = await client.complete(
            model=config.planner_model,
            messages=[
                {"role": "system", "content": COMPOUND_EDIT_PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=12000,
            response_format={"type": "json_object"},
        )
        message = response.get("choices", [{}])[0].get("message", {})
        text = str(message.get("content") or "")
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        response_path.write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "source_code_sha256": source_code_sha256,
                    "planner_model": config.planner_model,
                    "content": text,
                    "usage": usage,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    normalized = normalize_frozen_compound_plan(
        extract_json_object(text),
        case_id=case_id,
        source_code_sha256=source_code_sha256,
        planner_model=config.planner_model,
        frozen_subtasks=frozen_subtasks,
    )
    digest = write_frozen_compound_plan(output_path, normalized)
    trace_path = output_path.parent / "frozen_compound_planner.jsonl"
    trace_path.write_text(
        json.dumps(
            {
                "event": "complete",
                "case_id": case_id,
                "model": config.planner_model,
                "subtask_count": len(frozen_subtasks),
                "frozen_plan_sha256": digest,
                "usage": usage,
                "estimated_cost_usd": estimate_cost_usd(
                    config.planner_model, usage
                ),
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "plan": normalized,
        "frozen_plan_sha256": digest,
        "usage": usage,
        "estimated_cost_usd": estimate_cost_usd(config.planner_model, usage),
    }


__all__ = ["COMPOUND_EDIT_PLANNER_SYSTEM_PROMPT", "plan_frozen_compound_edit"]
