from __future__ import annotations

import json
from types import SimpleNamespace

from scripts.calibrate_minimal_path_cases import _existing_ids, _patch_order


def test_existing_ids_retries_transient_results_without_rewriting_history(tmp_path):
    output = tmp_path / "records.jsonl"
    output.write_text(
        "\n".join(
            json.dumps({"case_id": case_id, "status": status})
            for case_id, status in (
                ("timed-out", "timeout"),
                ("errored", "error"),
                ("accepted", "ok"),
                ("policy-rejected", "rejected"),
            )
        )
        + "\n"
    )

    assert _existing_ids(output) == {"accepted", "policy-rejected"}


def test_patch_order_traverses_recorded_reference_edges_in_both_directions():
    plan = {
        "source_change_cone": {
            "initial_paths": ["frontend/styles.css"],
            "dependency_edges": [
                {"from": "frontend/index.html", "to": "frontend/main.js"},
                {"from": "frontend/index.html", "to": "frontend/styles.css"},
            ],
        }
    }
    patches = [
        SimpleNamespace(path="main.js", change_id="p003"),
        SimpleNamespace(path="index.html", change_id="p002"),
        SimpleNamespace(path="styles.css", change_id="p001"),
    ]

    ordered = _patch_order(plan, patches)

    assert [patch.path for patch in ordered] == [
        "styles.css",
        "index.html",
        "main.js",
    ]
