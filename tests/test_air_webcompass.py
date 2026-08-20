from __future__ import annotations

import json

import pytest

from src.task_generation.air_webcompass import (
    WEBCOMPASS_EDIT_ACTION_PROFILES,
    WEBCOMPASS_EDIT_TYPES,
    WEBCOMPASS_REPAIR_TYPES,
    SeedContextBudgetError,
    append_jsonl_record,
    build_repair_training_input,
    load_seed_code,
    parse_sse_content,
    validate_initial_candidate,
    validate_refinement,
)
from src.orchestration.ui_action_contracts import SUPPORTED_UI_ACTIONS


def test_webcompass_taxonomies_are_closed_and_complete():
    assert len(WEBCOMPASS_EDIT_TYPES) == 40
    assert len(set(WEBCOMPASS_EDIT_TYPES)) == 40
    assert "Responsive Navigation" in WEBCOMPASS_EDIT_TYPES
    assert len(WEBCOMPASS_REPAIR_TYPES) == 11
    assert len(set(WEBCOMPASS_REPAIR_TYPES)) == 11
    assert "Loss of Interactivity" in WEBCOMPASS_REPAIR_TYPES


def test_every_0805_edit_type_has_supported_typed_browser_actions():
    assert set(WEBCOMPASS_EDIT_ACTION_PROFILES) == set(WEBCOMPASS_EDIT_TYPES)
    assert all(
        set(actions) <= SUPPORTED_UI_ACTIONS
        for actions in WEBCOMPASS_EDIT_ACTION_PROFILES.values()
    )
    assert "drag_and_drop" in WEBCOMPASS_EDIT_ACTION_PROFILES["Drag & Drop Interface"]
    assert "set_input_files" in WEBCOMPASS_EDIT_ACTION_PROFILES["File Upload with Progress"]
    assert "emulate_media" in WEBCOMPASS_EDIT_ACTION_PROFILES["Print Stylesheet"]


def test_initial_candidate_requires_exact_requested_types():
    payload = {
        "query": "Add an accessible accordion to the existing FAQ while preserving its content and visual style.",
        "task_descriptions": [
            {
                "task_type": "Accordion",
                "description": "Allow readers to expand and collapse existing FAQ answers with keyboard support.",
            }
        ],
        "acceptance_criteria": [
            "Clicking a question toggles only its associated answer.",
            "Enter and Space operate the focused question control.",
        ],
        "preservation_requirements": ["Keep every existing question and answer."],
    }

    result = validate_initial_candidate(payload, ("Accordion",))

    assert result["task_descriptions"][0]["task_type"] == "Accordion"
    with pytest.raises(ValueError, match="task types"):
        validate_initial_candidate(payload, ("Tabs",))


def test_refinement_adds_one_failure_backed_constraint():
    payload = {
        "selected_type": "Prior Condition",
        "constraint": "When the menu is open, Escape closes it and returns focus to the toggle.",
        "evidence_ids": ["UI-004"],
        "refined_query": "Add responsive navigation. When the menu is open, Escape must close it and return focus to the toggle.",
        "reason": "The browser trace shows that Escape leaves the menu open.",
    }

    result = validate_refinement(
        payload,
        original_query="Add responsive navigation.",
        allowed_evidence_ids={"UI-004", "UI-005"},
    )

    assert result["evidence_ids"] == ["UI-004"]
    with pytest.raises(ValueError, match="unknown evidence"):
        validate_refinement(
            {**payload, "evidence_ids": ["UI-999"]},
            original_query="Add responsive navigation.",
            allowed_evidence_ids={"UI-004"},
        )


def test_repair_training_input_is_code_only():
    files = [
        {"path": "index.html", "code": "<button>Open</button>"},
        {"path": "styles.css", "code": "button { color: red; }"},
    ]

    assert build_repair_training_input(files) == files


def test_seed_loader_is_deterministic_and_ignores_hidden_files(tmp_path):
    (tmp_path / "styles.css").write_text("body {}", encoding="utf-8")
    (tmp_path / "index.html").write_text("<main>Hello</main>", encoding="utf-8")
    (tmp_path / ".secret").write_text("do not read", encoding="utf-8")

    files = load_seed_code(tmp_path)

    assert [item["path"] for item in files] == ["index.html", "styles.css"]


def test_seed_loader_never_silently_truncates_multi_file_context(tmp_path):
    for index in range(3):
        (tmp_path / f"page-{index}.js").write_text("x" * 20, encoding="utf-8")

    with pytest.raises(SeedContextBudgetError, match="3 supported files"):
        load_seed_code(tmp_path, max_files=2, max_chars=1_000)
    with pytest.raises(SeedContextBudgetError, match="60 source characters"):
        load_seed_code(tmp_path, max_files=10, max_chars=50)

    files = load_seed_code(tmp_path, max_files=3, max_chars=60)
    assert len(files) == 3
    assert sum(len(item["code"]) for item in files) == 60


def test_jsonl_results_are_appended(tmp_path):
    target = tmp_path / "results.jsonl"

    append_jsonl_record(target, {"case_id": "a", "status": "ok"})
    append_jsonl_record(target, {"case_id": "b", "status": "error"})

    rows = [json.loads(line) for line in target.read_text().splitlines()]
    assert [row["case_id"] for row in rows] == ["a", "b"]


def test_parse_sse_content_joins_text_and_ignores_done():
    lines = [
        'data: {"choices":[{"delta":{"content":"PO"}}]}',
        'data: {"choices":[{"delta":{"content":"NG"}}]}',
        "data: [DONE]",
    ]

    assert parse_sse_content(lines) == "PONG"
