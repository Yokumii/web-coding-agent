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
    actions: list[dict[str, Any]] = Field(min_length=1, max_length=16)


class AtomicEditPlan(BaseModel):
    """The only semantic artifact authored by the Edit Planner."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["atomic-edit-plan-v1"] = "atomic-edit-plan-v1"
    title: NonEmptyString
    goal: NonEmptyString
    source_anchors: list[NonEmptyString] = Field(default_factory=list, max_length=12)
    deliverables: list[NonEmptyString] = Field(min_length=1, max_length=10)
    exit_criteria: list[NonEmptyString] = Field(min_length=1, max_length=10)
    requirement_changes: list[RequirementChange] = Field(min_length=1, max_length=10)
    impact_tags: list[NonEmptyString] = Field(min_length=1, max_length=10)
    unresolved_conflicts: list[RequirementConflict] = Field(default_factory=list)
    visual_evidence: Literal["required", "conditional", "not_required"]
    visual_evidence_reason: NonEmptyString
    checks: list[AtomicEditCheck] = Field(min_length=1, max_length=10)


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
) -> Path:
    plan = AtomicEditPlan.model_validate(
        normalize_atomic_edit_plan_payload(
            payload, instruction_delta=instruction_delta
        )
    )
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

    for index, check in enumerate(output.get("checks") or [], start=1):
        if not isinstance(check, dict):
            continue
        check.setdefault("id", f"EDIT-CHECK-{index}")
        check.setdefault("task", "Verify the requested atomic Edit.")
        check.setdefault("expected_result", "All target browser assertions pass.")
        check.setdefault("critical", True)
        check.setdefault("category", "functional")
        check.setdefault("route", "/")
        check.setdefault("fixtures", [])
        if not str(check.get("requirement_id") or "").strip():
            check["requirement_id"] = requirement_id
        if not check.get("impact_tags"):
            check["impact_tags"] = plan_impact_tags
        for action in check.get("actions") or []:
            if not isinstance(action, dict):
                continue
            if "target" in action and "selector" not in action:
                action["selector"] = action.pop("target")
            kind = action.get("action")
            if isinstance(action.get("selector"), str):
                action["selector"] = stable_selector(
                    str(action["selector"]), str(kind or "")
                )
            if kind == "assert_attribute" and "attribute" in action and "name" not in action:
                action["name"] = action.pop("attribute")
            if kind == "assert_text" and "text" in action and "value" not in action:
                action["value"] = action.pop("text")
        actions = check.get("actions") or []
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
