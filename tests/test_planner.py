from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk.types import ResultMessage

from src.agents.planner import (
    PlannerValidationError,
    _check_edit_transaction,
    _make_planner_stop_hook,
    _validate_planning_bundle,
    recover_trace_proven_planner_checkpoint,
    run_planner,
)
from src.config import HarnessConfig
from src.agents.edit_planner import (
    _apply_harness_visual_evidence_policy,
    _drop_harness_owned_preservation_presence_checks,
    _drop_unowned_storage_assertions,
    _drop_unowned_source_anchors,
    _downgrade_unowned_text_assertions,
    _drop_redundant_option_visibility_assertions,
    _normalize_unspecified_value_assertions,
    _drop_duplicate_pre_effect_assertions,
    _drop_adjacent_duplicate_assertions,
    _drop_incidental_summary_dependency_waits,
    _normalize_bounded_planner_actions,
    _normalize_instruction_context_actions,
    _normalize_hash_filtered_summary_flow,
    _normalize_named_view_flows,
    _normalize_observed_select_options,
    _normalize_scroll_restore_checks,
    _ensure_addressability_assertion,
    _ensure_explicit_reorder_history_fields,
    _stabilize_persistence_checks,
    _preserve_observed_hash_route_for_default_filter,
    _planned_testid_is_grounded,
    _relax_unowned_hash_encodings,
    _rewrite_observed_positional_selectors,
    _rewrite_observed_setup_control_selectors,
    _read_source_ui_contract,
    _validate_atomic_plan_grounding,
)
from src.orchestration.file_comm import FileComm
from src.prompts.edit_planner import ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT
from src.prompts.planner import PLANNER_SYSTEM_PROMPT


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _valid_spec_text() -> str:
    return (
        "# Counter App - Track Every Tap\n\n"
        "## Product Overview\nA simple spec.\n\n"
        "## Target Users\nPeople who count.\n\n"
        "## Feature Descriptions\n"
        "### 1. Counter\n"
        "**Description:** Count.\n"
        "**User Stories:**\n"
        "- As a user, I want to count.\n"
        "**(Priority: High)**\n\n"
        "## Technical Architecture\nClient-only architecture.\n\n"
        "## Visual Design Direction\nMinimal but distinctive.\n"
    )


def test_planner_requires_valid_form_precondition_for_submit_contracts():
    assert "assert_form_valid" in PLANNER_SYSTEM_PROMPT
    assert "including required select and textarea controls" in PLANNER_SYSTEM_PROMPT
    assert "select_option" in PLANNER_SYSTEM_PROMPT
    assert "do not infer a select value from ArrowDown/Enter" in PLANNER_SYSTEM_PROMPT
    assert "exact same-origin browser pathname" in PLANNER_SYSTEM_PROMPT
    assert "Consecutive checks on the same" in PLANNER_SYSTEM_PROMPT


def test_atomic_edit_planner_rejects_invented_test_selectors_and_storage_keys(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    plan = {
        "source_anchors": ["Category Magic"],
        "checks": [
            {
                "id": "invented-contract",
                "route": "/",
                "actions": [
                    {
                        "action": "click",
                        "selector": "[data-testid='category-filter-button']",
                    },
                    {
                        "action": "assert_storage_value",
                        "storage": "local",
                        "key": "settingsPreferences",
                        "value": "saved",
                        "match": "contains",
                    },
                ],
            }
        ]
    }

    with pytest.raises(ValueError, match="ungrounded atomic Edit checks") as exc:
        _validate_atomic_plan_grounding(
            plan=plan,
            user_prompt=(
                "Move Settings to /settings.html and preserve the existing filter "
                "and saved preferences across physical navigation."
            ),
            workdir=tmp_path,
        )
    message = str(exc.value)
    assert "Category Magic" in message
    assert "category-filter-button" in message
    assert "settingsPreferences" in message


def test_inline_edit_source_contract_and_setup_selector_rewrite_use_baseline(
    tmp_path: Path,
):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<button id="notifications-toggle">Notifications</button>'
        '<aside id="notification-center" aria-label="Notifications"></aside>',
        encoding="utf-8",
    )
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(["git", "add", "index.html"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-c", "user.name=Harness", "-c",
            "user.email=harness@localhost", "commit", "-m", "baseline",
        ],
        cwd=frontend, check=True, capture_output=True,
    )
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    (tmp_path / "seed_manifest.json").write_text(
        json.dumps({"baseline_commit": baseline}), encoding="utf-8"
    )

    contract = _read_source_ui_contract(tmp_path)
    assert any(
        item.get("selector") == "#notifications-toggle"
        for item in contract["observed_controls"]
    )
    rewritten = _rewrite_observed_setup_control_selectors(
        {
            "checks": [{
                "actions": [{
                    "action": "click",
                    "selector": "[data-testid='notifications-trigger']",
                }]
            }]
        },
        contract,
    )
    assert rewritten["checks"][0]["actions"][0]["selector"] == (
        "#notifications-toggle"
    )


def test_harness_skips_visual_model_for_behavior_only_atomic_edit(tmp_path: Path):
    plan = {
        "visual_evidence": "conditional",
        "visual_evidence_reason": "Planner was conservative.",
        "checks": [
            {
                "category": "interaction",
                "actions": [
                    {"action": "click", "selector": "#history"},
                    {"action": "assert_visible", "selector": "#history-list"},
                ],
            }
        ],
    }

    normalized = _apply_harness_visual_evidence_policy(
        plan,
        user_prompt="Add a download history list below the driver card.",
        workdir=tmp_path,
    )

    assert normalized["visual_evidence"] == "not_required"
    assert "typed DOM" in normalized["visual_evidence_reason"]


def test_harness_keeps_visual_review_for_style_edit(tmp_path: Path):
    plan = {
        "visual_evidence": "not_required",
        "visual_evidence_reason": "Planner missed it.",
        "checks": [
            {
                "category": "interaction",
                "actions": [{"action": "assert_visible", "selector": "#hero"}],
            }
        ],
    }

    normalized = _apply_harness_visual_evidence_policy(
        plan,
        user_prompt="Change the hero background color to blue.",
        workdir=tmp_path,
    )

    assert normalized["visual_evidence"] == "required"
    assert "independent rendered review" in normalized["visual_evidence_reason"]


def test_harness_does_not_trigger_visual_review_for_preserved_existing_style(tmp_path: Path):
    normalized = _apply_harness_visual_evidence_policy(
        {"visual_evidence": "required", "checks": [{"category": "interaction", "actions": []}]},
        user_prompt="Add a saved card. Keep the existing dark monospace styling intact.",
        workdir=tmp_path,
    )

    assert normalized["visual_evidence"] == "not_required"


def test_atomic_edit_planner_requires_instruction_relevant_ui_dimensions():
    assert "page content" in ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT
    assert "interaction behavior" in ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT
    assert "visual appearance" in ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT
    assert "non-target content/style" in ATOMIC_EDIT_PLANNER_SYSTEM_PROMPT


def test_atomic_planner_does_not_invent_exact_hash_router_encoding():
    normalized = _relax_unowned_hash_encodings(
        {
            "checks": [{
                "actions": [{
                    "action": "assert_hash",
                    "value": "#gallery?type=Timepiece",
                }]
            }]
        },
        grounding="Persist the selected Timepiece type in the URL hash.",
    )

    assert normalized["checks"][0]["actions"][0] == {
        "action": "assert_hash",
        "value": "Timepiece",
        "match": "contains",
    }


def test_atomic_planner_drops_redundant_option_visibility_assertion():
    normalized = _drop_redundant_option_visibility_assertions({
        "checks": [{
            "actions": [
                {"action": "assert_visible", "selector": "#type-filter"},
                {"action": "assert_visible", "selector": "#type-filter option"},
                {
                    "action": "assert_text",
                    "selector": "#type-filter option:nth-child(1)",
                    "value": "All",
                },
            ]
        }]
    })

    assert normalized["checks"][0]["actions"] == [
        {"action": "assert_visible", "selector": "#type-filter"},
        {
            "action": "assert_text",
            "selector": "#type-filter option:nth-child(1)",
            "value": "All",
        },
    ]


def test_atomic_planner_preserves_observed_gallery_hash_for_all_filter():
    normalized = _preserve_observed_hash_route_for_default_filter(
        {
            "checks": [{
                "actions": [
                    {"action": "click", "selector": "a[href='#gallery']"},
                    {"action": "select_option", "selector": "#filter", "value": "All"},
                    {"action": "assert_hash", "value": ""},
                ]
            }]
        },
        user_prompt="Selecting All restores the complete gallery.",
        source_ui_contract={
            "pages": [{
                "navigation": [{"href": "#gallery", "text": "Gallery"}],
                "surfaces": [{"id": "view-gallery", "tag": "section"}],
            }]
        },
    )

    assert normalized["checks"][0]["actions"][-2:] == [
        {"action": "assert_hash", "value": "#gallery", "match": "exact"},
        {"action": "assert_visible", "selector": "#view-gallery"},
    ]

def test_atomic_edit_planner_allows_semantically_grounded_new_target_testid(
    tmp_path: Path,
):
    plan = {
        "source_anchors": ["Download Now"],
        "checks": [
            {
                "id": "new-target",
                "route": "/",
                "actions": [
                    {
                        "action": "wait_for",
                        "selector": "[data-testid='download-history']",
                    },
                    {
                        "action": "assert_visible",
                        "selector": "[data-testid='download-history-item']",
                    },
                ],
            }
        ],
    }

    _validate_atomic_plan_grounding(
        plan=plan,
        user_prompt=(
            "After clicking 'Download Now', add a persistent download history "
            "list containing one entry."
        ),
        workdir=tmp_path,
    )


