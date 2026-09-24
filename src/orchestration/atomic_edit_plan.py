"""Compact semantic plan for one atomic Edit transaction.

The model authors only the information that can change the meaning or the
verification of the Edit.  Legacy planning files are deterministic compatibility
views for downstream components; they are not additional model outputs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.orchestration.file_comm import FileComm
from src.orchestration.schemas import (
    NonEmptyString,
    RequirementChange,
    RequirementConflict,
)
from src.orchestration.ui_action_contracts import TYPED_ASSERTION_ACTIONS


ATOMIC_EDIT_PLAN_NAME = "atomic_edit_plan.json"
ATOMIC_EDIT_FEATURE_ID = "EDIT-001"


class AtomicEditCheck(BaseModel):
    """One target check without redundant Sprint/feature bookkeeping."""

    model_config = ConfigDict(extra="forbid")

    id: NonEmptyString
    task: NonEmptyString
    expected_result: NonEmptyString
    critical: bool = True
    category: NonEmptyString
    requirement_id: NonEmptyString
    impact_tags: list[NonEmptyString] = Field(min_length=1, max_length=8)
    route: NonEmptyString = "/"
    fixtures: list[str] = Field(default_factory=list, max_length=20)
    # One continuous browser journey may cover negative, success, and
    # persistence states without being split into multiple checks.
    actions: list[dict[str, Any]] = Field(min_length=1, max_length=64)


class AtomicEditPlan(BaseModel):
    """The only semantic artifact authored by the Edit Planner."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["atomic-edit-plan-v1"] = "atomic-edit-plan-v1"
    title: NonEmptyString
    goal: NonEmptyString
    source_anchors: list[NonEmptyString] = Field(default_factory=list)
    deliverables: list[NonEmptyString] = Field(min_length=1, max_length=10)
    exit_criteria: list[NonEmptyString] = Field(min_length=1, max_length=10)
    requirement_changes: list[RequirementChange] = Field(min_length=1, max_length=10)
    impact_tags: list[NonEmptyString] = Field(min_length=1, max_length=10)
    unresolved_conflicts: list[RequirementConflict] = Field(default_factory=list)
    visual_evidence: Literal["required", "conditional", "not_required"]
    visual_evidence_reason: NonEmptyString
    # One authored flow can expand into many bounded action segments. Its
    # segment count is not a task quota; per-segment and runtime bounds remain.
    checks: list[AtomicEditCheck] = Field(min_length=1)


def atomic_edit_plan_path(harness_dir: Path) -> Path:
    return Path(harness_dir) / ATOMIC_EDIT_PLAN_NAME


def read_atomic_edit_plan(harness_dir: Path) -> dict[str, Any] | None:
    path = atomic_edit_plan_path(harness_dir)
    if not path.is_file():
        return None
    return AtomicEditPlan.model_validate_json(
        path.read_text(encoding="utf-8")
    ).model_dump(exclude_unset=True)


def write_atomic_edit_plan(
    harness_dir: Path,
    payload: dict[str, Any],
    *,
    instruction_delta: str = "",
    preserve_actions: bool = False,
) -> Path:
    normalized = (json.loads(json.dumps(payload)) if preserve_actions else
                  normalize_atomic_edit_plan_payload(payload, instruction_delta=instruction_delta))
    if preserve_actions:
        from src.orchestration.ui_action_contracts import validate_ui_action, validate_ui_action_sequence
        originals = {check["id"]: check for check in payload["checks"]}
        for check in normalized["checks"]:
            actions = originals[check["id"]]["actions"]
            for action in actions:
                validate_ui_action(action)
            validate_ui_action_sequence(actions)
            check["actions"] = actions
    plan = AtomicEditPlan.model_validate(normalized)
    path = atomic_edit_plan_path(harness_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        plan.model_dump_json(indent=2, exclude_unset=True) + "\n",
        encoding="utf-8",
    )
    return path


