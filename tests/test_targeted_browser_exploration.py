from inspiration_library.deep_browser_exploration import build_browser_first_exploration_plan


def test_target_component_controls_outrank_documentation_chrome():
    common = {"visible": True, "horizontally_reachable": True, "disabled": False}
    observation = {
        "source_kind": "live_url",
        "baseline": {
            "interactive": [
                {**common, "selector": "#support", "tag": "button", "text": "Support",
                 "in_header": True, "in_component": False},
                {**common, "selector": "#account-tab", "tag": "button", "role": "tab",
                 "text": "Account", "in_component": True, "region_text": "Tabs Account Password"},
                {**common, "selector": "#password-tab", "tag": "button", "role": "tab",
                 "text": "Password", "in_component": True, "region_text": "Tabs Account Password"},
                {**common, "selector": "#name", "tag": "input", "type": "text",
                 "name": "Name", "in_component": True, "region_text": "Tabs Account form"},
            ]
        },
        "exploration_paths": [],
    }
    plan = build_browser_first_exploration_plan(
        observation,
        max_paths=2,
        exploration_targets={"target_edit_types": ["Tab Switch"]},
    )
    selectors = {path["actions"][0]["selector"] for path in plan["paths"]}
    assert selectors == {"#account-tab", "#password-tab"}


def test_target_component_controls_outrank_code_toolbar():
    common = {"visible": True, "horizontally_reachable": True, "disabled": False}
    observation = {
        "source_kind": "live_url",
        "baseline": {
            "interactive": [
                {**common, "selector": "#account-tab", "tag": "button", "role": "tab",
                 "text": "Account", "in_component": True},
                {**common, "selector": "#password-tab", "tag": "button", "role": "tab",
                 "text": "Password", "in_component": True},
                {**common, "selector": "#expand-code", "tag": "button", "text": "Expand code",
                 "in_component": True, "in_code_region": True,
                 "region_text": "Tabs Trigger Account Password"},
                {**common, "selector": "#prop-description", "tag": "button",
                 "text": "Prop description", "in_component": True, "in_table": True,
                 "region_text": "Trigger data-state active inactive"},
            ]
        },
        "exploration_paths": [],
    }
    plan = build_browser_first_exploration_plan(
        observation,
        max_paths=2,
        exploration_targets={"target_edit_types": ["Tab Switch"]},
    )
    selectors = {path["actions"][0]["selector"] for path in plan["paths"]}
    assert selectors == {"#account-tab", "#password-tab"}


def test_target_plan_spreads_across_distinct_component_operations():
    common = {"visible": True, "horizontally_reachable": True, "disabled": False,
              "tag": "button", "in_component": True, "region_text": "Data Table records"}
    observation = {
        "source_kind": "live_url",
        "baseline": {"interactive": [
            {**common, "selector": "#select-two", "text": "Toggle selection status of second and third rows"},
            {**common, "selector": "#select-eligible", "text": "Toggle selection status based on selectable"},
            {**common, "selector": "#add-item", "text": "Add Item"},
        ]},
        "exploration_paths": [],
    }
    plan = build_browser_first_exploration_plan(
        observation, max_paths=2,
        exploration_targets={"target_edit_types": ["Data Table"]},
    )
    selectors = {path["actions"][0]["selector"] for path in plan["paths"]}
    assert "#add-item" in selectors
    assert len(selectors & {"#select-two", "#select-eligible"}) == 1
