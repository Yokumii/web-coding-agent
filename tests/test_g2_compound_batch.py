from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_g2_compound_batch.py"
SPEC = importlib.util.spec_from_file_location("g2_compound_batch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_selection_round_robins_task_count_buckets():
    plan = {
        "jobs": [
            {"instance_id": f"{count}-{index}", "task_count": count, "case": "x"}
            for count in (4, 5, 6, 7)
            for index in range(8)
        ]
    }

    selected = MODULE._select_jobs(plan, 20)

    assert len(selected) == 20
    assert {
        count: sum(item["task_count"] == count for item in selected)
        for count in (4, 5, 6, 7)
    } == {4: 5, 5: 5, 6: 5, 7: 5}