def normalize_atomic_edit_plan_payload(
    payload: dict[str, Any], *, instruction_delta: str = ""
) -> dict[str, Any]:
    """Normalize harmless provider aliases without changing planned semantics."""
    output = json.loads(json.dumps(payload))
    goal = str(output.get("goal") or instruction_delta or "Apply the requested atomic Edit.").strip()
    output["goal"] = goal
    output.setdefault("title", goal)
    output.setdefault("deliverables", [goal])
    output.setdefault("exit_criteria", ["All target browser assertions pass."])
    output.setdefault("impact_tags", ["atomic-edit"])
    output.setdefault("unresolved_conflicts", [])
    visual_evidence = output.get("visual_evidence")
    if isinstance(visual_evidence, str) and ":" in visual_evidence:
        mode, reason = (part.strip() for part in visual_evidence.split(":", 1))
        if mode in {"required", "conditional", "not_required"} and reason:
            output["visual_evidence"] = mode
            prior = output.get("visual_evidence_reason")
            output["visual_evidence_reason"] = f"{prior}; {reason}" if prior else reason
    if isinstance(visual_evidence, dict):
        output["visual_evidence"] = str(
            visual_evidence.get("type")
            or visual_evidence.get("status")
            or "conditional"
        )
        if not output.get("visual_evidence_reason"):
            output["visual_evidence_reason"] = str(
                visual_evidence.get("reason")
                or "The Harness decides the required evidence from the target checks."
            )
    output.setdefault("visual_evidence", "conditional")
    output.setdefault(
        "visual_evidence_reason",
        "The Harness decides whether DOM, computed-style, or visual evidence is required.",
    )
    if not output.get("source_anchors"):
        quoted = [
            next(value for value in groups if value is not None).strip()
            for groups in re.findall(
                r"'([^']+)'|\"([^\"]+)\"|‘([^’]+)’|“([^”]+)”",
                instruction_delta,
            )
            if any(value is not None and value.strip() for value in groups)
        ]
        output["source_anchors"] = list(dict.fromkeys(quoted))[:12]
    if not output.get("requirement_changes"):
        output["requirement_changes"] = [
            {
                "requirement_id": "REQ-EDIT-001",
                "relation": "add",
                "prior_requirement_ids": [],
                "rationale": (
                    instruction_delta.strip()
                    or "The explicit user Edit is the requirement delta for this transaction."
                ),
            }
        ]
    requirement_id = str(output["requirement_changes"][0]["requirement_id"])
    plan_impact_tags = list(output.get("impact_tags") or ["atomic-edit"])
    selector_aliases: dict[str, str] = {}
    selector_counts = {"control": 0, "target": 0}
    number_words = {
        0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
        6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    }

    def count_is_instruction_grounded(count: Any) -> bool:
        if isinstance(count, bool) or not isinstance(count, int):
            return False
        if count == 0:
            # Zero is an absence assertion, not a guessed source cardinality.
            return True
        literals = {str(count)}
        if count in number_words:
            literals.add(number_words[count])
        return any(
            re.search(rf"(?<![\w]){re.escape(literal)}(?![\w])", instruction_delta, re.IGNORECASE)
            for literal in literals
        )

    def stable_selector(selector: str, action: str) -> str:
        value = selector.strip()
        if not (value.lower().startswith(("text=", "xpath=")) or "," in value):
            return value
        if value in selector_aliases:
            return selector_aliases[value]
        role = "target" if action.startswith("assert_") else "control"
        selector_counts[role] += 1
        literal = value.split("=", 1)[1] if value.lower().startswith("text=") else ""
        slug = re.sub(r"[^a-z0-9]+", "-", literal.lower()).strip("-")[:48]
        suffix = slug or str(selector_counts[role])
        normalized = f"[data-testid='edit-{role}-{suffix}']"
        selector_aliases[value] = normalized
        return normalized

    def split_bounded_assertion_checks(check: dict[str, Any]) -> list[dict[str, Any]]:
        """Keep one ordered completion flow in one browser check."""
        actions = list(check.get("actions") or [])
        return [{**check, "actions": actions}]

    normalized_checks: list[dict[str, Any]] = []
    prior_check_has_state_setup = False
    for index, check in enumerate(output.get("checks") or [], start=1):
        if not isinstance(check, dict):
            continue
        check.setdefault("id", f"EDIT-CHECK-{index}")
        check.setdefault("task", "Verify the requested atomic Edit.")
        check.setdefault("expected_result", "All target browser assertions pass.")
        check.setdefault("critical", True)
        check.setdefault("category", "functional")
        check.setdefault("route", "/")
        route_fragment = ""
        if isinstance(check.get("route"), str) and "#" in check["route"]:
            check["route"], route_fragment = check["route"].split("#", 1)
            check["route"] = check["route"] or "/"
        check.setdefault("fixtures", [])
        if not str(check.get("requirement_id") or "").strip():
            check["requirement_id"] = requirement_id
        if not check.get("impact_tags"):
            check["impact_tags"] = plan_impact_tags
        normalized_actions: list[dict[str, Any]] = []
        for action in check.get("actions") or []:
            if not isinstance(action, dict):
                continue
            if "target" in action and "selector" not in action:
                action["selector"] = action.pop("target")
            kind = action.get("action")
            if (
                kind == "drag_and_drop"
                and "selector" in action
                and "source_selector" not in action
            ):
                action["source_selector"] = action.pop("selector")
            if kind == "drag_and_drop" and not str(
                action.get("target_selector") or ""
            ).strip():
                source_selector = str(action.get("source_selector") or "").strip()
                ordinal = re.fullmatch(r"(.+):nth-child\((\d+)\)", source_selector)
                first = re.fullmatch(r"(.+):first-child", source_selector)
                if ordinal is not None:
                    source_index = int(ordinal.group(2))
                    target_index = 2 if source_index == 1 else 1
                    action["target_selector"] = (
                        f"{ordinal.group(1)}:nth-child({target_index})"
                    )
                elif first is not None:
                    action["target_selector"] = f"{first.group(1)}:nth-child(2)"
            if kind == "select_option" and "option" in action and "value" not in action:
                action["value"] = action.pop("option")
            if kind in {
                "assert_hash",
                "assert_no_console_errors",
                "assert_url",
                "emulate_media",
                "reload",
                "scroll",
                "set_viewport",
            }:
                action.pop("selector", None)
            if kind == "assert_hash":
                action.pop("name", None)
            if kind == "assert_url" and action.get("value") == "":
                action = {
                    "action": "assert_hash",
                    "value": "",
                    "match": "nonempty",
                    **({"settle_ms": action["settle_ms"]} if "settle_ms" in action else {}),
                }
                kind = "assert_hash"
            if kind == "reload":
                action = {"action": "reload"}
            if kind == "scroll" and "y" not in action:
                # Some OpenAI-compatible providers emit the documented scroll
                # action without its scalar. Preserve the intended scroll
                # precondition with one bounded viewport-sized default rather
                # than discarding an otherwise valid atomic plan (or paying for
                # the planner to rewrite the complete bundle).
                action["y"] = 600
            if isinstance(action.get("selector"), str):
                action["selector"] = stable_selector(
                    str(action["selector"]), str(kind or "")
                )
            if kind == "assert_attribute" and "attribute" in action and "name" not in action:
                action["name"] = action.pop("attribute")
            if kind == "assert_text" and "text" in action and "value" not in action:
                action["value"] = action.pop("text")
            if kind == "assert_count" and "value" in action and "count" not in action:
                action["count"] = action.pop("value")
            if kind == "assert_text":
                action.setdefault("match", "contains")
                if action.get("match") == "nonempty" and "value" not in action:
                    action["value"] = ""
            if kind in {"set_storage_value", "assert_storage_value"}:
                if "name" in action and "key" not in action:
                    action["key"] = action.pop("name")
                action.setdefault("storage", "local")
                if (
                    kind == "assert_storage_value"
                    and isinstance(action.get("value"), str)
                ):
                    action.setdefault("match", "contains")
            if kind == "assert_visible" and isinstance(action.get("selector"), str):
                head_resource = re.fullmatch(
                    r"(link|script)\[(href|src)=(['\"])([^'\"]+)\3\]",
                    str(action["selector"]).strip(),
                )
                if head_resource and (
                    (head_resource.group(1), head_resource.group(2))
                    in {("link", "href"), ("script", "src")}
                ):
                    action["action"] = "assert_attribute"
                    action["name"] = head_resource.group(2)
                    action["value"] = head_resource.group(4)
            normalized_actions.append(action)

        # Route ownership is pathname-based. If a provider encodes a filter's
        # initial state in the route fragment, turn it into an executable
        # select/hash/reload/value flow instead of silently losing that setup.
        if route_fragment and not any(
            action.get("action") in {"fill", "select_option"}
            for action in normalized_actions
            if isinstance(action, dict)
        ):
            value_assertion = next(
                (
                    action
                    for action in normalized_actions
                    if isinstance(action, dict)
                    and action.get("action") == "assert_value"
                    and str(action.get("value") or "") in route_fragment
                    and "filter" in str(action.get("selector") or "").casefold()
                ),
                None,
            )
            if value_assertion is not None:
                navigation = [
                    action
                    for action in normalized_actions
                    if isinstance(action, dict)
                    and action.get("action") == "click"
                    and re.match(
                        r"^a(?:\b|[.#\[])",
                        str(action.get("selector") or "").strip(),
                        re.IGNORECASE,
                    )
                ]
                normalized_actions = navigation + [
                    {
                        "action": "select_option",
                        "selector": value_assertion["selector"],
                        "value": value_assertion["value"],
                    },
                    {"action": "assert_hash", "value": "#" + route_fragment},
                    {"action": "reload"},
                    value_assertion,
                ]
                check["category"] = "persistence"

        if any(
            isinstance(action, dict) and action.get("action") == "reload"
            for action in normalized_actions
        ):
            check["category"] = "persistence"

        state_seen = False
        for action_index, action in enumerate(normalized_actions):
            kind = action.get("action")
            if (
                kind == "assert_count"
                and not check.get("fixtures")
                and not any(prior.get("action") == "set_input_files" for prior in normalized_actions[:action_index])
                and not count_is_instruction_grounded(action.get("count"))
            ):
                # An exact source count without fixtures is usually guessed
                # rather than required by the instruction. Presence is the
                # strongest provider-independent claim available at planning
                # time. Explicit instruction cardinalities and zero-result
                # postconditions remain exact.
                normalized_actions[action_index] = {
                    "action": "assert_visible",
                    "selector": action.get("selector"),
                    **(
                        {"settle_ms": action["settle_ms"]}
                        if "settle_ms" in action
                        else {}
                    ),
                }
            selector = str(action.get("selector") or "").strip()
            navigation_click = (
                kind == "click"
                and re.match(r"^a(?:\b|[.#\[])", selector, re.IGNORECASE) is not None
            )
            if kind in {
                "click",
                "drag_and_drop",
                "fill",
                "key_press",
                "select_option",
                "set_hash",
                "set_input_files",
                "set_storage_value",
            } and not navigation_click:
                state_seen = True
        prior_check_has_state_setup = prior_check_has_state_setup or state_seen
        check["actions"] = normalized_actions
        actions = normalized_actions
        last_assertion = next(
            (
                index
                for index in range(len(actions) - 1, -1, -1)
                if isinstance(actions[index], dict)
                and actions[index].get("action") in TYPED_ASSERTION_ACTIONS
            ),
            None,
        )
        if last_assertion is not None and last_assertion < len(actions) - 1:
            check["actions"] = actions[: last_assertion + 1]
        normalized_checks.extend(split_bounded_assertion_checks(check))
    if len(normalized_checks) > 1:
        first = normalized_checks[0]
        first["actions"] = [
            action
            for item in normalized_checks
            for action in item.get("actions") or []
        ]
        normalized_checks = [first]
    output["checks"] = normalized_checks
    return output