def test_atomic_edit_planner_allows_new_local_route_and_grounded_title_testids(
    tmp_path: Path,
):
    harness_dir = tmp_path / ".harness"
    harness_dir.mkdir()
    (harness_dir / "edit_task_contract.json").write_text(
        json.dumps({
            "schema_version": "edit-task-contract-v1",
            "requested_target_routes": ["/page_gallery_intake.html"],
        }),
        encoding="utf-8",
    )
    plan = {
        "source_anchors": ["Gallery Intake", "Ideas"],
        "checks": [{
            "id": "new-local-page",
            "route": "/index.html",
            "actions": [
                {
                    "action": "click",
                    "selector": "a[href='page_gallery_intake.html']",
                },
                {
                    "action": "assert_text",
                    "selector": (
                        "[data-testid='gallery-intake-column-ideas-title']"
                    ),
                    "value": "Ideas",
                },
            ],
        }],
    }

    _validate_atomic_plan_grounding(
        plan=plan,
        user_prompt=(
            "Add a Gallery Intake page with a workflow column named Ideas."
        ),
        workdir=tmp_path,
    )


def test_atomic_edit_planner_rewrites_observed_positional_link_to_unique_href():
    payload = {
        "checks": [{
            "actions": [
                {"action": "click", "selector": "a:nth-of-type(3)"},
                {"action": "assert_visible", "selector": "#target"},
            ]
        }]
    }
    contract = {
        "pages": [{
            "navigation": [{"href": "#gallery", "text": "Exhibit Gallery"}]
        }],
        "observed_controls": [{
            "selector": "a:nth-of-type(3)",
            "tag": "a",
            "text": "EXHIBIT GALLERY",
        }],
    }

    rewritten = _rewrite_observed_positional_selectors(payload, contract)

    assert rewritten["checks"][0]["actions"][0]["selector"] == "a[href='#gallery']"
    assert payload["checks"][0]["actions"][0]["selector"] == "a:nth-of-type(3)"


def test_atomic_edit_planner_rewrites_positional_card_to_grounded_collection():
    payload = {
        "goal": "Open an artifact card detail view.",
        "checks": [{
            "actions": [
                {"action": "click", "selector": "#gallery-grid > div:first-child"},
                {"action": "assert_visible", "selector": "#detail"},
            ]
        }],
    }
    contract = {
        "observed_collections": [
            {"selector": ".nav-link", "source": "source-selector"},
            {"selector": ".artifact-card", "source": "source-selector"},
        ]
    }

    rewritten = _rewrite_observed_positional_selectors(payload, contract)

    assert rewritten["checks"][0]["actions"][0]["selector"] == ".artifact-card"


def test_atomic_edit_planner_drops_only_unowned_internal_storage_keys():
    payload = {
        "checks": [{
            "actions": [
                {
                    "action": "assert_storage_value",
                    "name": "downloadHistory",
                    "value": None,
                },
                {
                    "action": "assert_storage_value",
                    "key": "explicit_key",
                    "value": "saved",
                },
                {
                    "action": "assert_storage_value",
                    "value": "missing key must not survive",
                },
                {"action": "assert_visible", "selector": "#history"},
            ]
        }]
    }

    normalized = _drop_unowned_storage_assertions(
        payload, grounding="Persist using the explicit_key local storage key."
    )

    assert [action.get("key") or action.get("name") for action in normalized["checks"][0]["actions"]] == [
        "explicit_key",
        None,
    ]


def test_atomic_edit_planner_drops_new_target_from_source_anchors():
    normalized = _drop_unowned_source_anchors(
        {
            "source_anchors": [
                "#downloadBtn",
                "data-testid='download-history'",
            ]
        },
        grounding="Observed control: #downloadBtn",
    )

    assert normalized["source_anchors"] == ["#downloadBtn"]


def test_atomic_edit_planner_makes_empty_runtime_placeholders_nonempty_assertions():
    normalized = _normalize_unspecified_value_assertions({
        "checks": [{"actions": [
            {"action": "assert_text", "selector": "#title", "value": "", "match": "contains"},
            {"action": "assert_attribute", "selector": "#detail", "name": "data-id", "value": ""},
            {"action": "assert_attribute", "selector": "#latest", "name": "data-value", "match": "nonempty"},
        ]}],
    })

    assert normalized["checks"][0]["actions"] == [
        {"action": "assert_text", "selector": "#title", "value": "", "match": "nonempty"},
        {"action": "assert_attribute", "selector": "#detail", "name": "data-id", "value": ""},
        {"action": "assert_attribute", "selector": "#latest", "name": "data-value", "value": "", "match": "nonempty"},
    ]


def test_atomic_edit_planner_repairs_swapped_attribute_match_fields_from_peer():
    normalized = _normalize_bounded_planner_actions({
        "checks": [
            {"actions": [{
                "action": "assert_attribute",
                "selector": "[data-testid='summary-last-timestamp']",
                "name": "data-value",
                "value": "",
            }]},
            {"actions": [{
                "action": "assert_attribute",
                "selector": "[data-testid='summary-last-timestamp']",
                "name": "match",
                "value": "nonempty",
            }]},
        ],
    })

    assert normalized["checks"][1]["actions"] == [{
        "action": "assert_attribute",
        "selector": "[data-testid='summary-last-timestamp']",
        "name": "data-value",
        "value": "",
        "match": "nonempty",
    }]


def test_atomic_edit_planner_marks_empty_to_nonempty_attribute_transition_exact():
    normalized = _normalize_unspecified_value_assertions({
        "checks": [
            {"actions": [{
                "action": "assert_attribute",
                "selector": "#latest",
                "name": "data-value",
                "value": "",
            }]},
            {"actions": [{
                "action": "assert_attribute",
                "selector": "#latest",
                "name": "match",
                "value": "nonempty",
            }]},
        ],
    })

    assert normalized["checks"][0]["actions"][0]["match"] == "exact"


def test_hash_filtered_summary_plan_proves_global_to_contextual_transition():
    normalized = _normalize_hash_filtered_summary_flow(
        {"checks": [{"actions": [
            {"action": "set_hash", "value": "#audio-studio-driver"},
            {
                "action": "click",
                "selector": 'a.sidebar-item[data-name="Audio Studio Driver"]',
            },
            {"action": "assert_visible", "selector": "[data-testid='download-summary-panel']"},
            {
                "action": "assert_text",
                "selector": "[data-testid='total-downloads']",
                "value": "",
                "match": "nonempty",
            },
            {
                "action": "assert_text",
                "selector": "[data-testid='total-volume']",
                "value": "",
                "match": "nonempty",
            },
        ]}]},
        user_prompt="Filter summary statistics by URL hash for a specific driver.",
        source_ui_contract={
            "observed_controls": [{"selector": "#downloadBtn"}],
            "pages": [{"outputs": [
                {"selector": "[data-testid='total-downloads']"},
                {"selector": "[data-testid='total-volume']"},
            ]}],
        },
    )

    assert normalized["checks"][0]["actions"] == [
        {"action": "click", "selector": "#downloadBtn", "settle_ms": 2_000},
        {
            "action": "assert_text",
            "selector": "[data-testid='total-downloads']",
            "value": "1",
            "match": "exact",
        },
        {"action": "set_hash", "value": "#audio-studio-driver"},
        {
            "action": "click",
            "selector": 'a.sidebar-item[data-name="Audio Studio Driver"]',
        },
        {"action": "assert_visible", "selector": "[data-testid='download-summary-panel']"},
        {
            "action": "assert_text",
            "selector": "[data-testid='total-downloads']",
            "value": "0",
            "match": "exact",
        },
        {
            "action": "assert_text",
            "selector": "[data-testid='total-volume']",
            "value": "0",
            "match": "contains",
        },
    ]


def test_atomic_edit_planner_drops_only_adjacent_duplicate_assertions():
    repeated = {"action": "assert_visible", "selector": ".summary"}
    normalized = _drop_adjacent_duplicate_assertions({
        "checks": [{"actions": [
            repeated,
            repeated,
            {"action": "click", "selector": "#refresh"},
            repeated,
        ]}],
    })

    assert normalized["checks"][0]["actions"] == [
        repeated,
        {"action": "click", "selector": "#refresh"},
        repeated,
    ]


def test_atomic_edit_planner_drops_duplicate_effect_assertion_before_drag():
    entry = {
        "action": "assert_visible",
        "selector": "[data-testid='layout-history-list'] [data-testid='history-entry']",
    }
    normalized = _drop_duplicate_pre_effect_assertions({
        "checks": [{"actions": [
            {"action": "click", "selector": "a[href='#gallery']"},
            {"action": "wait_for", "selector": "[data-testid='layout-history-list']"},
            entry,
            {
                "action": "drag_and_drop",
                "source_selector": ".artifact-card:nth-child(1)",
                "target_selector": ".artifact-card:nth-child(3)",
            },
            {"action": "assert_visible", "selector": "[data-testid='layout-history-list']"},
            entry,
        ]}],
    })

    assert normalized["checks"][0]["actions"] == [
        {"action": "click", "selector": "a[href='#gallery']"},
        {
            "action": "drag_and_drop",
            "source_selector": ".artifact-card:nth-child(1)",
            "target_selector": ".artifact-card:nth-child(3)",
        },
        {"action": "assert_visible", "selector": "[data-testid='layout-history-list']"},
        entry,
    ]


def test_atomic_edit_planner_drops_history_output_wait_before_drag():
    normalized = _drop_duplicate_pre_effect_assertions({
        "checks": [{"actions": [
            {"action": "click", "selector": "a[href='#gallery']"},
            {"action": "wait_for", "selector": "#gallery-grid"},
            {"action": "wait_for", "selector": "[data-testid='reorder-history-list']"},
            {
                "action": "drag_and_drop",
                "source_selector": ".artifact-card:first-child",
                "target_selector": ".artifact-card:last-child",
            },
            {"action": "assert_visible", "selector": "[data-testid='reorder-history-item']"},
        ]}],
    })

    assert normalized["checks"][0]["actions"] == [
        {"action": "click", "selector": "a[href='#gallery']"},
        {"action": "wait_for", "selector": "#gallery-grid"},
        {
            "action": "drag_and_drop",
            "source_selector": ".artifact-card:first-child",
            "target_selector": ".artifact-card:last-child",
        },
        {"action": "assert_visible", "selector": "[data-testid='reorder-history-item']"},
    ]


