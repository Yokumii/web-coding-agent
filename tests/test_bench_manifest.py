from __future__ import annotations

import json
from pathlib import Path

from src.bench.manifest import (
    BenchRecord,
    HarnessRecord,
    Manifest,
    ManifestStore,
    PackageRecord,
    SampleRecord,
    new_manifest,
)
from src.bench.sampler import Sample


def _sample(idx: str = "000001") -> Sample:
    return Sample.from_row({
        "id": idx,
        "instruction": f"build {idx}",
        "Category": {"primary_category": "Data Management"},
        "application_type": "Dashboard",
    })


def test_new_manifest_initial_status_pending() -> None:
    manifest = new_manifest(
        run_id="run-1",
        jsonl_source="test.jsonl",
        strata="application_type",
        samples=[_sample("000001"), _sample("000002")],
    )

    assert manifest.run_id == "run-1"
    assert len(manifest.samples) == 2
    assert manifest.samples[0].id == "000001"
    assert manifest.samples[0].harness.status == "pending"
    assert manifest.samples[0].package.status == "pending"
    assert manifest.samples[0].bench.status == "pending"


def test_round_trip_through_disk(tmp_path: Path) -> None:
    manifest = new_manifest(
        run_id="run-1", jsonl_source="t.jsonl", strata=None,
        samples=[_sample("000001")],
    )
    manifest.samples[0].harness.status = "completed"
    manifest.samples[0].harness.last_verdict = "completed"
    manifest.samples[0].harness.cost_usd = 5.5
    manifest.samples[0].bench.ui_accuracy = 0.71

    store = ManifestStore(tmp_path / "manifest.json")
    store.save(manifest)
    reloaded = store.load()

    assert reloaded.samples[0].harness.status == "completed"
    assert reloaded.samples[0].harness.last_verdict == "completed"
    assert reloaded.samples[0].harness.cost_usd == 5.5
    assert reloaded.samples[0].bench.ui_accuracy == 0.71


def test_pending_samples_skips_completed_includes_running_and_errored() -> None:
    manifest = new_manifest(
        run_id="r", jsonl_source="t.jsonl", strata=None,
        samples=[
            _sample("000001"), _sample("000002"),
            _sample("000003"), _sample("000004"),
        ],
    )
    manifest.samples[0].harness.status = "completed"
    manifest.samples[1].harness.status = "running"
    manifest.samples[2].harness.status = "errored"
    # samples[3] stays pending

    pending = manifest.pending_harness_samples()

    # completed skipped; running/errored/pending all retained
    assert [s.id for s in pending] == ["000002", "000003", "000004"]


def test_reset_running_to_pending(tmp_path: Path) -> None:
    manifest = new_manifest(
        run_id="r", jsonl_source="t.jsonl", strata=None,
        samples=[_sample("000001")],
    )
    manifest.samples[0].harness.status = "running"
    store = ManifestStore(tmp_path / "manifest.json")
    store.save(manifest)

    reloaded = store.load()
    store.reset_running_to_pending(reloaded)

    assert reloaded.samples[0].harness.status == "pending"


def test_atomic_save_does_not_truncate_on_crash(tmp_path: Path) -> None:
    # Save once successfully
    manifest = new_manifest(
        run_id="r", jsonl_source="t.jsonl", strata=None,
        samples=[_sample("000001")],
    )
    store = ManifestStore(tmp_path / "manifest.json")
    store.save(manifest)

    # Confirm tmp file does not linger
    tmp_files = list(tmp_path.glob("manifest.json.*.tmp"))
    assert tmp_files == []

    # File parses cleanly
    raw = json.loads((tmp_path / "manifest.json").read_text())
    assert raw["run_id"] == "r"
