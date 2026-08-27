from __future__ import annotations

from scripts.validate_webcompass_edit_matrix import (
    MATRIX_CASES,
    pagination_patches,
    source_destination_maps,
)
from src.orchestration.ui_action_contracts import validate_ui_action_sequence


def test_real_edit_matrix_covers_multi_page_hash_and_minimality_cases():
    assert len(MATRIX_CASES) == 5
    assert any(len(case.target_routes) == 2 for case in MATRIX_CASES)
    assert any("#" in route for case in MATRIX_CASES for route in case.target_routes)
    assert sum(case.certify_minimality for case in MATRIX_CASES) == 1
    for case in MATRIX_CASES:
        for check in (*case.target_checks, *case.protected_checks):
            validate_ui_action_sequence(list(check["actions"]))


def test_source_destination_maps_replays_ordered_ground_truth_patches():
    row = {
        "instruction": {"src_code": [{"path": "app.js", "code": "a\nb\n"}]},
        "response": [
            {
                "task_type": "Pagination",
                "path": "app.js",
                "search": "a",
                "replace": "A",
            },
            {
                "task_type": "Pagination",
                "path": "app.js",
                "search": "b",
                "replace": "B",
            },
        ],
    }
    patches = pagination_patches(row)
    source, destination = source_destination_maps(row, patches)

    assert source == {"app.js": "a\nb\n"}
    assert destination == {"app.js": "A\nB\n"}