def test_atomic_edit_planner_drops_history_output_wait_before_reset_click():
    normalized = _drop_duplicate_pre_effect_assertions({
        "checks": [{"actions": [
            {"action": "click", "selector": "a[href='#gallery']"},
            {"action": "wait_for", "selector": "[data-testid='reorder-history-list']"},
            {"action": "click", "selector": "#reset-layout-btn"},
            {"action": "assert_visible", "selector": "[data-testid='reorder-history-item']"},
            {
                "action": "assert_text",
                "selector": (
                    "[data-testid='reorder-history-item']:first-child "
                    "[data-testid='history-event-type']"
                ),
                "value": "reset",
            },
        ]}],
    })

    assert normalized["checks"][0]["actions"] == [
        {"action": "click", "selector": "a[href='#gallery']"},
        {"action": "click", "selector": "#reset-layout-btn"},
        {"action": "assert_visible", "selector": "[data-testid='reorder-history-item']"},
        {
            "action": "assert_text",
            "selector": (
                "[data-testid='reorder-history-item']:first-child "
                "[data-testid='history-event-type']"
            ),
            "value": "reset",
        },
    ]


def test_atomic_edit_planner_keeps_explicit_reorder_position_fields_observable():
    normalized = _ensure_explicit_reorder_history_fields(
        {"checks": [{"actions": [
            {
                "action": "drag_and_drop",
                "source_selector": ".artifact-card:nth-child(1)",
                "target_selector": ".artifact-card:nth-child(2)",
            },
            {
                "action": "assert_visible",
                "selector": "[data-testid='reorder-history-item']",
            },
            {
                "action": "assert_attribute",
                "selector": (
                    "[data-testid='reorder-history-item']:first-child "
                    "[data-testid='history-timestamp']"
                ),
                "name": "data-value",
                "value": "",
                "match": "nonempty",
            },
        ]}]},
        user_prompt=(
            "Record the moved artifact identifier, its previous and new positions, "
            "and a timestamp in durable history."
        ),
    )

    actions = normalized["checks"][0]["actions"]
    assert actions[-2:] == [
        {
            "action": "assert_attribute",
            "selector": (
                "[data-testid='reorder-history-item']:first-child "
                "[data-testid='previous-position']"
            ),
            "name": "data-value",
            "value": "",
            "match": "nonempty",
        },
        {
            "action": "assert_attribute",
            "selector": (
                "[data-testid='reorder-history-item']:first-child "
                "[data-testid='new-position']"
            ),
            "name": "data-value",
            "value": "",
            "match": "nonempty",
        },
    ]


def test_atomic_edit_planner_canonicalizes_explicit_reorder_field_aliases():
    normalized = _ensure_explicit_reorder_history_fields(
        {"checks": [{"actions": [
            {
                "action": "drag_and_drop",
                "source_selector": ".artifact-card:nth-child(1)",
                "target_selector": ".artifact-card:nth-child(2)",
            },
            {
                "action": "assert_visible",
                "selector": "[data-testid='reorder-history-item']",
            },
            {
                "action": "assert_attribute",
                "selector": "[data-testid='history-prev-pos']",
                "name": "data-value",
                "value": "",
            },
            {
                "action": "assert_attribute",
                "selector": "[data-testid=\"history-new-pos\"]",
                "name": "data-value",
                "value": "",
            },
        ]}]},
        user_prompt="Capture the previous and new positions for each reorder.",
    )

    selectors = [item.get("selector") for item in normalized["checks"][0]["actions"]]
    assert "[data-testid='previous-position']" in selectors
    assert '[data-testid="new-position"]' in selectors
    assert len(normalized["checks"][0]["actions"]) == 4


def test_atomic_edit_planner_grounds_bounded_summary_field_synonyms():
    instruction = (
        "Add a summary panel showing the total number of events, grouped by "
        "reorder vs reset, and the most recent event timestamp."
    )

    assert _planned_testid_is_grounded("summary-reorder-count", instruction)
    assert _planned_testid_is_grounded("summary-reset-count", instruction)
    assert _planned_testid_is_grounded("summary-last-timestamp", instruction)


def test_atomic_edit_planner_grounds_requested_form_labels_and_validation_targets():
    instruction = (
        "Add a Gallery Intake modal with labeled fields for event name and date. "
        "When either required field is missing, show a clear inline message."
    )

    assert _planned_testid_is_grounded("gallery-intake-event-name-label", instruction)
    assert _planned_testid_is_grounded("gallery-intake-date-error", instruction)
    assert _planned_testid_is_grounded("gallery-intake-validation-message", instruction)
    assert not _planned_testid_is_grounded("gallery-intake-owner-email-error", instruction)


def test_atomic_edit_planner_drops_empty_history_wait_before_summary_assertions():
    normalized = _drop_incidental_summary_dependency_waits(
        {"checks": [{"actions": [
            {"action": "click", "selector": "a[href='#gallery']"},
            {"action": "wait_for", "selector": "#reorder-history-list"},
            {"action": "assert_visible", "selector": "[data-testid='layout-history-summary']"},
            {"action": "assert_text", "selector": "[data-testid='summary-total-events']", "value": "0"},
        ]}]},
        user_prompt="Add a summary panel next to the layout history.",
    )

    assert normalized["checks"][0]["actions"] == [
        {"action": "click", "selector": "a[href='#gallery']"},
        {"action": "assert_visible", "selector": "[data-testid='layout-history-summary']"},
        {"action": "assert_text", "selector": "[data-testid='summary-total-events']", "value": "0"},
    ]


def test_atomic_edit_planner_makes_named_view_consumers_self_contained():
    normalized = _normalize_named_view_flows(
        {"checks": [
            {"id": "save-view", "actions": [
                {"action": "click", "selector": "a[href='#gallery']"},
                {"action": "select_option", "selector": "[data-testid='type-filter-dropdown']", "value": "Timepiece"},
                {"action": "fill", "selector": "[data-testid='save-view-name-input']", "value": "Victorian Textiles"},
                {"action": "click", "selector": "[data-testid='save-view-button']"},
            ]},
            {"id": "restore-view", "actions": [
                {"action": "click", "selector": "a[href='#gallery']"},
                {"action": "select_option", "selector": "[data-testid='type-filter-dropdown']", "value": "All"},
                {"action": "click", "selector": "[data-testid='saved-view-item-Victorian-Textiles']"},
                {"action": "assert_value", "selector": "[data-testid='type-filter-dropdown']", "value": "Document"},
            ]},
            {"id": "saved-views-do-not-alter-layout-order", "actions": [
                {"action": "click", "selector": "a[href='#gallery']"},
                {"action": "drag_and_drop", "source_selector": ".artifact-card:nth-child(1)", "target_selector": ".artifact-card:nth-child(3)"},
                {"action": "assert_attribute", "selector": ".artifact-card:nth-child(1)", "name": "data-id", "value": "nonempty"},
                {"action": "reload"},
                {"action": "click", "selector": "a[href='#gallery']"},
                {"action": "click", "selector": "[data-testid='saved-view-item-Test-View']"},
                {"action": "assert_visible", "selector": ".artifact-card"},
            ]},
        ]},
        user_prompt=(
            "Allow users to save the current type filter as a named view "
            "(e.g., 'Victorian Textiles') and restore it from saved views."
        ),
    )

    restore = normalized["checks"][1]["actions"]
    assert restore[1:4] == [
        {"action": "select_option", "selector": "[data-testid='type-filter-dropdown']", "value": "Document"},
        {"action": "fill", "selector": "[data-testid='save-view-name-input']", "value": "Victorian Textiles"},
        {"action": "click", "selector": "[data-testid='save-view-button']"},
    ]
    assert restore[5]["selector"] == (
        "[data-testid='saved-view-item'][data-name=\"Victorian Textiles\"]"
    )
    preserve = normalized["checks"][2]["actions"]
    assert preserve[1]["value"] == "All"
    assert any(item.get("action") == "capture_attribute" for item in preserve)
    assert preserve[-1] == {
        "action": "assert_attribute",
        "selector": ".artifact-card:nth-child(1)",
        "name": "data-id",
        "snapshot": "saved-view-layout-first-id",
    }


def test_named_view_order_check_uses_same_filter_and_attribute_snapshot():
    payload = _normalize_bounded_planner_actions({"checks": [
        {"id": "save-view", "actions": [
            {"action": "select_option", "selector": "#type-filter", "value": "Timepiece"},
            {"action": "fill", "selector": "#save-view-name", "value": "Victorian Textiles"},
            {"action": "click", "selector": "#save-view-button"},
        ]},
        {"id": "restore-view-preserves-layout-order", "actions": [
            {"action": "click", "selector": "a[href='#gallery']"},
            {"action": "reload", "action_detail": {}},
            {
                "action": "assert_property",
                "selector": "#gallery-grid",
                "name": "innerHTML",
                "value": "nonempty",
                "match": "nonempty",
            },
            {"action": "click", "selector": "[data-testid='saved-view-item-Test-View']"},
            {"action": "assert_value", "selector": "#type-filter", "value": "Photograph"},
            {"action": "assert_no_console_errors", "action_detail": {}},
        ]},
    ]})

    normalized = _normalize_named_view_flows(
        payload,
        user_prompt="Save the current filter as a named view and preserve layout order.",
    )
    actions = normalized["checks"][1]["actions"]

    assert any(
        item.get("action") == "capture_attribute"
        and item.get("selector") == ".artifact-card:nth-child(1)"
        for item in actions
    )
    assert actions[-1] == {
        "action": "assert_attribute",
        "selector": ".artifact-card:nth-child(1)",
        "name": "data-id",
        "snapshot": "saved-view-layout-first-id",
    }
    assert next(
        item["value"] for item in actions
        if item.get("action") == "assert_value"
    ) == "All"
    assert all("action_detail" not in item for item in actions)


