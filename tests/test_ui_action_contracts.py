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


@pytest.mark.parametrize('bounds',[{'min':float('nan'),'max':1},{'min':2,'max':1},{'min':True,'max':1}])
def test_numeric_assertion_rejects_invalid_bounds(bounds):
    with pytest.raises(ActionContractError):
        validate_ui_action({'action':'assert_number','selector':'progress','property':'value',**bounds})


def test_file_fixture_cannot_expand_without_a_bound():
    with pytest.raises(ActionContractError):
        validate_ui_action({'action':'set_input_files','selector':'input','files':[
            {'name':'huge.txt','mime_type':'text/plain','content':'x','size_bytes':33*1024*1024}]})


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


def test_nonempty_text_and_attribute_assertions_are_bounded():
    validate_ui_action(
        {"action": "assert_text", "selector": "#title", "value": "", "match": "nonempty"}
    )
    validate_ui_action({"action": "assert_hash", "value": "", "match": "nonempty"})
    validate_ui_action({"action": "assert_scroll", "y": 200, "tolerance": 2})
    validate_ui_action({"action": "assert_in_view", "selector": "#driver"})
    validate_ui_action({"action": "click", "selector": "#card", "capture_scroll_as": "before-detail"})
    validate_ui_action({"action": "assert_scroll", "snapshot": "before-detail", "tolerance": 2})
    validate_ui_action(
        {
            "action": "assert_attribute",
            "selector": "#detail",
            "name": "data-id",
            "value": "",
            "match": "nonempty",
        }
    )


def test_scroll_assertion_requires_one_expected_source():
    with pytest.raises(ActionContractError, match="exactly one"):
        validate_ui_action({"action": "assert_scroll"})
    with pytest.raises(ActionContractError, match="exactly one"):
        validate_ui_action({
            "action": "assert_scroll",
            "y": 200,
            "snapshot": "before-detail",
        })


def test_input_value_snapshot_and_match_contract():
    validate_ui_action({"action": "assert_value", "selector": "#filter", "match": "nonempty", "capture_as": "before"})
    validate_ui_action({"action": "assert_value", "selector": "#filter", "snapshot": "before"})
    validate_ui_action({"action": "assert_value", "selector": "#filter", "value": "", "match": "exact"})
    for fields in ({"snapshot": "before", "value": "x"}, {"snapshot": "before", "match": "nonempty"}, {"value": "", "match": "contains"}, {"match": "unknown"}, {"match": "nonempty", "capture_as": ""}):
        with pytest.raises(ActionContractError):
            validate_ui_action({"action": "assert_value", "selector": "#filter", **fields})


def test_attribute_snapshot_actions_require_one_expected_source():
    validate_ui_action(
        {
            "action": "capture_attribute",
            "selector": "#card",
            "name": "data-id",
            "snapshot": "first-card-id",
        }
    )
    validate_ui_action(
        {
            "action": "assert_attribute",
            "selector": "#card",
            "name": "data-id",
            "snapshot": "first-card-id",
        }
    )
    with pytest.raises(ActionContractError, match="exactly one"):
        validate_ui_action(
            {
                "action": "assert_attribute",
                "selector": "#card",
                "name": "data-id",
                "value": "artifact-1",
                "snapshot": "first-card-id",
            }
        )


def test_hash_assertions_and_storage_fixtures_are_bounded():
    validate_ui_action({"action": "set_hash", "value": "#audio-studio-driver"})
    validate_ui_action(
        {"action": "assert_hash", "value": "#/library", "match": "exact"}
    )
    validate_ui_action(
        {"action": "assert_hash", "value": "#type=Timepiece", "match": "exact"}
    )
    validate_ui_action(
        {"action": "assert_hash", "value": "Timepiece", "match": "contains"}
    )
    validate_ui_action({"action": "assert_hash", "value": ""})
    validate_ui_action(
        {
            "action": "set_storage_value",
            "storage": "local",
            "key": "catalog_state",
            "value": {"page": 2, "filters": ["open"]},
            "encoding": "json",
        }
    )
    with pytest.raises(ActionContractError, match="bounded URL fragment"):
        validate_ui_action(
            {"action": "assert_hash", "value": "https://example.com/#/library"}
        )
    with pytest.raises(ActionContractError, match="same-page URL fragment"):
        validate_ui_action({"action": "set_hash", "value": "https://example.com/#bad"})
    with pytest.raises(ActionContractError, match="32768-byte"):
        validate_ui_action(
            {
                "action": "set_storage_value",
                "storage": "local",
                "key": "oversized",
                "value": "x" * 40_000,
                "encoding": "string",
            }
        )
    with pytest.raises(ActionContractError, match="requires value"):
        validate_ui_action(
            {
                "action": "set_storage_value",
                "storage": "session",
                "key": "missing",
                "encoding": "json",
            }
        )


def test_computed_style_assertion_has_a_closed_property_allowlist():
    validate_ui_action(
        {
            "action": "assert_computed_style",
            "selector": "#drawer",
            "property": "display",
            "value": "none",
            "match": "exact",
        }
    )
    with pytest.raises(ActionContractError, match="computed style property"):
        validate_ui_action(
            {
                "action": "assert_computed_style",
                "selector": "#drawer",
                "property": "background-image",
                "value": "anything",
            }
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
        {"action": "assert_hash", "value": "#/settings", "match": "exact"},
        {
            "action": "assert_computed_style",
            "selector": "#panel",
            "property": "visibility",
            "value": "visible",
            "match": "exact",
        },
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


def test_harness_webcompass_risk_assertion_uses_closed_taxonomy():
    validate_ui_action({
        "action": "assert_webcompass_risk",
        "selector": "[data-testid='dialog']",
        "defect_type": "Occlusion",
    })

    with pytest.raises(ActionContractError, match="official WebCompass taxonomy"):
        validate_ui_action({
            "action": "assert_webcompass_risk",
            "selector": "body",
            "defect_type": "Generic Visual Bug",
        })
