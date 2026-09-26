"""Immutable acceptance bundle for one 4--12 subtask compound Edit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.orchestration.atomic_edit_plan import (
    AtomicEditPlan,
    normalize_atomic_edit_plan_payload,
)
from src.orchestration.schemas import NonEmptyString
from src.orchestration.ui_action_contracts import (
    validate_ui_action,
    validate_ui_action_sequence,
)


FROZEN_COMPOUND_PLAN_NAME = "frozen_compound_edit_plan.json"


class FrozenCompoundSubtask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: NonEmptyString
    task_type: NonEmptyString
    instruction: NonEmptyString
    target_routes: list[NonEmptyString] = Field(min_length=1, max_length=12)
    atomic_plan: AtomicEditPlan

    @model_validator(mode="after")
    def validate_plan_binding(self) -> "FrozenCompoundSubtask":
        if self.atomic_plan.goal != self.instruction:
            raise ValueError("atomic plan goal must equal the frozen instruction")
        routes = set(self.target_routes)
        for check in self.atomic_plan.checks:
            if check.route not in routes:
                raise ValueError(
                    f"check route {check.route!r} is outside target_routes"
                )
            for action in check.actions:
                validate_ui_action(action)
            validate_ui_action_sequence(check.actions)
        return self


class FrozenCompoundEditPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["frozen-compound-edit-plan-v1"]
    case_id: NonEmptyString
    source_code_sha256: NonEmptyString
    planner_model: NonEmptyString
    subtasks: list[FrozenCompoundSubtask] = Field(min_length=4, max_length=12)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "FrozenCompoundEditPlan":
        ids = [item.id for item in self.subtasks]
        if len(ids) != len(set(ids)):
            raise ValueError("frozen compound subtask ids must be unique")
        return self


def canonical_plan_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def plan_from_atomic_contracts(
    *,
    case_id: str,
    source_code_sha256: str,
    frozen_subtasks: list[dict[str, str]],
    source_ui_contract: dict[str, Any],
    source_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Bind dataset-owned atomic Edits without asking an LLM to re-plan them."""
    observed_routes = [
        str(page.get("route") or "").strip()
        for page in source_ui_contract.get("pages") or []
        if isinstance(page, dict) and str(page.get("route") or "").strip()
    ]
    inferred_routes: list[str] = []
    for raw_path in source_paths or []:
        path = Path(str(raw_path))
        stem = re.sub(r"(?:page|view|screen)$", "", path.stem, flags=re.I)
        stem = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", stem).lower()
        if path.suffix.lower() in {".html", ".htm"}:
            inferred_routes.append("/" if stem in {"index", "home"} else f"/{stem}")
        elif path.suffix.lower() in {".js", ".jsx", ".ts", ".tsx", ".vue"} and re.search(
            r"(?:page|view|screen)$", path.stem, re.I
        ):
            inferred_routes.append("/" if stem in {"index", "home"} else f"/{stem}")
    routes = list(dict.fromkeys([*observed_routes, *inferred_routes])) or ["/"]

    def target_route(instruction: str) -> str:
        lowered = instruction.lower()
        matches: list[tuple[int, int, str]] = []
        for route in routes:
            route_terms = [
                term
                for term in re.split(r"[^a-z0-9]+", urlsplit(route).path.lower())
                if term and term not in {"html", "index"}
            ]
            if route_terms and all(term in lowered for term in route_terms):
                phrase = " ".join(route_terms)
                explicit_page = int(
                    any(
                        marker in lowered
                        for marker in (
                            f"{phrase} page",
                            f"{phrase} route",
                            f"{phrase} screen",
                            f"{phrase} view",
                        )
                    )
                )
                matches.append((explicit_page, len(route_terms), route))
        if matches:
            return max(matches, key=lambda item: (item[0], item[1], -routes.index(item[2])))[2]
        return "/" if "/" in routes else routes[0]

    raw = {
        "subtasks": [
            {
                "id": item["id"],
                "target_routes": [route := target_route(item["instruction"])],
                "atomic_plan": {
                    "goal": item["instruction"],
                    "source_anchors": [],
                    "visual_evidence": "not_required",
                    "visual_evidence_reason": (
                        "Production acceptance is owned by reverse/validate."
                    ),
                    "checks": [
                        {
                            "id": f"{item['id']}-runtime",
                            "task": "Keep the edited page free of fatal runtime errors.",
                            "expected_result": "The page has no console errors.",
                            "category": "runtime",
                            "requirement_id": "REQ-EDIT-001",
                            "impact_tags": ["atomic-edit"],
                            "route": route,
                            "actions": [{"action": "assert_no_console_errors"}],
                        }
                    ],
                },
            }
            for item in frozen_subtasks
        ]
    }
    return normalize_frozen_compound_plan(
        raw,
        case_id=case_id,
        source_code_sha256=source_code_sha256,
        planner_model="dataset-atomic-contract",
        frozen_subtasks=frozen_subtasks,
    )


