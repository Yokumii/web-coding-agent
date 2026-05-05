from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.bench.sampler import (
    Sample,
    filter_by_ids,
    load_jsonl,
    stratified_sample,
    take_first_n,
    write_jsonl,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _row(idx: str, app_type: str, primary: str = "Data Management") -> dict:
    return {
        "id": idx,
        "instruction": f"build {idx}",
        "Category": {"primary_category": primary, "subcategories": []},
        "application_type": app_type,
        "ui_instruct": [],
    }


def test_load_jsonl_parses_required_fields(tmp_path: Path) -> None:
    src = tmp_path / "test.jsonl"
    _write_jsonl(src, [_row("000001", "Dashboard"), _row("000002", "Form")])

    samples = load_jsonl(src)

    assert [s.id for s in samples] == ["000001", "000002"]
    assert samples[0].instruction == "build 000001"
    assert samples[0].application_type == "Dashboard"
    assert samples[0].primary_category == "Data Management"
    assert samples[0].raw["id"] == "000001"


def test_stratified_sample_takes_per_stratum_uniform_seed(tmp_path: Path) -> None:
    rows = [_row(f"00000{i}", "A") for i in range(1, 4)]
    rows += [_row(f"00001{i}", "B") for i in range(0, 3)]
    samples = [Sample.from_row(r) for r in rows]

    picked_a = stratified_sample(samples, "application_type", per_stratum=1, seed=42)
    picked_b = stratified_sample(samples, "application_type", per_stratum=1, seed=42)

    assert {s.application_type for s in picked_a} == {"A", "B"}
    assert len(picked_a) == 2
    assert [s.id for s in picked_a] == [s.id for s in picked_b]  # deterministic


def test_stratified_sample_handles_undersized_strata(tmp_path: Path) -> None:
    rows = [_row("000001", "A"), _row("000002", "B"), _row("000003", "B")]
    samples = [Sample.from_row(r) for r in rows]

    picked = stratified_sample(samples, "application_type", per_stratum=5, seed=42)

    # A has only 1, B has only 2 — take all of them
    assert sorted(s.id for s in picked) == ["000001", "000002", "000003"]


def test_stratified_sample_by_primary_category(tmp_path: Path) -> None:
    rows = [
        _row("000001", "X", primary="Data Management"),
        _row("000002", "Y", primary="User Interaction"),
    ]
    samples = [Sample.from_row(r) for r in rows]

    picked = stratified_sample(samples, "primary_category", per_stratum=1, seed=42)

    assert {s.primary_category for s in picked} == {"Data Management", "User Interaction"}


def test_filter_by_ids_preserves_order_and_skips_missing() -> None:
    rows = [_row(i, "A") for i in ["000001", "000002", "000003"]]
    samples = [Sample.from_row(r) for r in rows]

    picked = filter_by_ids(samples, ["000003", "999999", "000001"])

    assert [s.id for s in picked] == ["000003", "000001"]


def test_take_first_n_returns_prefix() -> None:
    samples = [Sample.from_row(_row(f"00000{i}", "A")) for i in range(1, 6)]
    assert [s.id for s in take_first_n(samples, 3)] == ["000001", "000002", "000003"]


def test_write_jsonl_round_trip(tmp_path: Path) -> None:
    samples = [Sample.from_row(_row("000001", "A")), Sample.from_row(_row("000002", "B"))]
    out = tmp_path / "sampled.jsonl"

    write_jsonl(samples, out)
    reloaded = load_jsonl(out)

    assert [s.id for s in reloaded] == ["000001", "000002"]
    assert reloaded[0].application_type == "A"


def test_stratified_sample_unknown_strata_raises() -> None:
    samples = [Sample.from_row(_row("000001", "A"))]
    with pytest.raises(ValueError):
        stratified_sample(samples, "nonsense_field", per_stratum=1, seed=42)