def test_named_view_persistence_replaces_guessed_storage_oracle_with_public_ui():
    normalized = _normalize_named_view_flows(
        {"checks": [{
            "id": "save-named-view-persistence",
            "actions": [
                {"action": "click", "selector": "a[href='#gallery']"},
                {"action": "select_option", "selector": "#type-filter", "value": "Timepiece"},
                {"action": "fill", "selector": "#save-view-name", "value": "Victorian Textiles"},
                {"action": "click", "selector": "#save-view-button"},
                {
                    "action": "assert_storage_value",
                    "storage": "local",
                    "key": "guessed-key",
                    "value": "Victorian Textiles",
                },
            ],
        }]},
        user_prompt="Save the current filter as a named view that persists across sessions.",
    )
    actions = normalized["checks"][0]["actions"]

    assert all(item.get("action") != "assert_storage_value" for item in actions)
    assert {"action": "reload"} in actions
    assert actions[-2:] == [
        {"action": "assert_visible", "selector": "[data-testid='saved-views-list']"},
        {
            "action": "assert_text",
            "selector": "[data-testid='saved-views-list']",
            "value": "Victorian Textiles",
            "match": "contains",
        },
    ]


def test_atomic_edit_planner_waits_for_exact_state_before_reload():
    normalized = _stabilize_persistence_checks({
        "checks": [
            {"actions": [{"action": "assert_text", "selector": "#count", "value": "1"}]},
            {"actions": [
                {"action": "click", "selector": "#download"},
                {"action": "wait_for", "selector": ".history-item"},
                {"action": "reload"},
                {"action": "assert_visible", "selector": "#summary"},
                {"action": "assert_text", "selector": "#count", "value": "2"},
            ]},
        ],
    })

    second = normalized["checks"][1]["actions"]
    assert second[2:5] == [
        {"action": "assert_text", "selector": "#count", "value": "2"},
        {"action": "reload"},
        {"action": "assert_visible", "selector": "#summary"},
    ]


def test_atomic_edit_planner_normalizes_scroll_and_unknown_hash_state():
    normalized = _normalize_bounded_planner_actions({
        "checks": [{"actions": [
            {"action": "scroll", "selector": "#grid", "x": 0, "y": 200},
            {"action": "assert_property", "selector": "#grid", "name": "scrollTop", "value": 200},
            {"action": "assert_hash", "selector": "body", "value": "", "match": "contains"},
        ]}],
    })

    assert normalized["checks"][0]["actions"] == [
        {"action": "set_viewport", "width": 1280, "height": 400},
        {"action": "scroll", "y": 200},
        {"action": "assert_scroll", "y": 200, "tolerance": 2},
        {"action": "assert_hash", "value": "", "match": "nonempty"},
    ]


def test_atomic_edit_planner_maps_empty_url_placeholder_to_nonempty_hash():
    normalized = _normalize_bounded_planner_actions({
        "checks": [{"actions": [{
            "action": "assert_url",
            "value": "",
            "match": "contains",
        }]}],
    })

    assert normalized["checks"][0]["actions"] == [{
        "action": "assert_hash",
        "value": "",
        "match": "nonempty",
    }]


def test_atomic_edit_planner_normalizes_key_press_value_alias():
    normalized = _normalize_bounded_planner_actions({
        "checks": [{"actions": [
            {"action": "key_press", "value": "Escape"},
            {"action": "assert_hidden", "selector": "#menu"},
        ]}],
    })

    assert normalized["checks"][0]["actions"][0] == {
        "action": "key_press",
        "key": "Escape",
    }


def test_atomic_edit_planner_uses_live_form_value_not_html_attribute():
    normalized = _normalize_bounded_planner_actions({
        "checks": [{"actions": [{
            "action": "assert_attribute",
            "selector": "#filter",
            "name": "value",
            "value": "Timepiece",
        }]}],
    })

    assert normalized["checks"][0]["actions"] == [
        {"action": "assert_value", "selector": "#filter", "value": "Timepiece"}
    ]


def test_atomic_edit_planner_normalizes_viewport_property_alias():
    normalized = _normalize_bounded_planner_actions({
        "checks": [{"actions": [{
            "action": "assert_property",
            "selector": "#driver",
            "name": "isInView",
            "value": True,
        }]}],
    })

    assert normalized["checks"][0]["actions"] == [{
        "action": "assert_in_view",
        "selector": "#driver",
    }]


def test_atomic_edit_planner_strips_empty_action_detail_and_inner_html_alias():
    normalized = _normalize_bounded_planner_actions({
        "checks": [{"actions": [
            {"action": "reload", "action_detail": {}},
            {
                "action": "assert_property",
                "selector": "#gallery-grid",
                "name": "innerHTML",
                "value": "nonempty",
                "match": "nonempty",
            },
            {"action": "assert_no_console_errors", "action_detail": {}},
        ]}],
    })

    assert normalized["checks"][0]["actions"] == [
        {"action": "reload"},
        {"action": "assert_visible", "selector": "#gallery-grid"},
        {"action": "assert_no_console_errors"},
    ]


def test_atomic_edit_planner_checks_class_membership_with_state_selector():
    normalized = _normalize_bounded_planner_actions({
        "checks": [{"actions": [{
            "action": "assert_attribute",
            "selector": "#item",
            "name": "class",
            "value": "highlighted",
        }]}],
    })

    assert normalized["checks"][0]["actions"] == [{
        "action": "assert_visible",
        "selector": "#item[class~='highlighted']",
    }]


def test_atomic_edit_planner_materializes_mobile_first_link_context():
    normalized = _normalize_instruction_context_actions(
        {"checks": [{"actions": [
            {"action": "click", "selector": ".nav-toggle"},
            {"action": "assert_focus", "selector": ".main-nav a[href='#']"},
        ]}]},
        user_prompt="On mobile move focus to the first link.",
    )

    assert normalized["checks"][0]["actions"] == [
        {"action": "set_viewport", "width": 390, "height": 844},
        {"action": "click", "selector": ".nav-toggle"},
        {"action": "assert_focus", "selector": ".main-nav li:first-child a"},
    ]


def test_atomic_edit_planner_compares_restored_scroll_to_click_time_snapshot():
    normalized = _normalize_scroll_restore_checks(
        {"checks": [{"id": "return", "actions": [
            {"action": "scroll", "y": 500},
            {"action": "click", "selector": ".artifact-card"},
            {"action": "click", "selector": "#back"},
            {"action": "assert_scroll", "y": 500, "tolerance": 3},
        ]}]},
        user_prompt="Return to the exact gallery state, including scroll position.",
    )

    actions = normalized["checks"][0]["actions"]
    assert actions[1]["capture_scroll_as"] == "scroll-before-detail-1"
    assert actions[-1] == {
        "action": "assert_scroll",
        "snapshot": "scroll-before-detail-1",
        "tolerance": 3,
    }


def test_atomic_edit_planner_leaves_literal_scroll_check_for_unrelated_edits():
    payload = {"checks": [{"actions": [
        {"action": "scroll", "y": 500},
        {"action": "click", "selector": "#load-more"},
        {"action": "assert_scroll", "y": 500},
    ]}]}

    assert _normalize_scroll_restore_checks(
        payload, user_prompt="Load more cards."
    ) == payload


def test_atomic_edit_planner_requires_url_evidence_before_direct_link_reload():
    normalized = _ensure_addressability_assertion(
        {"checks": [{"actions": [
            {"action": "click", "selector": ".artifact-card"},
            {"action": "reload"},
            {"action": "assert_visible", "selector": "#detail"},
        ]}]},
        user_prompt="The detail view must be addressable via URL for a direct link.",
    )

    assert normalized["checks"][0]["actions"][1:3] == [
        {"action": "assert_hash", "value": "", "match": "nonempty"},
        {"action": "reload"},
    ]


def test_atomic_edit_planner_replaces_invented_select_fixture_with_observed_value():
    normalized = _normalize_observed_select_options(
        {"checks": [{"actions": [
            {"action": "select_option", "selector": "#filter", "value": "Blueprint"},
            {"action": "assert_value", "selector": "#filter", "value": "Painting"},
        ]}]},
        source_ui_contract={"pages": [{"controls": [{
            "tag": "select",
            "selector": "[data-testid='type-filter']",
            "selector_aliases": ["#filter"],
            "options": [{"value": "All"}, {"value": "Timepiece"}],
        }]}]},
        user_prompt="Preserve the active type filter.",
    )

    assert [
        action["value"] for action in normalized["checks"][0]["actions"]
    ] == ["Timepiece", "Timepiece"]


def test_atomic_edit_planner_does_not_assert_unrequested_observed_label():
    normalized = _downgrade_unowned_text_assertions(
        {
            "checks": [
                {
                    "actions": [
                        {
                            "action": "assert_text",
                            "selector": "[data-testid='download-history']",
                            "value": "Unrelated Sidebar Driver",
                        }
                    ]
                }
            ]
        },
        grounding="Add a persistent download history.",
    )

    assert normalized["checks"][0]["actions"] == [
        {
            "action": "assert_visible",
            "selector": "[data-testid='download-history']",
        }
    ]


def test_atomic_edit_planner_downgrades_unowned_positional_text_to_collection_presence():
    normalized = _downgrade_unowned_text_assertions(
        {
            "checks": [
                {
                    "actions": [
                        {
                            "action": "assert_text",
                            "selector": "[data-testid='download-history-item']:nth-of-type(1)",
                            "value": "Unrequested driver name",
                        }
                    ]
                }
            ]
        },
        grounding="Add a persistent download history.",
    )

    assert normalized["checks"][0]["actions"] == [
        {
            "action": "assert_visible",
            "selector": "[data-testid='download-history-item']",
        }
    ]


