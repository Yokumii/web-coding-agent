from __future__ import annotations

import json
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
