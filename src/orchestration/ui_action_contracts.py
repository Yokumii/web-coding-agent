"""Typed, bounded browser action contracts shared by planning and execution."""
from __future__ import annotations

import json
from pathlib import PurePath
from typing import Any


TYPED_ASSERTION_ACTIONS = frozenset(
    {
        "assert_aria",
        "assert_attribute",
        "assert_count",
        "assert_computed_style",
        "assert_focus",
        "assert_hash",
        "assert_hidden",
        "assert_property",
        "assert_no_console_errors",
        "assert_storage_value",
        "assert_text",
        "assert_url",
        "assert_value",
        "assert_visible",
    }
)

# ``evaluate`` remains executable only so historical tapes can be replayed and
# audited.  New planner output is required to end in one of the bounded typed
# assertions above; model-authored JavaScript is not a current contract format.
LEGACY_ASSERTION_ACTIONS = frozenset({"evaluate"})
ASSERTION_UI_ACTIONS = TYPED_ASSERTION_ACTIONS | LEGACY_ASSERTION_ACTIONS

SUPPORTED_UI_ACTIONS = frozenset(
    {
        *TYPED_ASSERTION_ACTIONS,
        "assert_form_valid",
        "click",
        "drag_and_drop",
        "emulate_media",
        "evaluate",
        "fill",
        "hover",
        "key_press",
        "reload",
        "scroll",
        "select_option",
        "set_storage_value",
        "set_input_files",
        "set_viewport",
        "wait_for",
    }
)

_ACTION_FIELDS = {
    "assert_aria": {"selector", "attribute", "value"},
    "assert_attribute": {"selector", "name", "value"},
    "assert_count": {"selector", "count"},
    "assert_computed_style": {"selector", "property", "value", "match"},
    "assert_focus": {"selector"},
    "assert_hash": {"value", "match"},
    "assert_form_valid": {"selector"},
    "assert_hidden": {"selector"},
    "assert_property": {"selector", "name", "value"},
    "assert_no_console_errors": set(),
    "assert_storage_value": {"storage", "key", "value", "match"},
    "assert_text": {"selector", "value", "match"},
    "assert_url": {"value", "match"},
    "assert_value": {"selector", "value"},
    "assert_visible": {"selector"},
    "click": {"selector", "button"},
    "drag_and_drop": {"source_selector", "target_selector"},
    "emulate_media": {"media", "color_scheme"},
    "evaluate": {"expression"},
    "fill": {"selector", "value"},
    "hover": {"selector"},
    "key_press": {"selector", "key", "count"},
    "reload": set(),
    "scroll": {"y"},
    "select_option": {"selector", "value"},
    "set_storage_value": {"storage", "key", "value", "encoding"},
    "set_input_files": {"selector", "files"},
    "set_viewport": {"width", "height"},
    "wait_for": {"selector", "state", "timeout_ms"},
}

_COMPUTED_STYLE_PROPERTIES = frozenset(
    {
        "display",
        "visibility",
        "opacity",
        "position",
        "overflow",
        "overflow-x",
        "overflow-y",
        "pointer-events",
        "z-index",
    }
)


class ActionContractError(ValueError):
    """A planner action is malformed and must not become a product failure."""


def _non_empty_string(step: dict[str, Any], field: str, action: str) -> str:
    value = step.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ActionContractError(f"{action} requires non-empty {field}")
    return value


def _bounded_int(
    step: dict[str, Any], field: str, action: str, *, minimum: int, maximum: int
) -> int:
    value = step.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ActionContractError(
            f"{action} {field} must be an integer from {minimum} to {maximum}"
        )
    return value


