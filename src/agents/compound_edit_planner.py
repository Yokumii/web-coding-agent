"""Cached acceptance planner with one bounded correction for a frozen compound Edit."""
from __future__ import annotations

import json
import hashlib
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

Exercise the actual entry journey before asserting a new panel: click the source's existing
menu/tab/button named in the instruction. Never require a modal feature to be visible on page
load when the user requested opening it from an existing control. Do not invent extra controls
such as a Start Upload button when the instruction and reference allow automatic upload.
Selectors are behavioral anchors, not an HTML serialization contract. For an existing or
reference-backed component, derive the selector from the source DOM and accept a stable
equivalent value/trend/status node when its metric role and state are the same; do not fail
solely because a framework adds a suffix such as `-value`, `-trend`, or `-status`. Keep the
actual user controls and state transitions grounded and exact. For asynchronous behavior, wait_for observable state selectors rather than asserting an exact
percentage after a fixed delay. Check progress is nonzero and nonfinal, then reaches completion.
Use valid file contents: content is literal UTF-8, not base64; use a valid inline SVG for an image
fixture rather than PNG header text. Use distinct files for completed, cancelled and retry flows.
No implementation may special-case a test filename to pass; failure fixtures must exercise a
general, documented simulation policy when simulated transport is explicitly requested.
assert_count uses the integer field count, including zero after removal; never use value.

Test behavior, not invented implementation details. Do not require sentinel attributes such
as data-progress="preserved", fake success strings, a predetermined unread count on startup,
or one exact HTML serialization. Metadata preservation means capturing a real value before
an action and comparing it afterwards. Reordering means checking individual item positions,
not concatenated container text that excludes the cards' descriptions and controls.
Use capture_attribute {selector,name,snapshot} followed by assert_attribute
{selector,name,snapshot} for an existing attribute; never invent a sentinel to stand for data.
For rich text, select the editing surface with key_press selector and key="ControlOrMeta+a",
assert a semantic descendant (strong, h2, ul li, blockquote, a[href]) immediately after its
formatting action, and check the submitted value contains the corresponding markup. Do not
assume every sequential block-format command nests around all earlier formats. Use the
reference DOM class names; sanitized links need not retain testing attributes.
Notification checks must cause a real specified host event before asserting an arrival.
Assert that event row is unread, mark that row read and assert its state changed; then use
mark-all to establish a zero unread count. Never expect an already-read event to become
unread merely by reopening its panel. Scope each
entry check to one stable event, not all rows. A related-action button is required only if
the instruction asks for navigation; displaying related context alone does not require it.
After reload, re-enter the host page through its ordinary entry journey; persisting feature
data does not imply persisting the current game screen or opening a panel automatically.
Respect source entry_transitions: after an entry hides a menu control, do not click that
control later in the same journey. For a cross-feature event, perform it before leaving its
accessible view, then open the destination view to inspect the resulting state.
Avoid fixed sleeps for arrivals. wait_for the specific event/state within the normal timeout.
Aim for at most 24 actions per subtask. This is a lightweight causal smoke test, not an exhaustive
UI inventory. Exercise the core operation and one important edge, then stop. The Editing Agent
still implements every instruction requirement, even those not enumerated in this smoke test.
Do not overwrite content with fill and later expect its previous value to remain. A closed
panel's content becomes visible only after opening that panel; wait_for means visible.
Choose simple independent formats, not a long sequence of all toolbar operations.

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
For progress use {"action":"assert_number","selector":"...","property":"value",
"min":0.001,"max":0.999,"timeout_ms":5000} for a normalized 0..1 progress element;
it waits for a numeric interval. Never assert a precise non-final progress value or zero
immediately after starting an upload. Observe completion through the state selector.
Follow the selected reference's DOM/API contract. Do not invent a fake oversized fixture:
set_input_files content is literal bytes, so short text is a small file regardless of its name.
An optional integer size_bytes on a file fixture pads it with zero bytes (max 32 MiB/file,
32 MiB/action); use it to exercise an actual size limit, with a separately asserted error.
assert_storage_value uses storage=`local` or `session` and must compare an exact value grounded
in the instruction or earlier actions. Never use `nonempty` as its value or match mode; omit that
storage assertion when an exact value cannot be known.
Other supported bounded actions are reload, go_back, scroll with integer y, wait with
milliseconds up to 5000, wait_for with selector, set_viewport, key_press, hover, and
drag_and_drop with source_selector and target_selector.