def test_atomic_edit_planner_accepts_explicit_routes_resources_and_named_keys(
    tmp_path: Path,
):
    plan = {
        "checks": [
            {
                "id": "grounded-contract",
                "route": "/",
                "actions": [
                    {"action": "click", "selector": "a[href='/settings.html']"},
                    {"action": "assert_url", "value": "/settings.html"},
                    {
                        "action": "assert_attribute",
                        "selector": "script[src='app.js']",
                        "name": "src",
                        "value": "app.js",
                    },
                    {
                        "action": "assert_storage_value",
                        "storage": "local",
                        "key": "eit_prefs",
                        "value": "saved",
                        "match": "contains",
                    },
                ],
            }
        ]
    }

    _validate_atomic_plan_grounding(
        plan=plan,
        user_prompt=(
            "Use the exact physical route /settings.html, keep app.js, and preserve "
            "the existing eit_prefs storage record."
        ),
        workdir=tmp_path,
    )


def _write_valid_planning_bundle(file_comm: FileComm) -> None:
    file_comm.write_spec(_valid_spec_text())
    file_comm.write_design_tokens(
        {
            "theme_name": "editorial counter",
            "color": {"bg": "#111111"},
            "typography": {"display": "Space Grotesk"},
            "spacing": {"base": 8},
            "radius": {"card": 16},
            "motion": {"duration_fast": 160},
            "style_rules": ["bold hierarchy"],
            "anti_patterns": ["generic cards"],
            "visual_experiment": {
                "design_hypothesis": "Use poster-like asymmetry.",
                "reason_for_image_first": "Text-only outputs stay too templated.",
                "desired_break_from_web_templates": ["poster-like asymmetry"],
                "visual_opportunities_beyond_css": ["ink texture"],
                "forbidden_generic_patterns": ["centered card grid"],
            },
        }
    )
    file_comm.write_feature_list(
        {
            "features": [
                {
                    "id": "F001",
                    "name": "Counter",
                    "priority": "high",
                    "depends_on": [],
                    "description": "Count values.",
                    "acceptance_criteria": ["Counter increments correctly."],
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
                    "title": "Core counter",
                    "goal": "Ship the primary counter flow.",
                    "feature_ids": ["F001"],
                    "deliverables": ["Visible counter UI."],
                    "exit_criteria": ["Counter increments correctly."],
                }
            ],
        }
    )
    file_comm.write_ui_verification_plan(
        {
            "sprints": [
                {
                    "sprint": 1,
                    "checks": [
                        {
                            "id": "UI-001",
                            "feature_id": "F001",
                            "task": "Click increment once.",
                            "expected_result": "Counter changes by one step.",
                            "critical": True,
                            "category": "core_interaction",
                        }
                    ],
                }
            ]
        }
    )
    file_comm.write_progress("# Progress Log\n\n## planning\n- status: complete")


def test_explicit_edit_is_one_multi_page_transaction(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "edit_task_contract.json").write_text(
        '{"schema_version":"edit-task-contract-v1"}'
    )
    sprint = {
        "number": 1,
        "requirement_changes": [{"requirement_id": "REQ-1", "relation": "add"}],
        "impact_tags": ["shared-store"],
        "visual_evidence_reason": "State-only change uses DOM/property evidence.",
    }
    verification = {"sprints": [{"sprint": 1, "checks": [
        {"id": "UI-a", "requirement_id": "REQ-1", "impact_tags": ["shared-store"]},
        {"id": "UI-b", "requirement_id": "REQ-1", "impact_tags": ["shared-store"]},
    ]}]}

    _check_edit_transaction(
        file_comm,
        {"total_sprints": 1, "sprints": [sprint]},
        verification,
    )

    with pytest.raises(PlannerValidationError, match="exactly one Sprint"):
        _check_edit_transaction(
            file_comm,
            {"total_sprints": 2, "sprints": [sprint, {**sprint, "number": 2}]},
            verification,
        )


def test_atomic_edit_planner_allows_composed_grounded_target_testid(tmp_path: Path):
    _validate_atomic_plan_grounding(
        plan={
            "source_anchors": [],
            "checks": [{
                "id": "summary",
                "actions": [{
                    "action": "assert_visible",
                    "selector": "[data-testid='summary-total-downloads']",
                }],
            }],
        },
        user_prompt=(
            "Add a summary panel that displays the total number of downloads."
        ),
        workdir=tmp_path,
    )


def test_atomic_edit_planner_rejects_invented_composite_css_class(tmp_path: Path):
    with pytest.raises(ValueError, match="selector identity 'gallery-item'"):
        _validate_atomic_plan_grounding(
            plan={
                "source_anchors": [],
                "checks": [{
                    "id": "gallery",
                    "actions": [{
                        "action": "assert_visible",
                        "selector": ".gallery-item",
                    }],
                }],
            },
            user_prompt="Filter the gallery so that only matching artifacts appear.",
            workdir=tmp_path,
        )


def _write_planner_trace(file_comm: FileComm, artifact_names: list[str]) -> None:
    trace_dir = file_comm.dir / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    events: list[dict] = []
    for index, name in enumerate(artifact_names):
        events.extend(
            [
                {
                    "event": "assistant",
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "write_file",
                                    "arguments": json.dumps(
                                        {"path": f".harness/{name}", "content": "model output"}
                                    ),
                                },
                                "id": f"call-{index}",
                            }
                        ]
                    },
                },
                {"event": "tool", "name": "write_file", "ok": True},
            ]
        )
    events.append(
        {
            "event": "run_error",
            "cumulative_usage": {"input_tokens": 100, "output_tokens": 20},
            "estimated_cost_usd": 0.001,
        }
    )
    (trace_dir / "planner.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


def test_recover_trace_proven_planner_checkpoint_without_second_model_call(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    _write_planner_trace(
        file_comm,
        [
            "spec.md",
            "design_tokens.json",
            "feature_list.json",
            "sprint_plan.json",
            "ui_verification_plan.json",
        ],
    )

    stats = recover_trace_proven_planner_checkpoint(file_comm, HarnessConfig())

    assert stats is not None
    assert stats.cost_usd == 0.001
    assert stats.token_usage == {"input_tokens": 100, "output_tokens": 20}
    assert stats.usage["recovery"] == "trace_proven_planner_checkpoint"
    assert file_comm.read_accepted_sprints() == {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 0,
    }


def test_planner_checkpoint_recovery_requires_every_semantic_artifact_in_trace(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    _write_planner_trace(
        file_comm,
        ["spec.md", "design_tokens.json", "feature_list.json", "sprint_plan.json"],
    )

    assert recover_trace_proven_planner_checkpoint(file_comm, HarnessConfig()) is None


def test_atomic_edit_planner_checkpoint_recovers_from_single_semantic_artifact(
    tmp_path: Path,
):
    from src.orchestration.atomic_edit_plan import write_atomic_edit_plan

    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "edit_task_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "edit-task-contract-v1",
                "task_mode": "edit",
                "requested_target_routes": ["/"],
            }
        ),
        encoding="utf-8",
    )
    write_atomic_edit_plan(
        file_comm.dir,
        {
            "schema_version": "atomic-edit-plan-v1",
            "title": "Add a status toggle",
            "goal": "Toggle the visible status panel.",
            "deliverables": ["A working status toggle."],
            "exit_criteria": ["Clicking the toggle reveals the status panel."],
            "requirement_changes": [
                {
                    "requirement_id": "REQ-STATUS",
                    "relation": "add",
                    "prior_requirement_ids": [],
                    "rationale": "New requested behavior.",
                }
            ],
            "impact_tags": ["route:/", "status-toggle"],
            "unresolved_conflicts": [],
            "visual_evidence": "not_required",
            "visual_evidence_reason": "DOM visibility and click behavior are sufficient.",
            "checks": [
                {
                    "id": "UI-STATUS",
                    "task": "Click the status toggle.",
                    "expected_result": "The status panel is visible.",
                    "critical": True,
                    "category": "interaction",
                    "requirement_id": "REQ-STATUS",
                    "impact_tags": ["route:/", "status-toggle"],
                    "route": "/",
                    "fixtures": [],
                    "actions": [
                        {"action": "click", "selector": "#status-toggle"},
                        {"action": "assert_visible", "selector": "#status-panel"},
                    ],
                }
            ],
        },
    )
    trace = file_comm.dir / "traces" / "planner.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(
        '{"event":"atomic_edit_plan","artifact":".harness/atomic_edit_plan.json"}\n'
        '{"event":"usage","cumulative_usage":{"input_tokens":30,"output_tokens":10}}\n',
        encoding="utf-8",
    )

    stats = recover_trace_proven_planner_checkpoint(file_comm, HarnessConfig())

    assert stats is not None
    assert stats.token_usage == {"input_tokens": 30, "output_tokens": 10}
    assert file_comm.read_sprint_plan()["total_sprints"] == 1