def validate_ui_action(step: dict[str, Any]) -> None:
    """Fail closed on unsupported, ambiguous, or unbounded browser actions."""
    if not isinstance(step, dict):
        raise ActionContractError("action step must be an object")
    action = step.get("action")
    if not isinstance(action, str) or action not in SUPPORTED_UI_ACTIONS:
        raise ActionContractError(f"unsupported action {action!r}")
    allowed = {"action", "settle_ms"} | _ACTION_FIELDS[action]
    extras = sorted(set(step) - allowed)
    if extras:
        raise ActionContractError(
            f"{action} contains unsupported fields: {', '.join(extras)}"
        )
    if "settle_ms" in step:
        _bounded_int(step, "settle_ms", action, minimum=0, maximum=5_000)

    if action in {
        "assert_aria",
        "assert_attribute",
        "assert_count",
        "assert_computed_style",
        "assert_focus",
        "assert_form_valid",
        "assert_hidden",
        "assert_property",
        "assert_text",
        "assert_value",
        "assert_visible",
        "click",
        "fill",
        "hover",
        "select_option",
        "set_input_files",
        "wait_for",
    }:
        _non_empty_string(step, "selector", action)
    if action == "assert_aria":
        attribute = _non_empty_string(step, "attribute", action)
        if attribute != "role" and attribute != "accessible_name" and not attribute.startswith("aria-"):
            raise ActionContractError(
                "assert_aria attribute must be role, accessible_name, or an aria-* attribute"
            )
        if not isinstance(step.get("value"), (str, bool)):
            raise ActionContractError("assert_aria requires string or boolean value")
    elif action == "assert_attribute":
        _non_empty_string(step, "name", action)
        if not isinstance(step.get("value"), (str, bool, int, float)):
            raise ActionContractError("assert_attribute requires scalar value")
    elif action == "assert_count":
        _bounded_int(step, "count", action, minimum=0, maximum=10_000)
    elif action == "assert_computed_style":
        property_name = _non_empty_string(step, "property", action).lower()
        if property_name not in _COMPUTED_STYLE_PROPERTIES:
            raise ActionContractError(
                "assert_computed_style computed style property must be one of: "
                + ", ".join(sorted(_COMPUTED_STYLE_PROPERTIES))
            )
        if not isinstance(step.get("value"), (str, int, float)):
            raise ActionContractError("assert_computed_style requires scalar value")
        if step.get("match", "exact") not in {"exact", "contains"}:
            raise ActionContractError(
                "assert_computed_style match must be exact or contains"
            )
    elif action == "assert_property":
        name = _non_empty_string(step, "name", action)
        if name not in {
            "checked",
            "disabled",
            "indeterminate",
            "multiple",
            "open",
            "readOnly",
            "required",
            "selected",
            "selectedIndex",
            "value",
        }:
            raise ActionContractError(
                "assert_property property name must be a bounded form/dialog state"
            )
        if not isinstance(step.get("value"), (str, bool, int, float)) and step.get("value") is not None:
            raise ActionContractError("assert_property requires scalar or null value")
    elif action == "assert_storage_value":
        if step.get("storage") not in {"local", "session"}:
            raise ActionContractError("assert_storage_value storage must be local or session")
        _non_empty_string(step, "key", action)
        if not isinstance(step.get("value"), (str, int, float, bool)) and step.get("value") is not None:
            raise ActionContractError("assert_storage_value requires scalar or null value")
        if step.get("match", "exact") not in {"exact", "contains"}:
            raise ActionContractError(
                "assert_storage_value match must be exact or contains"
            )
    elif action in {"assert_text", "assert_url", "assert_hash"}:
        if action == "assert_url":
            expected_url = _non_empty_string(step, "value", action)
            if "#" in expected_url:
                raise ActionContractError(
                    "assert_url fragments are unsupported for new contracts; use an owned pathname route"
                )
        elif action == "assert_hash":
            expected_hash = _non_empty_string(step, "value", action)
            segments = expected_hash.removeprefix("#").split("/")
            if (
                not expected_hash.startswith("#/")
                or "\\" in expected_hash
                or "?" in expected_hash
                or "://" in expected_hash
                or any(segment in {".", ".."} for segment in segments)
            ):
                raise ActionContractError(
                    "assert_hash value must be one bounded hash-router path such as '#/report'"
                )
        elif not isinstance(step.get("value"), (str, int, float)):
            raise ActionContractError("assert_text requires scalar value")
        if step.get("match", "exact") not in {"exact", "contains"}:
            raise ActionContractError(f"{action} match must be exact or contains")
    elif action == "assert_value":
        if not isinstance(step.get("value"), (str, int, float)):
            raise ActionContractError("assert_value requires scalar value")
    elif action == "set_viewport":
        _bounded_int(step, "width", action, minimum=240, maximum=4_096)
        _bounded_int(step, "height", action, minimum=200, maximum=4_096)
    elif action == "click":
        if step.get("button", "left") not in {"left", "right", "middle"}:
            raise ActionContractError("click button must be left, right, or middle")
    elif action == "drag_and_drop":
        _non_empty_string(step, "source_selector", action)
        _non_empty_string(step, "target_selector", action)
    elif action == "emulate_media":
        media = step.get("media")
        color_scheme = step.get("color_scheme")
        if media is None and color_scheme is None:
            raise ActionContractError("emulate_media requires media or color_scheme")
        if media is not None and media not in {"screen", "print"}:
            raise ActionContractError("emulate_media media must be screen or print")
        if color_scheme is not None and color_scheme not in {
            "light",
            "dark",
            "no-preference",
        }:
            raise ActionContractError(
                "emulate_media color_scheme must be light, dark, or no-preference"
            )
    elif action == "evaluate":
        _non_empty_string(step, "expression", action)
    elif action in {"fill", "select_option"}:
        if "value" not in step or not isinstance(step["value"], (str, int, float)):
            raise ActionContractError(f"{action} requires scalar value")
    elif action == "set_storage_value":
        if step.get("storage") not in {"local", "session"}:
            raise ActionContractError("set_storage_value storage must be local or session")
        _non_empty_string(step, "key", action)
        if "value" not in step:
            raise ActionContractError("set_storage_value requires value")
        encoding = step.get("encoding", "string")
        if encoding not in {"json", "string"}:
            raise ActionContractError(
                "set_storage_value encoding must be json or string"
            )
        try:
            encoded = (
                json.dumps(step.get("value"), ensure_ascii=False, separators=(",", ":"))
                if encoding == "json"
                else str(step.get("value", ""))
            )
        except (TypeError, ValueError) as exc:
            raise ActionContractError(
                "set_storage_value value must be JSON serializable"
            ) from exc
        if len(encoded.encode("utf-8")) > 32_768:
            raise ActionContractError(
                "set_storage_value value exceeds the 32768-byte budget"
            )
    elif action == "key_press":
        key = _non_empty_string(step, "key", action)
        if "selector" in step:
            _non_empty_string(step, "selector", action)
        elif key == "Tab":
            raise ActionContractError(
                "key_press Tab requires a starting selector so focus order is deterministic"
            )
        if "count" in step:
            _bounded_int(step, "count", action, minimum=1, maximum=20)
    elif action == "scroll":
        _bounded_int(step, "y", action, minimum=-100_000, maximum=100_000)
    elif action == "set_input_files":
        files = step.get("files")
        if not isinstance(files, list) or not 1 <= len(files) <= 3:
            raise ActionContractError("set_input_files files must contain 1 to 3 fixtures")
        total_bytes = 0
        for fixture in files:
            if not isinstance(fixture, dict) or set(fixture) != {
                "name",
                "mime_type",
                "content",
            }:
                raise ActionContractError(
                    "set_input_files fixtures require name, mime_type, and content only"
                )
            name = _non_empty_string(fixture, "name", "set_input_files fixture")
            if PurePath(name).name != name or "/" in name or "\\" in name:
                raise ActionContractError(
                    "set_input_files fixture name must be a safe base name"
                )
            _non_empty_string(fixture, "mime_type", "set_input_files fixture")
            content = fixture.get("content")
            if not isinstance(content, str):
                raise ActionContractError("set_input_files fixture content must be text")
            total_bytes += len(content.encode("utf-8"))
        if total_bytes > 32_768:
            raise ActionContractError(
                "set_input_files fixture content exceeds the 32768-byte budget"
            )
    elif action == "wait_for":
        if step.get("state", "visible") not in {
            "attached",
            "detached",
            "visible",
            "hidden",
        }:
            raise ActionContractError(
                "wait_for state must be attached, detached, visible, or hidden"
            )
        if "timeout_ms" in step:
            _bounded_int(step, "timeout_ms", action, minimum=1, maximum=5_000)


def validate_ui_action_sequence(actions: list[dict[str, Any]]) -> None:
    """Reject cross-step contradictions that would manufacture product failures."""
    if not isinstance(actions, list) or not actions:
        raise ActionContractError("action sequence must be a non-empty list")
    for step in actions:
        validate_ui_action(step)
    for previous, current in zip(actions, actions[1:]):
        if (
            previous.get("action") == "key_press"
            and previous.get("key") == "Tab"
            and isinstance(previous.get("selector"), str)
            and current.get("action") == "assert_focus"
            and current.get("selector") == previous.get("selector")
        ):
            raise ActionContractError(
                "Tab moves focus away from its starting selector; the following "
                "assert_focus must name the destination selector"
            )


__all__ = [
    "ASSERTION_UI_ACTIONS",
    "ActionContractError",
    "LEGACY_ASSERTION_ACTIONS",
    "SUPPORTED_UI_ACTIONS",
    "TYPED_ASSERTION_ACTIONS",
    "validate_ui_action",
    "validate_ui_action_sequence",
]
