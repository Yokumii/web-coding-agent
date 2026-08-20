from __future__ import annotations

import json
import gzip
from pathlib import Path

from scripts.audit_forward_alignment import audit


def test_audit_detects_repair_contract_and_shape(tmp_path: Path):
    (tmp_path / "text-repair.v2.jsonl").write_text(json.dumps({
        "instance_id": "r1", "task": "text-repair", "task_type": ["Interaction"],
        "instruction": [{"path": "app.js", "code": "bad"}],
        "response": [{"path": "app.js", "search": "bad", "replace": "good", "task_type": "Interaction"}],
    }) + "\n")
    (tmp_path / "text-edit.v2.jsonl").write_text(json.dumps({
        "instance_id": "e1", "task": "text-editing", "task_type": ["Search"],
        "instruction": {"src_code": [{"path": "app.js", "code": "old"}], "description": "Add search."},
        "response": [{"path": "app.js", "search": "old", "replace": "new", "task_type": "Search"}],
    }) + "\n")

    result = audit(tmp_path)

    assert result["forward"]["text-editing"]["reverse_shape_eligible"] == 1
    assert result["forward"]["text-repair"]["contract_errors"] == []
    assert result["alignment"]["text-editing"]["observed_atomic_task_types"] == ["Search"]
    assert result["harness_action_capabilities"]["status"] == "covered"


def test_audit_reads_authoritative_gzip_reference_without_materializing_it(tmp_path: Path):
    record = {
        "instance_id": "e1",
        "task": "text-editing",
        "task_type": ["Tooltip"],
        "page_type": "mp",
        "instruction": {
            "src_code": [{"path": "index.html", "code": "old"}],
            "description": [{"task_type": "Tooltip", "description": "Add tooltip."}],
        },
        "response": [
            {
                "path": "index.html",
                "search": "old",
                "replace": "new",
                "task_type": "Tooltip",
            }
        ],
    }
    reference = tmp_path / "reference.jsonl.gz"
    with gzip.open(reference, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

    result = audit(tmp_path, reference_edit=reference)

    summary = result["reverse_reference"]["text-editing"]
    assert summary["count"] == 1
    assert summary["page_type_distribution"] == {"mp": 1}
    assert summary["atomic_task_types"] == ["Tooltip"]