def normalize_frozen_compound_plan(
    payload: dict[str, Any],
    *,
    case_id: str,
    source_code_sha256: str,
    planner_model: str,
    frozen_subtasks: list[dict[str, str]],
) -> dict[str, Any]:
    """Validate exact task identity while filling only atomic compatibility fields."""
    output = json.loads(json.dumps(payload))
    output["schema_version"] = "frozen-compound-edit-plan-v1"
    output["case_id"] = case_id
    output["source_code_sha256"] = source_code_sha256
    output["planner_model"] = planner_model
    planned = output.get("subtasks")
    if not isinstance(planned, list) or len(planned) != len(frozen_subtasks):
        raise ValueError("planner must return one plan for every frozen subtask")
    normalized: list[dict[str, Any]] = []
    for index, (expected, item) in enumerate(zip(frozen_subtasks, planned), 1):
        if not isinstance(item, dict):
            raise ValueError(f"planned subtask {index} is not an object")
        expected_id = str(expected["id"])
        expected_type = str(expected["task_type"])
        expected_instruction = str(expected["instruction"])
        # The dataset owns these fields; the planner need not spend tokens
        # reproducing long instructions. Explicit conflicting values still fail.
        item.setdefault("task_type", expected_type)
        item.setdefault("instruction", expected_instruction)
        for key, value in (
            ("id", expected_id),
            ("task_type", expected_type),
            ("instruction", expected_instruction),
        ):
            if item.get(key) != value:
                raise ValueError(f"planner changed frozen subtask {index} field {key}")
        atomic = item.get("atomic_plan")
        if not isinstance(atomic, dict):
            raise ValueError(f"planned subtask {index} has no atomic_plan")
        atomic["goal"] = expected_instruction
        for check in atomic.get("checks") or []:
            if not isinstance(check, dict):
                continue
            actions = []
            for action in check.get("actions") or []:
                if not isinstance(action, dict) or action.get("action") != "navigate":
                    if isinstance(action, dict):
                        if action.get("action") == "press":
                            action["action"] = "key_press"
                        if action.get("action") == "drag_and_drop":
                            for alias, canonical in (
                                ("source", "source_selector"),
                                ("destination", "target_selector"),
                                ("target", "target_selector"),
                            ):
                                if alias in action and canonical not in action:
                                    action[canonical] = action.pop(alias)
                        if (
                            action.get("action") == "select_option"
                            and "option" in action
                            and "value" not in action
                        ):
                            action["value"] = action.pop("option")
                        if (
                            action.get("action") == "key_press"
                            and "key" not in action
                            and "value" in action
                        ):
                            action["key"] = action.pop("value")
                        if action.get("action") == "scroll" and "y" not in action:
                            amount = int(action.pop("amount", 600))
                            direction = str(action.pop("direction", "down"))
                            action.pop("selector", None)
                            action["y"] = 0 if direction == "up" else amount
                        if (
                            action.get("action") == "assert_url"
                            and "#" in str(action.get("value") or "")
                        ):
                            parsed_url = urlsplit(str(action["value"]))
                            action["value"] = parsed_url.path or str(check.get("route") or "/")
                            actions.append(action)
                            actions.append(
                                {
                                    "action": "assert_hash",
                                    "value": f"#{parsed_url.fragment}",
                                }
                            )
                            continue
                        if (
                            action.get("action") == "assert_property"
                            and "property" in action
                            and "name" not in action
                        ):
                            action["name"] = action.pop("property")
                        if (
                            action.get("action") == "assert_property"
                            and str(action.get("name") or "").startswith(("data-", "aria-"))
                        ):
                            action["action"] = "assert_attribute"
                        if action.get("action") in {
                            "assert_storage_value",
                            "set_storage_value",
                        }:
                            storage_aliases = {
                                "localStorage": "local",
                                "sessionStorage": "session",
                            }
                            if action.get("storage") in storage_aliases:
                                action["storage"] = storage_aliases[action["storage"]]
                    actions.append(action)
                    continue
                destination = str(
                    action.get("route") or action.get("url") or action.get("path") or ""
                ).strip()
                if destination:
                    parsed = urlsplit(destination)
                    if parsed.scheme or parsed.netloc:
                        raise ValueError("compound plan navigate must be same-origin")
                    check["route"] = parsed.path or "/"
            check["actions"] = actions
        atomic = normalize_atomic_edit_plan_payload(
            atomic, instruction_delta=expected_instruction
        )
        normalized.append(
            {
                "id": expected_id,
                "task_type": expected_type,
                "instruction": expected_instruction,
                "target_routes": item.get("target_routes") or ["/"],
                "atomic_plan": atomic,
            }
        )
    output["subtasks"] = normalized
    return FrozenCompoundEditPlan.model_validate(output).model_dump(exclude_unset=True)


def write_frozen_compound_plan(path: Path, payload: dict[str, Any]) -> str:
    validated = FrozenCompoundEditPlan.model_validate(payload).model_dump(
        exclude_unset=True
    )
    digest = canonical_plan_sha256(validated)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {**validated, "frozen_plan_sha256": digest},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return digest


def read_frozen_compound_plan(path: Path) -> tuple[dict[str, Any], str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    recorded = str(raw.pop("frozen_plan_sha256", ""))
    validated = FrozenCompoundEditPlan.model_validate(raw).model_dump(
        exclude_unset=True
    )
    actual = canonical_plan_sha256(validated)
    if not recorded or recorded != actual:
        raise ValueError("frozen compound plan hash mismatch")
    return validated, actual


__all__ = [
    "FROZEN_COMPOUND_PLAN_NAME",
    "FrozenCompoundEditPlan",
    "canonical_plan_sha256",
    "normalize_frozen_compound_plan",
    "plan_from_atomic_contracts",
    "read_frozen_compound_plan",
    "write_frozen_compound_plan",
]