Return JSON only. Do not echo task_type, instruction, or goal: Harness binds these immutable
fields from the supplied subtask ID. Spend output on the executable acceptance flow:
{"subtasks":[{"id":"q1",
"target_routes":["/"],"atomic_plan":{"schema_version":"atomic-edit-plan-v1",
"source_anchors":[],"visual_evidence":"conditional",
"checks":[{"id":"q1-flow","route":"/","actions":[...]}]}}]}

Use only Harness-supported actions. Do not use arbitrary JavaScript, XPath, text= selectors,
comma-separated selectors, or private storage as a substitute for a user action. Preserve the
meaning of every instruction; the plan is frozen once and will not be regenerated after code
changes.
"""


def _planner_prompt(
    *, frozen_subtasks: list[dict[str, str]], source_ui_contract: dict[str, Any]
) -> str:
    required_ids = [str(item["id"]) for item in frozen_subtasks]
    return (
        f"Return exactly {len(frozen_subtasks)} subtask plans as one json object. Required IDs in order: "
        + json.dumps(required_ids, ensure_ascii=False)
        + ". A syntactically valid response that omits any ID is invalid. Keep each continuous "
        "check focused, but do not stop before every required ID is present.\n\n"
        "Frozen subtasks:\n"
        + json.dumps(frozen_subtasks, ensure_ascii=False, indent=2)
        + "\n\nAccepted source UI contract:\n"
        + json.dumps(source_ui_contract, ensure_ascii=False, separators=(",", ":"))
        + "\n\nPrefer visual_evidence=not_required when typed assertions establish every requirement. Otherwise declare only the unresolved explicit visual requirement for targeted review. Return the complete plan now."
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
    """Plan once with at most one evidence-guided correction, then freeze the result."""
    if not 4 <= len(frozen_subtasks) <= 12:
        raise ValueError("compound Edit requires 4--12 frozen subtasks")
    client = OpenAIHTTPClient(config, config.agent_request_timeout_seconds)
    prompt = _planner_prompt(
        frozen_subtasks=frozen_subtasks,
        source_ui_contract=source_ui_contract,
    )
    request_sha = hashlib.sha256((COMPOUND_EDIT_PLANNER_SYSTEM_PROMPT + "\n" + prompt).encode()).hexdigest()
    started = time.monotonic()
    messages = [
        {"role": "system", "content": COMPOUND_EDIT_PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    request_usages = []
    required_ids = [str(item["id"]) for item in frozen_subtasks]
    accumulated_subtasks: dict[str, dict[str, Any]] = {}
    initial_visibility = {
        (page["route"], control["selector"]): control.get("initially_visible")
        for page in source_ui_contract.get("pages", [])
        for control in page.get("controls", []) if control.get("selector")
    }
    entry_transitions = {
        (page["route"], item["click"]): item["hides"]
        for page in source_ui_contract.get("pages", [])
        for item in page.get("entry_transitions", [])
    }
    for attempt in range(2):
        suffix = "" if attempt == 0 else "_correction_1"
        response_path = output_path.parent / f"frozen_compound_planner_response{suffix}.json"
        if response_path.is_file():
            saved = json.loads(response_path.read_text(encoding="utf-8"))
            if (saved.get("case_id") != case_id
                or saved.get("source_code_sha256") != source_code_sha256
                or saved.get("planner_model") != config.planner_model):
                raise ValueError("saved compound planner response belongs to another case/source/model")
        else:
            response = await client.complete(model=config.planner_model, messages=messages,
                temperature=0, max_tokens=12000,
                reasoning_effort="low" if config.openai_wire_api == "responses" else None,
                response_format={"type": "json_object"})
            saved = {
                "case_id": case_id, "source_code_sha256": source_code_sha256,
                "planner_model": config.planner_model, "request_sha256": request_sha,
                "content": str(response.get("choices", [{}])[0].get("message", {}).get("content") or ""),
                "usage": response.get("usage") or {},
            }
            response_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        text = str(saved["content"])
        request_usages.append(saved.get("usage") or {})
        try:
            payload = extract_json_object(text)
            returned_subtasks = payload.get("subtasks") or []
            if isinstance(returned_subtasks, list):
                for item in returned_subtasks:
                    if isinstance(item, dict) and str(item.get("id") or "") in required_ids:
                        accumulated_subtasks[str(item["id"])] = item
            payload["subtasks"] = [
                accumulated_subtasks[item_id]
                for item_id in required_ids
                if item_id in accumulated_subtasks
            ]
            if config.edit_skills_enabled:
                for item in payload.get("subtasks") or []:
                    atomic_plan = item.get("atomic_plan") or {}
                    visual = atomic_plan.get("visual_evidence")
                    if visual not in {"required", "conditional", "not_required"}:
                        if isinstance(visual, str) and visual.strip():
                            atomic_plan["visual_evidence_reason"] = visual.strip()
                            atomic_plan["visual_evidence"] = "conditional"
                        else:
                            atomic_plan["visual_evidence"] = "not_required"
                    checks = atomic_plan.get("checks") or []
                    if len(checks) != 1:
                        raise ValueError("Each Skill subtask requires one continuous browser check")
                    actions = checks[0].get("actions") or []
                    if not any(action.get("action", "").startswith("assert_") and action.get("action") != "assert_no_console_errors" for action in actions):
                        raise ValueError("Skill acceptance needs a behavior assertion, not console-only success")
                    if not actions or actions[-1].get("action") != "assert_no_console_errors":
                        actions.append({"action": "assert_no_console_errors"})
                    checks[0]["actions"] = actions
                    first_interaction = next((action for action in actions if action.get("action") in {"click", "fill", "key_press", "select_option", "set_input_files"}), {})
                    if initial_visibility.get((checks[0].get("route", "/"), first_interaction.get("selector"))) is False:
                        raise ValueError(f"{item.get('id')}: first interaction targets an initially hidden control: {first_interaction}. Open its parent view using an initially visible source control first; check every subtask for the same entry error.")
                    hidden_by_entry = set()
                    for action in actions:
                        if action.get("action") == "reload":
                            hidden_by_entry.clear()
                        elif action.get("action") == "click":
                            selector = action.get("selector")
                            if selector in hidden_by_entry:
                                raise ValueError(f"{item.get('id')}: source entry navigation hides {selector} before this click. Perform that feature's operation before leaving its view, then inspect the resulting state from the destination view.")
                            hidden_by_entry.update(entry_transitions.get((checks[0].get("route", "/"), selector), []))
            normalized = normalize_frozen_compound_plan(payload, case_id=case_id,
                source_code_sha256=source_code_sha256, planner_model=config.planner_model,
                frozen_subtasks=frozen_subtasks)
            if config.edit_skills_enabled and saved.get("request_sha256") != request_sha:
                raise ValueError("Source observation or planner contracts changed. Adapt this saved candidate to the current prompt, especially entry_transitions and the reference API; preserve valid actions.")
            break
        except ValueError as exc:
            feedback = str(exc)
            (output_path.parent / f"planner_rejection_{attempt + 1}.json").write_text(
                json.dumps({"attempt": attempt + 1, "error": feedback, "response": response_path.name}, ensure_ascii=False, indent=2) + "\n")
            if attempt == 1:
                raise
            missing_ids = [item_id for item_id in required_ids if item_id not in accumulated_subtasks]
            missing_guidance = (
                " Return only the missing subtask plans for IDs in this exact order: "
                + json.dumps(missing_ids, ensure_ascii=False)
                + ". Existing returned plans are retained and will be merged locally."
                if missing_ids
                else " Return the complete corrected bundle."
            )
            messages += [{"role": "assistant", "content": text}, {"role": "user", "content":
                "Correct this same acceptance plan using the exact evidence below. Do not echo frozen instructions or restart the task. Fix all occurrences of the same defect."
                + missing_guidance + "\n" + feedback}]
    usage = {
        "input_tokens": sum(int(item.get("input_tokens", item.get("prompt_tokens", 0)) or 0) for item in request_usages),
        "output_tokens": sum(int(item.get("output_tokens", item.get("completion_tokens", 0)) or 0) for item in request_usages),
        "requests": request_usages,
    }
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