def test_atomic_edit_planner_recovers_normalized_response_without_second_call(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "edit_task_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "edit-task-contract-v1",
                "task_mode": "edit",
                "requested_target_routes": ["/", "/settings.html"],
            }
        ),
        encoding="utf-8",
    )
    raw_plan = {
        "schema_version": "atomic-edit-plan-v1",
        "goal": "Create one direct-loadable settings page.",
        "source_anchors": ["Settings"],
        "visual_evidence": "not_required",
        "checks": [
            {
                "id": "physical-pages",
                "route": "/",
                "actions": [
                    {"action": "assert_visible", "selector": "a[href='/']"},
                    {
                        "action": "assert_visible",
                        "selector": "a[href='/settings.html']",
                    },
                    {"action": "click", "selector": "a[href='/settings.html']"},
                    {"action": "assert_url", "value": "/settings.html"},
                    {
                        "action": "assert_visible",
                        "selector": "link[href='styles.css']",
                    },
                    {
                        "action": "assert_visible",
                        "selector": "script[src='app.js']",
                    },
                    {
                        "action": "assert_storage_value",
                        "name": "preferences",
                        "value": "saved",
                    },
                ],
            }
        ],
    }
    trace = file_comm.dir / "traces" / "planner.jsonl"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text(
        "".join(
            json.dumps(event) + "\n"
            for event in [
                {
                    "event": "run_start",
                    "phase": "atomic_edit_planner",
                    "prompt": (
                        "Create the atomic Edit plan for this instruction:\n\n"
                        "Move Settings to /settings.html.\n\n"
                        'Requested target routes: ["/", "/settings.html"]'
                    ),
                },
                {"event": "assistant_response", "content": json.dumps(raw_plan)},
                {
                    "event": "usage",
                    "cumulative_usage": {"input_tokens": 80, "output_tokens": 40},
                    "estimated_cost_usd": 0.002,
                },
            ]
        ),
        encoding="utf-8",
    )

    stats = recover_trace_proven_planner_checkpoint(file_comm, HarnessConfig())

    assert stats is not None
    assert stats.token_usage == {"input_tokens": 80, "output_tokens": 40}
    checks = file_comm.read_ui_verification_plan()["sprints"][0]["checks"]
    assert [item["id"] for item in checks] == ["physical-pages"]
    edit_card = json.loads((file_comm.dir / "edit_card.json").read_text())
    assert edit_card["status"] == "ready"
    assert edit_card["target_routes"] == ["/", "/settings.html"]


def test_final_project_mode_instruction_requests_natural_complete_roadmap():
    from src.agents.planner import _build_planner_prompt

    prompt = _build_planner_prompt(
        HarnessConfig(final_project_mode=True), "build everything", Path("/tmp/work")
    )
    assert "natural number of Sprints" in prompt
    assert "complete requested product" in prompt
    assert "exactly one Sprint" not in prompt


def test_planner_prompt_preserves_existing_frontend_stack(tmp_path: Path):
    from src.agents.planner import _build_planner_prompt

    (tmp_path / "frontend").mkdir()
    prompt = _build_planner_prompt(HarnessConfig(), "add one interaction", tmp_path)

    assert "existing runnable frontend" in prompt
    assert "do not propose a stack migration" in prompt


def test_planner_system_prompt_requires_economical_stack_preserving_plan():
    from src.prompts.planner import PLANNER_SYSTEM_PROMPT

    assert "preserve that stack" in PLANNER_SYSTEM_PROMPT
    assert "no more than 700 words" in PLANNER_SYSTEM_PROMPT
    assert "Do not spend tool calls rereading" in PLANNER_SYSTEM_PROMPT


def test_planner_rejects_malformed_browser_action_contract(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Scroll and check.",
        "expected_result": "Control is visible.", "critical": True, "category": "scroll",
        "actions": [
            {"action": "scroll", "count": 0},
            {"action": "assert_visible", "selector": "#control"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="scroll action requires integer y"):
        _validate_planning_bundle(file_comm)


@pytest.mark.parametrize("route", ["https://example.com/catalog", "//example.com", "../catalog", "/a/../catalog", "/catalog?q=x", "/#/catalog"])
def test_planner_rejects_unsafe_browser_route(tmp_path: Path, route: str):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    plan = file_comm.read_ui_verification_plan()
    assert plan is not None
    plan["sprints"][0]["checks"][0]["route"] = route
    file_comm.write_ui_verification_plan(plan)

    with pytest.raises(PlannerValidationError, match="safe same-origin path"):
        _validate_planning_bundle(file_comm)


def test_planner_requires_final_typed_assertion_for_authored_contract(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Activate control.",
        "expected_result": "State changes.", "critical": True, "category": "interaction",
        "actions": [{"action": "click", "selector": "#control"}],
    }]}]})

    with pytest.raises(PlannerValidationError, match="must end with a typed assertion"):
        _validate_planning_bundle(file_comm)


def test_planner_accepts_related_typed_assertions_in_one_action_contract(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Activate control.",
        "expected_result": "State changes.", "critical": True, "category": "interaction",
        "actions": [
            {"action": "click", "selector": "#control"},
            {"action": "assert_visible", "selector": "#control"},
            {"action": "assert_attribute", "selector": "#control", "name": "data-state", "value": "active"},
        ],
    }]}]})

    _validate_planning_bundle(file_comm)


def test_atomic_edit_planner_drops_only_weak_duplicate_preservation_checks():
    payload = {
        "checks": [
            {
                "id": "preserve-header-and-cards",
                "task": "Preserve the existing header navigation.",
                "actions": [
                    {"action": "set_viewport", "width": 390, "height": 844},
                    {"action": "assert_visible", "selector": "[data-testid='header-navigation']"},
                ],
            },
            {
                "id": "preserve-scroll-position",
                "task": "Preserve the accepted scroll restoration behavior.",
                "actions": [
                    {"action": "reload"},
                    {"action": "assert_scroll", "y": 640, "tolerance": 4},
                ],
            },
            {
                "id": "new-drawer",
                "task": "Verify the new notification drawer.",
                "actions": [
                    {"action": "assert_visible", "selector": "#notification-drawer"},
                ],
            },
        ]
    }

    normalized = _drop_harness_owned_preservation_presence_checks(payload)

    assert [check["id"] for check in normalized["checks"]] == [
        "preserve-scroll-position",
        "new-drawer",
    ]


@pytest.mark.parametrize("attribute", ["class", "style"])
def test_planner_rejects_exact_presentation_attribute_as_state_proxy(
    tmp_path: Path, attribute: str
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Focus the control.",
        "expected_result": "Focus is observable.", "critical": True,
        "category": "accessibility", "route": "/",
        "actions": [
            {"action": "key_press", "selector": "#name", "key": "Tab"},
            {"action": "assert_focus", "selector": "#save"},
            {"action": "assert_attribute", "selector": "#save", "name": attribute, "value": "focused"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="exact class/style assertions are forbidden"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_observational_exact_count_without_declared_fixtures(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Inspect catalog.",
        "expected_result": "Catalog has suggestions.", "critical": True,
        "category": "functionality", "route": "/catalog",
        "actions": [
            {"action": "reload"},
            {"action": "wait_for", "selector": ".item", "state": "visible"},
            {"action": "assert_count", "selector": ".item", "count": 1},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="exact count has no state-producing setup or declared fixtures"):
        _validate_planning_bundle(file_comm)


def test_planner_accepts_filter_literal_backed_by_declared_fixture(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Filter catalog.",
        "expected_result": "Matching fixture remains.", "critical": True,
        "category": "functionality", "route": "/catalog",
        "fixtures": ["Dune"],
        "actions": [
            {"action": "fill", "selector": "#filter", "value": "Dune"},
            {"action": "assert_text", "selector": ".item", "value": "Dune", "match": "contains"},
        ],
    }]}]})

    _validate_planning_bundle(file_comm)


def test_planner_rejects_filter_literal_without_declared_fixture(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Filter catalog.",
        "expected_result": "Matching item remains.", "critical": True,
        "category": "functionality", "route": "/catalog",
        "actions": [
            {"action": "fill", "selector": "#filter", "value": "Dune"},
            {"action": "assert_text", "selector": ".item", "value": "Dune", "match": "contains"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="filter assertion literal 'Dune' is not declared in fixtures"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_more_than_eight_typed_assertions(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    actions = [
        {"action": "assert_visible", "selector": f"#control-{index}"}
        for index in range(9)
    ]
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Inspect one journey.",
        "expected_result": "Related states are visible.", "critical": True,
        "category": "interaction", "route": "/", "actions": actions,
    }]}]})

    with pytest.raises(PlannerValidationError, match="1 to 8 related typed assertions"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_tab_asserting_focus_remains_on_start(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Tab from save.",
        "expected_result": "Focus advances.", "critical": True, "category": "accessibility",
        "route": "/",
        "actions": [
            {"action": "key_press", "selector": "#save", "key": "Tab"},
            {"action": "assert_focus", "selector": "#save"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="destination selector"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_tab_starting_from_global_container(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Tab into the form.",
        "expected_result": "Title receives focus.", "critical": True,
        "category": "accessibility", "route": "/",
        "actions": [
            {"action": "key_press", "selector": "body", "key": "Tab"},
            {"action": "assert_focus", "selector": "#title"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="Tab start must name a focusable control"):
        _validate_planning_bundle(file_comm)


def test_planner_requires_explicit_storage_string_match_mode(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Check saved item.",
        "expected_result": "Storage contains the title.", "critical": True,
        "category": "persistence", "route": "/",
        "actions": [
            {
                "action": "assert_storage_value",
                "storage": "local",
                "key": "items",
                "value": "Dune",
            },
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="string storage assertions require explicit match"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_initial_empty_state_after_stateful_route_checks(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [
        {
            "id": "UI-001", "feature_id": "F001", "task": "Create item.",
            "expected_result": "Item exists.", "critical": True,
            "category": "functionality", "route": "/",
            "actions": [
                {"action": "fill", "selector": "#name", "value": "Atlas"},
                {"action": "click", "selector": "#create"},
                {"action": "assert_count", "selector": ".item", "count": 1},
            ],
        },
        {
            "id": "UI-002", "feature_id": "F001", "task": "Show initial empty state.",
            "expected_result": "Empty guidance is visible.", "critical": True,
            "category": "empty_state", "route": "/",
            "actions": [
                {"action": "reload"},
                {"action": "assert_visible", "selector": ".empty-state"},
            ],
        },
    ]}]})

    with pytest.raises(PlannerValidationError, match="initial empty-state reload"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_persistence_count_not_established_in_current_sprint(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [
        {
            "id": "UI-001", "feature_id": "F001", "task": "Save from catalog.",
            "expected_result": "One item is visible.", "critical": True,
            "category": "functionality", "route": "/library.html",
            "actions": [
                {"action": "click", "selector": ".save-btn"},
                {"action": "click", "selector": "a[href='/']"},
                {"action": "assert_url", "value": "/"},
                {"action": "assert_visible", "selector": ".item"},
            ],
        },
        {
            "id": "UI-002", "feature_id": "F001", "task": "Reload saved items.",
            "expected_result": "Two items persist.", "critical": True,
            "category": "persistence", "route": "/",
            "actions": [
                {"action": "reload"},
                {"action": "assert_count", "selector": ".item", "count": 2},
            ],
        },
    ]}]})

    with pytest.raises(PlannerValidationError, match="current-sprint setup establishes only 1"):
        _validate_planning_bundle(file_comm)


def test_planner_accepts_persistence_count_established_after_route_transition(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [
        {
            "id": "UI-001", "feature_id": "F001", "task": "Save from catalog.",
            "expected_result": "One item is visible.", "critical": True,
            "category": "functionality", "route": "/library.html",
            "actions": [
                {"action": "click", "selector": ".save-btn"},
                {"action": "click", "selector": "a[href='/']"},
                {"action": "assert_url", "value": "/"},
                {"action": "assert_visible", "selector": ".item"},
            ],
        },
        {
            "id": "UI-002", "feature_id": "F001", "task": "Reload saved item.",
            "expected_result": "One item persists.", "critical": True,
            "category": "persistence", "route": "/",
            "actions": [
                {"action": "reload"},
                {"action": "assert_count", "selector": ".item", "count": 1},
            ],
        },
    ]}]})

    _validate_planning_bundle(file_comm)


