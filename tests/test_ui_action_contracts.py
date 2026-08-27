from __future__ import annotations

import pytest

from src.orchestration.ui_action_contracts import (
    ActionContractError,
    TYPED_ASSERTION_ACTIONS,
    validate_ui_action,
    validate_ui_action_sequence,
)


def test_tab_focus_sequence_must_assert_the_destination():
    with pytest.raises(ActionContractError, match="destination selector"):
        validate_ui_action_sequence([
            {"action": "key_press", "selector": "#save", "key": "Tab"},
            {"action": "assert_focus", "selector": "#save"},
        ])


def test_tab_requires_a_deterministic_starting_selector():
    with pytest.raises(ActionContractError, match="starting selector"):
        validate_ui_action_sequence([
            {"action": "key_press", "key": "Tab"},
            {"action": "assert_focus", "selector": "#query"},
        ])


def test_new_url_assertions_reject_hash_router_state():
    with pytest.raises(ActionContractError, match="fragments are unsupported"):
        validate_ui_action(
            {"action": "assert_url", "value": "#/library", "match": "contains"}
        )


@pytest.mark.parametrize(
    "step",
    [
        {"action": "assert_visible", "selector": "#save"},
        {"action": "assert_hidden", "selector": "[role=dialog]"},
        {"action": "assert_text", "selector": "#status", "value": "Saved", "match": "contains"},
        {"action": "assert_value", "selector": "#query", "value": "atlas"},
        {"action": "assert_count", "selector": ".result", "count": 3},
        {"action": "assert_url", "value": "/settings", "match": "contains"},
        {"action": "assert_attribute", "selector": "#save", "name": "data-state", "value": "done"},
        {"action": "assert_property", "selector": "#done", "name": "checked", "value": True},
        {"action": "assert_aria", "selector": "#toggle", "attribute": "aria-expanded", "value": True},
        {"action": "assert_focus", "selector": "#query"},
        {
            "action": "assert_storage_value",
            "storage": "local",
            "key": "theme",
            "value": "dark",
            "match": "contains",
        },
        {"action": "assert_no_console_errors"},
    ],
)
def test_typed_assertion_contracts_are_bounded(step):
    assert step["action"] in TYPED_ASSERTION_ACTIONS
    validate_ui_action(step)


def test_reload_is_a_bounded_interaction_action():
    validate_ui_action({"action": "reload"})
    with pytest.raises(ActionContractError, match="unsupported fields"):
        validate_ui_action({"action": "reload", "url": "/other"})


def test_assert_aria_rejects_unbounded_property_access():
    with pytest.raises(ActionContractError, match=r"role, accessible_name, or an aria-\*"):
        validate_ui_action(
            {
                "action": "assert_aria",
                "selector": "#toggle",
                "attribute": "innerHTML",
                "value": "anything",
            }
        )


def test_assert_property_allows_only_bounded_browser_state():
    validate_ui_action(
        {
            "action": "assert_property",
            "selector": "#submit",
            "name": "disabled",
            "value": True,
        }
    )
    with pytest.raises(ActionContractError, match="property name"):
        validate_ui_action(
            {
                "action": "assert_property",
                "selector": "#submit",
                "name": "innerHTML",
                "value": "anything",
            }
        )