def _compatibility_spec(instruction_delta: str, plan: dict[str, Any]) -> str:
    """Keep old visual/export readers working without another model-authored spec."""
    return (
        f"# Atomic Edit - {plan['title']}\n\n"
        "## Product Overview\n"
        "This run extends an already accepted frontend. The accepted project remains the "
        "authoritative product specification.\n\n"
        "## Target Users\n"
        "The existing product users affected by this one requested change.\n\n"
        "## Feature Descriptions\n"
        f"{instruction_delta.strip()}\n\n"
        "## Technical Architecture\n"
        "Preserve the accepted stack and change only the route-scoped source selected by the Harness.\n\n"
        "## Visual Design Direction\n"
        "Preserve the accepted visual system outside the target surface; the Edit instruction and "
        "reference inputs control any target-local visual change.\n"
    )


def materialize_atomic_edit_compatibility_bundle(
    *,
    file_comm: FileComm,
    instruction_delta: str,
    plan: dict[str, Any],
) -> None:
    """Create the old planning views locally from one validated atomic plan."""
    validated = AtomicEditPlan.model_validate(plan).model_dump(exclude_unset=True)
    file_comm.write_spec(_compatibility_spec(instruction_delta, validated))
    file_comm.write_design_tokens(
        {
            "theme_name": "accepted-project-preservation",
            "color": {},
            "typography": {},
            "spacing": {},
            "radius": {},
            "motion": {},
            "style_rules": [
                "Preserve accepted project tokens and styling outside the target surface."
            ],
            "anti_patterns": ["Global restyling for a route-local Edit."],
            "visual_experiment": {
                "design_hypothesis": "A target-local Edit should inherit the accepted visual system.",
                "reason_for_image_first": "A supplied reference image, when present, controls only the target surface.",
                "desired_break_from_web_templates": ["Preserve the existing product identity."],
                "visual_opportunities_beyond_css": ["Use supplied target references when present."],
                "forbidden_generic_patterns": ["Unrequested project-wide redesign."],
            },
        }
    )
    file_comm.write_feature_list(
        {
            "features": [
                {
                    "id": ATOMIC_EDIT_FEATURE_ID,
                    "name": validated["title"],
                    "priority": "high",
                    "depends_on": [],
                    "description": validated["goal"],
                    "acceptance_criteria": list(validated["exit_criteria"]),
                    "status": "planned",
                    "sprint": 1,
                }
            ]
        }
    )
    file_comm.write_sprint_plan(
        {
            "total_sprints": 1,
            "sprints": [
                {
                    "number": 1,
                    "title": validated["title"],
                    "goal": validated["goal"],
                    "feature_ids": [ATOMIC_EDIT_FEATURE_ID],
                    "deliverables": validated["deliverables"],
                    "exit_criteria": validated["exit_criteria"],
                    "requirement_changes": validated["requirement_changes"],
                    "impact_tags": validated["impact_tags"],
                    "unresolved_conflicts": validated["unresolved_conflicts"],
                    "visual_evidence": validated["visual_evidence"],
                    "visual_evidence_reason": validated["visual_evidence_reason"],
                }
            ],
        }
    )
    checks = []
    for item in validated["checks"]:
        checks.append({"feature_id": ATOMIC_EDIT_FEATURE_ID, **item})
    file_comm.write_ui_verification_plan(
        {"sprints": [{"sprint": 1, "checks": checks}]}
    )
    file_comm.write_progress(
        "# Progress Log\n\nAtomic Edit plan validated and expanded into Harness-owned compatibility views.\n"
    )


__all__ = [
    "ATOMIC_EDIT_FEATURE_ID",
    "ATOMIC_EDIT_PLAN_NAME",
    "AtomicEditCheck",
    "AtomicEditPlan",
    "atomic_edit_plan_path",
    "materialize_atomic_edit_compatibility_bundle",
    "normalize_atomic_edit_plan_payload",
    "read_atomic_edit_plan",
    "write_atomic_edit_plan",
]