def test_planner_rejects_legacy_model_authored_evaluate(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Activate control.",
        "expected_result": "State changes.", "critical": True, "category": "interaction",
        "actions": [{"action": "evaluate", "expression": "true"}],
    }]}]})

    with pytest.raises(PlannerValidationError, match="must end with a typed assertion"):
        _validate_planning_bundle(file_comm)


def test_planner_reports_all_typed_contract_errors_in_one_retry(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [
        {
            "id": "UI-001", "feature_id": "F001", "task": "Check class.",
            "expected_result": "Changed.", "critical": True, "category": "interaction",
            "actions": [{
                "action": "assert_attribute", "selector": "#control",
                "attribute": "class", "match": "contains", "value": "active",
            }],
        },
        {
            "id": "UI-002", "feature_id": "F001", "task": "Check URL.",
            "expected_result": "Changed.", "critical": True, "category": "interaction",
            "actions": [{"action": "assert_url", "url": "/done"}],
        },
    ]}]})

    with pytest.raises(PlannerValidationError) as caught:
        _validate_planning_bundle(file_comm)

    message = str(caught.value)
    assert "UI-001" in message and "unsupported fields: attribute" in message
    assert "UI-002" in message and "unsupported fields: url" in message


def test_planner_rejects_invalid_action_settle_time(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Type then check.",
        "expected_result": "State changes.", "critical": True, "category": "interaction",
        "actions": [
            {"action": "fill", "selector": "#control", "value": "x", "settle_ms": 9000},
            {"action": "assert_visible", "selector": "#control"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="settle_ms must be an integer from 0 to 5000"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_unsupported_browser_action_before_evaluation(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Use unsupported action.",
        "expected_result": "State changes.", "critical": True, "category": "interaction",
        "actions": [
            {"action": "teleport", "selector": "#control"},
            {"action": "assert_visible", "selector": "#control"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="unsupported action 'teleport'"):
        _validate_planning_bundle(file_comm)


def test_planner_rejects_unsafe_upload_fixture_contract(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [{
        "id": "UI-001", "feature_id": "F001", "task": "Upload fixture.",
        "expected_result": "File is accepted.", "critical": True, "category": "interaction",
        "actions": [
            {
                "action": "set_input_files",
                "selector": "#upload",
                "files": [{"name": "../secret.txt", "mime_type": "text/plain", "content": "x"}],
            },
            {"action": "assert_visible", "selector": "#upload"},
        ],
    }]}]})

    with pytest.raises(PlannerValidationError, match="safe base name"):
        _validate_planning_bundle(file_comm)


@pytest.mark.anyio
async def test_planner_initializes_accepted_sprints_after_successful_run(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        _write_valid_planning_bundle(file_comm)
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                usage={"input_tokens": 100_000},
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    stats = await run_planner(
        HarnessConfig(planner_model="claude-sonnet-4-6"),
        "build a counter app",
        file_comm,
        tmp_path,
    )

    # claude-sonnet-4-6 at $3 per 1M input tokens
    # → 100_000 * 3 / 1e6 = $0.30.
    assert stats.cost_usd == 0.3
    assert stats.duration_ms == 1
    assert file_comm.read_spec().startswith("# Counter App - Track Every Tap")
    assert file_comm.read_accepted_sprints() == {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 0,
    }


@pytest.mark.anyio
async def test_planner_raises_when_planning_bundle_is_missing_required_artifact(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_spec(_valid_spec_text())
        file_comm.write_progress("# Progress")
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(PlannerValidationError, match="design_tokens.json"):
        await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)


@pytest.mark.anyio
async def test_planner_raises_when_planning_bundle_is_malformed(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_spec(_valid_spec_text())
        file_comm.write_design_tokens(
            {
                "theme_name": "editorial counter",
                "color": {"bg": "#111111"},
                "typography": {"display": "Space Grotesk"},
                "spacing": {"base": 8},
                "radius": {"card": 16},
                "motion": {"duration_fast": 160},
                "style_rules": ["bold hierarchy"],
                "anti_patterns": ["generic cards"],
                "visual_experiment": {
                    "design_hypothesis": "Use poster-like asymmetry.",
                    "reason_for_image_first": "Text-only outputs stay too templated.",
                    "desired_break_from_web_templates": ["poster-like asymmetry"],
                    "visual_opportunities_beyond_css": ["ink texture"],
                    "forbidden_generic_patterns": ["centered card grid"],
                },
            }
        )
        file_comm.write_feature_list({"features": []})
        file_comm.write_sprint_plan(
            {
                "total_sprints": 1,
                "sprints": [
                    {
                        "number": 1,
                        "title": "Core counter",
                        "goal": "Ship the primary counter flow.",
                        "feature_ids": ["F001"],
                        "deliverables": ["Visible counter UI."],
                        "exit_criteria": ["Counter increments correctly."],
                    }
                ],
            }
        )
        file_comm.write_ui_verification_plan(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-001",
                                "feature_id": "F001",
                                "task": "Click increment once.",
                                "expected_result": "Counter changes by one step.",
                                "critical": True,
                                "category": "core_interaction",
                            }
                        ],
                    }
                ]
            }
        )
        file_comm.write_progress("# Progress")
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(PlannerValidationError, match="F001"):
        await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)


@pytest.mark.anyio
async def test_planner_rejects_missing_visual_experiment(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_spec(_valid_spec_text())
        (file_comm.dir / "design_tokens.json").write_text(
            json.dumps(
                {
                    "theme_name": "editorial counter",
                    "color": {"bg": "#111111"},
                    "typography": {"display": "Space Grotesk"},
                    "spacing": {"base": 8},
                    "radius": {"card": 16},
                    "motion": {"duration_fast": 160},
                    "style_rules": ["bold hierarchy"],
                    "anti_patterns": ["generic cards"],
                    "visual_experiment": {},
                }
            ),
            encoding="utf-8",
        )
        file_comm.write_feature_list(
            {
                "features": [
                    {
                        "id": "F001",
                        "name": "Counter",
                        "priority": "high",
                        "depends_on": [],
                        "description": "Count values.",
                        "acceptance_criteria": ["Counter increments correctly."],
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
                        "title": "Core counter",
                        "goal": "Ship the primary counter flow.",
                        "feature_ids": ["F001"],
                        "deliverables": ["Visible counter UI."],
                        "exit_criteria": ["Counter increments correctly."],
                    }
                ],
            }
        )
        file_comm.write_ui_verification_plan(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-001",
                                "feature_id": "F001",
                                "task": "Click increment once.",
                                "expected_result": "Counter changes by one step.",
                                "critical": True,
                                "category": "core_interaction",
                            }
                        ],
                    }
                ]
            }
        )
        file_comm.write_progress("# Progress")
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(PlannerValidationError, match="schema validation"):
        await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)


@pytest.mark.anyio
async def test_planner_prompt_explicitly_forbids_bash_and_uses_precreated_artifacts(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    captured: dict[str, str] = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        captured["system_prompt"] = kwargs["system_prompt"]
        captured["stop_hooks"] = kwargs["stop_hooks"]
        assert file_comm.read_spec().startswith("# Draft Product - Working Title")
        assert file_comm.read_progress() == "# Progress Log\n"
        assert (file_comm.dir / "design_tokens.json").exists() is True
        assert (file_comm.dir / "feature_list.json").exists() is True
        assert (file_comm.dir / "sprint_plan.json").exists() is True
        assert (file_comm.dir / "ui_verification_plan.json").exists() is True
        _write_valid_planning_bundle(file_comm)
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)

    assert "Bash is unavailable for this task." in captured["prompt"]
    assert "The Harness has already prepared the workdir, the .harness directory, and the required artifact files." in captured["prompt"]
    assert "Replace the scaffold content in those files" in captured["prompt"]
    assert "`Bash` is unavailable for this task." in captured["system_prompt"]
    assert "The Harness prepares the workdir, the `.harness/` directory, and the required artifact" in captured["system_prompt"]
    assert "Use `total_sprints` exactly as written, never `total_sprint`." in captured["system_prompt"]
    assert "Every sprint entry must include at least one item in `feature_ids`" in captured["system_prompt"]
    assert "ensure all six required artifacts were written" in captured["system_prompt"]
    assert len(captured["stop_hooks"]) == 1


