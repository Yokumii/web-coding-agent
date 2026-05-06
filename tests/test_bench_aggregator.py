from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.bench.aggregator import (
    aggregate,
    parse_appearance_grade,
    parse_first_grade_int,
    parse_ui_accuracy,
    render_markdown,
)
from src.bench.manifest import (
    BenchRecord,
    HarnessRecord,
    Manifest,
    PackageRecord,
    SampleRecord,
)


def _manifest_with(samples: list[SampleRecord]) -> Manifest:
    return Manifest(
        run_id="r1", created_at="2026-05-05T00:00:00",
        jsonl_source="test.jsonl", strata="application_type", samples=samples,
    )


def _make_interact_messages(extracted_dir: Path, app_id: str,
                            sub_idx: int, verdict_text: str) -> None:
    task_dir = extracted_dir / "results" / f"task{app_id}_{sub_idx}"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "interact_messages.json").write_text(json.dumps([
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": verdict_text},
    ]))


def _make_shots_result(extracted_dir: Path, app: str, model_output: str) -> None:
    app_dir = extracted_dir / app / "shots"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "result.json").write_text(json.dumps({"model_output": model_output}))


def test_parse_first_grade_int_extracts_digit() -> None:
    text = "Analysis: looks good\n\nGrade: 4\n"
    assert parse_first_grade_int(text) == 4


def test_parse_first_grade_int_returns_zero_when_missing() -> None:
    assert parse_first_grade_int("no grade here") == 0


def test_parse_ui_accuracy_yes_partial_no_average(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    # app_id 000001; 3 sub-tasks: YES, PARTIAL, NO
    _make_interact_messages(extracted, "000001", 0, "Done. YES")
    _make_interact_messages(extracted, "000001", 1, "PARTIAL achievement")
    _make_interact_messages(extracted, "000001", 2, "Could not. NO")

    acc = parse_ui_accuracy(extracted, app_id="000001", sub_count=3)

    # (1 + 0.5 + 0) / 3 = 0.5
    assert acc == pytest.approx(0.5)


def test_parse_ui_accuracy_missing_subtask_counts_as_zero(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    _make_interact_messages(extracted, "000002", 0, "YES")
    # sub-task 1 missing — counts as 0

    acc = parse_ui_accuracy(extracted, app_id="000002", sub_count=2)

    assert acc == pytest.approx(0.5)


def test_parse_ui_accuracy_returns_none_when_no_subtasks(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    acc = parse_ui_accuracy(extracted, app_id="000001", sub_count=0)
    assert acc is None


def test_parse_appearance_grade_reads_shots_result(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    _make_shots_result(extracted, "000001", "...\nGrade: 5")

    grade = parse_appearance_grade(extracted, app_id="000001")

    assert grade == 5.0


def test_parse_appearance_grade_handles_nested_app_dir(tmp_path: Path) -> None:
    """webgen unzips into <app_id>/<inner>/ when zip has wrapper dir."""
    extracted = tmp_path / "extracted"
    inner = extracted / "000001" / "wrapper" / "shots"
    inner.mkdir(parents=True)
    (inner / "result.json").write_text(json.dumps({"model_output": "Grade: 3"}))

    grade = parse_appearance_grade(extracted, app_id="000001")

    assert grade == 3.0


def test_parse_appearance_grade_returns_none_when_missing(tmp_path: Path) -> None:
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    grade = parse_appearance_grade(extracted, app_id="000001")
    assert grade is None


def test_aggregate_writes_summary(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "r1"
    bench_input = run_dir / "bench_input"
    extracted = bench_input / "extracted"

    # sample 1 (000001): UI 1.0, appearance 5
    _make_interact_messages(extracted, "000001", 0, "YES")
    _make_shots_result(extracted, "000001", "Grade: 5")
    # sample 2 (000002): UI 0.0, appearance 1 (errored bench)
    _make_interact_messages(extracted, "000002", 0, "NO")
    _make_shots_result(extracted, "000002", "Grade: 1")

    sample_1_raw = {"id": "000001", "ui_instruct": [{"task": "x", "expected_result": "y",
                                                     "task_category": {}}]}
    sample_2_raw = {"id": "000002", "ui_instruct": [{"task": "x", "expected_result": "y",
                                                     "task_category": {}}]}

    s1 = SampleRecord(id="000001", instruction="i1",
                      harness=HarnessRecord(status="completed", last_verdict="completed",
                                            cost_usd=10.0, rounds=2),
                      package=PackageRecord(status="done"),
                      bench=BenchRecord(status="done"))
    s2 = SampleRecord(id="000002", instruction="i2",
                      harness=HarnessRecord(status="completed", last_verdict="failed_review",
                                            cost_usd=5.0, rounds=3),
                      package=PackageRecord(status="done"),
                      bench=BenchRecord(status="done"))
    manifest = _manifest_with([s1, s2])

    summary = aggregate(
        manifest=manifest,
        bench_input_dir=bench_input,
        sample_raw_by_id={"000001": sample_1_raw, "000002": sample_2_raw},
    )

    assert summary["totals"]["samples"] == 2
    assert summary["totals"]["ui_accuracy_mean"] == pytest.approx(0.5)
    assert summary["totals"]["appearance_grade_mean"] == pytest.approx(3.0)
    assert summary["totals"]["total_cost_usd"] == pytest.approx(15.0)
    assert summary["by_verdict"]["completed"]["n"] == 1
    assert summary["by_verdict"]["failed_review"]["n"] == 1
    assert summary["by_verdict"]["completed"]["ui_acc_mean"] == pytest.approx(1.0)
    assert summary["by_verdict"]["failed_review"]["ui_acc_mean"] == pytest.approx(0.0)
    assert len(summary["samples"]) == 2

    # manifest got updated with computed numbers
    assert s1.bench.ui_accuracy == pytest.approx(1.0)
    assert s1.bench.appearance_grade == 5.0
    assert s2.bench.ui_accuracy == pytest.approx(0.0)
    assert s2.bench.appearance_grade == 1.0


def test_render_markdown_contains_per_sample_rows() -> None:
    summary = {
        "run_id": "r1",
        "totals": {"samples": 1, "harness_completed": 1, "harness_errored": 0,
                   "ui_accuracy_mean": 0.5, "appearance_grade_mean": 3.0, "total_cost_usd": 10.0},
        "by_verdict": {"completed": {"n": 1, "ui_acc_mean": 0.5, "app_mean": 3.0}},
        "samples": [{
            "id": "000001",
            "harness": {"last_verdict": "completed", "rounds": 2, "cost_usd": 10.0},
            "bench": {"ui_accuracy": 0.5, "appearance_grade": 3.0},
        }],
    }
    md = render_markdown(summary)
    assert "000001" in md
    assert "completed" in md
    assert "0.5" in md or "0.50" in md
