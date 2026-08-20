"""Typed, bounded browser action contracts shared by planning and execution."""
from __future__ import annotations

from pathlib import PurePath
from typing import Any


SUPPORTED_UI_ACTIONS = frozenset(
    {
        "assert_form_valid",
        "click",
        "drag_and_drop",
        "emulate_media",
        "evaluate",
        "fill",
        "hover",
        "key_press",
        "scroll",
        "select_option",
        "set_input_files",
        "set_viewport",
        "wait_for",
    }
)

_ACTION_FIELDS = {
    "assert_form_valid": {"selector"},
    "click": {"selector", "button"},
    "drag_and_drop": {"source_selector", "target_selector"},
    "emulate_media": {"media", "color_scheme"},
    "evaluate": {"expression"},
    "fill": {"selector", "value"},
    "hover": {"selector"},
    "key_press": {"selector", "key", "count"},
    "scroll": {"y"},
    "select_option": {"selector", "value"},
    "set_input_files": {"selector", "files"},
    "set_viewport": {"width", "height"},
    "wait_for": {"selector", "state", "timeout_ms"},
}


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
        "assert_form_valid",
        "click",
        "fill",
        "hover",
        "select_option",
        "set_input_files",
        "wait_for",
    }:
        _non_empty_string(step, "selector", action)
    if action == "set_viewport":
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
    elif action == "key_press":
        _non_empty_string(step, "key", action)
        if "selector" in step:
            _non_empty_string(step, "selector", action)
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


__all__ = ["ActionContractError", "SUPPORTED_UI_ACTIONS", "validate_ui_action"]