@pytest.mark.anyio
async def test_planner_prepares_missing_workdir_and_harness_dir(monkeypatch, tmp_path: Path):
    workdir = tmp_path / "missing-workdir"
    file_comm = FileComm(workdir / ".harness")
    harness_dir = workdir / ".harness"
    if harness_dir.exists():
        harness_dir.rmdir()
    if workdir.exists():
        workdir.rmdir()

    async def fake_run_sdk_agent(**kwargs):
        assert workdir.exists() is True
        assert harness_dir.exists() is True
        assert (harness_dir / "spec.md").exists() is True
        assert (harness_dir / "design_tokens.json").exists() is True
        assert (harness_dir / "feature_list.json").exists() is True
        assert (harness_dir / "sprint_plan.json").exists() is True
        assert (harness_dir / "ui_verification_plan.json").exists() is True
        assert (harness_dir / "progress.md").exists() is True
        _write_valid_planning_bundle(file_comm)
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    await run_planner(HarnessConfig(), "build a counter app", file_comm, workdir)

    assert workdir.exists() is True
    assert harness_dir.exists() is True


@pytest.mark.anyio
async def test_planner_stop_hook_blocks_invalid_bundle(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.initialize_planning_artifacts()
    hook = _make_planner_stop_hook(file_comm, HarnessConfig())

    result = await hook({}, None, None)

    assert result["decision"] == "block"
    assert "Planning artifact validation failed" in result["reason"]
    assert ".harness/spec.md" in result["reason"]


@pytest.mark.anyio
async def test_planner_stop_hook_allows_valid_bundle(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    hook = _make_planner_stop_hook(file_comm, HarnessConfig())

    result = await hook({}, None, None)

    assert result == {"decision": "complete"}


# --- cross-ref consistency between the three plan files ---


def _seed_valid_bundle(file_comm: FileComm) -> None:
    """Write a self-consistent planning bundle with two features and two sprints."""
    file_comm.write_spec(_valid_spec_text())
    file_comm.write_design_tokens(
        {
            "theme_name": "editorial counter",
            "color": {"bg": "#111111"},
            "typography": {"display": "Space Grotesk"},
            "spacing": {"base": 8},
            "radius": {"card": 16},
            "motion": {"duration_fast": 160},
            "style_rules": ["bold hierarchy"],
            "anti_patterns": ["generic cards"],
            "visual_experiment": {
                "design_hypothesis": "Use poster-like asymmetry.",
                "reason_for_image_first": "Text-only outputs stay too templated.",
                "desired_break_from_web_templates": ["poster-like asymmetry"],
                "visual_opportunities_beyond_css": ["ink texture"],
                "forbidden_generic_patterns": ["centered card grid"],
            },
        }
    )
    file_comm.write_feature_list(
        {
            "features": [
                {
                    "id": "F001",
                    "name": "Counter",
                    "priority": "high",
                    "depends_on": [],
                    "description": "Count values.",
                    "acceptance_criteria": ["Counter increments correctly."],
                    "status": "planned",
                    "sprint": 1,
                },
                {
                    "id": "F002",
                    "name": "Polish",
                    "priority": "medium",
                    "depends_on": ["F001"],
                    "description": "Animate.",
                    "acceptance_criteria": ["Animation runs."],
                    "status": "planned",
                    "sprint": 2,
                },
            ]
        }
    )
    file_comm.write_sprint_plan(
        {
            "total_sprints": 2,
            "sprints": [
                {
                    "number": 1,
                    "title": "Core counter",
                    "goal": "Ship the primary counter flow.",
                    "feature_ids": ["F001"],
                    "deliverables": ["Visible counter UI."],
                    "exit_criteria": ["Counter increments correctly."],
                },
                {
                    "number": 2,
                    "title": "Polish",
                    "goal": "Add motion.",
                    "feature_ids": ["F002"],
                    "deliverables": ["Animated counter."],
                    "exit_criteria": ["Animation runs."],
                },
            ],
        }
    )
    file_comm.write_ui_verification_plan(
        {
            "sprints": [
                {
                    "sprint": 1,
                    "checks": [
                        {
                            "id": "UI-001",
                            "feature_id": "F001",
                            "task": "Click increment once.",
                            "expected_result": "Counter changes by one step.",
                            "critical": True,
                            "category": "core_interaction",
                        }
                    ],
                },
                {
                    "sprint": 2,
                    "checks": [
                        {
                            "id": "UI-002",
                            "feature_id": "F002",
                            "task": "Observe animation.",
                            "expected_result": "Counter animates.",
                            "critical": False,
                            "category": "appearance",
                        }
                    ],
                },
            ]
        }
    )
    file_comm.write_progress("# Progress Log\n\n## planning\n- status: complete")


def test_validate_planning_bundle_passes_for_consistent_bundle(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)
    # No exception → bundle is internally consistent.
    _validate_planning_bundle(file_comm)
    assert file_comm.read_sprint_plan()["total_sprints"] == 2


def test_validate_planning_bundle_rejects_dangling_sprint_feature_id(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["feature_ids"] = ["F999"]  # not in feature_list
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match="F999"):
        _validate_planning_bundle(file_comm)


def test_validate_planning_bundle_rejects_dangling_ui_check_feature_id(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    plan = file_comm.read_ui_verification_plan()
    plan["sprints"][0]["checks"][0]["feature_id"] = "F404"
    file_comm.write_ui_verification_plan(plan)

    with pytest.raises(PlannerValidationError, match="F404"):
        _validate_planning_bundle(file_comm)


def test_validate_planning_bundle_rejects_feature_assigned_to_unknown_sprint(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    feature_list = file_comm.read_feature_list()
    feature_list["features"][0]["sprint"] = 99  # outside total_sprints=2
    file_comm.write_feature_list(feature_list)

    with pytest.raises(PlannerValidationError, match="sprint"):
        _validate_planning_bundle(file_comm)


def test_validate_planning_bundle_rejects_sprint_plan_missing_a_sprint_number(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    # total_sprints=2 but sprint_plan only declares sprint 1.
    sprint_plan["sprints"] = [sprint_plan["sprints"][0]]
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match="sprint"):
        _validate_planning_bundle(file_comm)


# --- sprint sizing caps ---


def test_validate_sprint_plan_rejects_too_many_deliverables(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["deliverables"] = [f"Deliverable {i}" for i in range(6)]
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match=r"deliverables.*max allowed is 5"):
        _validate_planning_bundle(
            file_comm, HarnessConfig(max_deliverables_per_sprint=5)
        )


def test_validate_sprint_plan_rejects_too_many_exit_criteria(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["exit_criteria"] = [f"Criterion {i}" for i in range(6)]
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match=r"exit_criteria.*max allowed is 5"):
        _validate_planning_bundle(
            file_comm, HarnessConfig(max_exit_criteria_per_sprint=5)
        )


def test_validate_sprint_plan_accepts_at_cap(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["deliverables"] = [f"D{i}" for i in range(5)]
    sprint_plan["sprints"][0]["exit_criteria"] = [f"C{i}" for i in range(5)]
    file_comm.write_sprint_plan(sprint_plan)

    _validate_planning_bundle(file_comm)


def test_validate_sprint_plan_respects_config_override_for_caps(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["deliverables"] = [f"D{i}" for i in range(8)]
    file_comm.write_sprint_plan(sprint_plan)

    relaxed = HarnessConfig(max_deliverables_per_sprint=8)
    _validate_planning_bundle(file_comm, relaxed)


def test_expansive_data_enforces_three_item_sprint_cap(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)
    sprint_plan = file_comm.read_sprint_plan()
    feature_list = file_comm.read_feature_list()
    feature_id = feature_list["features"][0]["id"]
    sprint_plan["total_sprints"] = 6
    template = sprint_plan["sprints"][0]
    sprint_plan["sprints"] = []
    for number in range(1, 7):
        item = dict(template)
        item["number"] = number
        item["feature_ids"] = [feature_id]
        item["deliverables"] = [f"D{i}" for i in range(5)]
        sprint_plan["sprints"].append(item)
    file_comm.write_sprint_plan(sprint_plan)
    feature_list["features"][0]["sprint"] = 1
    file_comm.write_feature_list(feature_list)
    verification = file_comm.read_ui_verification_plan()
    verification["sprints"] = [verification["sprints"][0]]
    file_comm.write_ui_verification_plan(verification)

    config = HarnessConfig(planner_scope_mode="expansive-data")
    with pytest.raises(PlannerValidationError, match=r"deliverables.*max allowed is 3"):
        _validate_planning_bundle(file_comm, config)


def test_planner_prompt_documents_sprint_size_caps():
    from src.prompts.planner import PLANNER_SYSTEM_PROMPT

    # Bounded caps remain visible while allowing one coherent multi-page slice.
    assert "10 concise deliverables" in PLANNER_SYSTEM_PROMPT
    assert "10 concise exit_criteria" in PLANNER_SYSTEM_PROMPT
    assert "vertical slice" in PLANNER_SYSTEM_PROMPT.lower()


def test_expansive_data_scope_uses_shallow_natural_sprint_expansion():
    from src.prompts.planner import planner_system_prompt

    prompt = planner_system_prompt("expansive-data")
    assert "6-9 dependency-ordered Sprints" in prompt
    assert "2-3 closely related user-visible deliverables" in prompt
    assert 'standalone\n   "polish/refactor/cleanup" Sprint' in prompt
    assert "generate/edit" in prompt


def test_query_aligned_scope_does_not_enable_legacy_expansion():
    from src.prompts.planner import planner_system_prompt

    prompt = planner_system_prompt("query-aligned")
    assert "Scope Profile: Expansive Data Construction" not in prompt


def test_drag_destination_alias_preserves_target_and_rejects_conflict():
    action = {"action": "drag_and_drop", "source": "#card", "destination": "#ready"}
    normalized = _normalize_bounded_planner_actions({"checks": [{"actions": [action]}]})
    assert normalized["checks"][0]["actions"][0] == {"action":"drag_and_drop", "source_selector":"#card", "target_selector":"#ready"}
    action["target_selector"] = "#other"
    conflicting = _normalize_bounded_planner_actions({"checks":[{"actions":[action]}]})
    assert conflicting["checks"][0]["actions"][0]["destination"] == "#ready"
