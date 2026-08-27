"""Materialize one concise, harness-owned description of an Edit transaction."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


EDIT_CARD_NAME = "edit_card.json"
_VISUAL_POLICIES = {"required", "conditional", "not_required"}
_VISUAL_CHECK_CATEGORIES = {
    "appearance",
    "canvas",
    "image",
    "layout",
    "responsive",
    "style",
    "visual",
}


class EditCardBlockedError(RuntimeError):
    """The planner found a requirement conflict that needs external resolution."""


def _unique_strings(values: list[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in output:
            output.append(text)
    return output


def read_edit_card(harness_dir: Path) -> dict[str, Any] | None:
    path = Path(harness_dir) / EDIT_CARD_NAME
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "edit-card-v1":
        raise ValueError(f"unsupported Edit card: {path}")
    return payload


def visual_evidence_required(
    card: dict[str, Any] | None, *, has_image_input: bool = False
) -> bool:
    if not card:
        return True
    policy = card.get("visual_evidence")
    if policy == "required":
        return True
    if policy == "not_required":
        return False
    categories = {
        str(category).strip().lower()
        for category in card.get("target_check_categories") or []
    }
    return has_image_input or bool(categories & _VISUAL_CHECK_CATEGORIES)


def materialize_edit_card(
    *,
    harness_dir: Path,
    instruction_delta: str,
    edit_contract: dict[str, Any],
    sprint_plan: dict[str, Any],
    verification_plan: dict[str, Any],
) -> dict[str, Any]:
    """Bind planner semantics to exact routes/checks without another model call."""
    if int(sprint_plan.get("total_sprints") or 0) != 1 or len(sprint_plan.get("sprints") or []) != 1:
        raise ValueError(
            "One Edit instruction must be exactly one Sprint; split oversized requests before the harness."
        )
    sprint = sprint_plan["sprints"][0]
    verification_sprints = verification_plan.get("sprints") or []
    if len(verification_sprints) != 1 or int(verification_sprints[0].get("sprint") or 0) != 1:
        raise ValueError("One Edit transaction requires exactly one verification Sprint.")
    checks = verification_sprints[0].get("checks") or []
    if not checks:
        raise ValueError("Edit card requires at least one executable target check.")

    requested_routes = _unique_strings(edit_contract.get("requested_target_routes") or [])
    check_routes = _unique_strings([check.get("route", "/") for check in checks])
    if requested_routes and not set(check_routes) <= set(requested_routes):
        raise ValueError("Edit verification routes exceed the requested target routes.")
    target_routes = requested_routes or check_routes
    impact_tags = _unique_strings([
        *(sprint.get("impact_tags") or []),
        *(tag for check in checks for tag in (check.get("impact_tags") or [])),
    ])
    if not impact_tags:
        raise ValueError("Edit card requires planner-authored impact_tags.")

    changes = list(sprint.get("requirement_changes") or [])
    if not changes:
        raise ValueError("Edit card requires at least one requirement change.")
    retired: list[str] = []
    for change in changes:
        if change.get("relation") in {"replace", "withdraw"}:
            retired.extend(str(item) for item in change.get("prior_requirement_ids") or [])

    conflicts = list(sprint.get("unresolved_conflicts") or [])
    visual_policy = str(sprint.get("visual_evidence") or "conditional")
    if visual_policy not in _VISUAL_POLICIES:
        raise ValueError(f"unsupported visual_evidence policy: {visual_policy!r}")
    card = {
        "schema_version": "edit-card-v1",
        "owner": "harness",
        "status": "blocked" if conflicts else "ready",
        "instruction_delta": instruction_delta,
        "target_routes": target_routes,
        "target_feature_ids": _unique_strings(sprint.get("feature_ids") or []),
        "target_check_ids": _unique_strings([check.get("id") for check in checks]),
        "target_check_categories": _unique_strings(
            [check.get("category") for check in checks]
        ),
        "requirement_changes": changes,
        "retired_requirement_ids": _unique_strings(retired),
        "impact_tags": impact_tags,
        "unresolved_conflicts": conflicts,
        "visual_evidence": visual_policy,
        "visual_evidence_reason": str(sprint.get("visual_evidence_reason") or "").strip(),
    }
    harness_dir = Path(harness_dir)
    harness_dir.mkdir(parents=True, exist_ok=True)
    (harness_dir / EDIT_CARD_NAME).write_text(
        json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if conflicts:
        raise EditCardBlockedError(
            "Edit has an unresolved requirement conflict; stop before source mutation."
        )
    return card


__all__ = [
    "EDIT_CARD_NAME",
    "EditCardBlockedError",
    "materialize_edit_card",
    "read_edit_card",
    "visual_evidence_required",
]
