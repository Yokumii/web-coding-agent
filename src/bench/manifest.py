from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from src.bench.sampler import Sample

HarnessStatus = Literal["pending", "running", "completed", "errored"]
PackageStatus = Literal["pending", "done", "errored"]
BenchStatus = Literal["pending", "done", "errored"]


@dataclass
class HarnessRecord:
    status: HarnessStatus = "pending"
    workdir: str = ""
    last_verdict: str | None = None
    rounds: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class PackageRecord:
    status: PackageStatus = "pending"
    zip_path: str | None = None
    json_path: str | None = None
    error: str | None = None


@dataclass
class BenchRecord:
    status: BenchStatus = "pending"
    ui_accuracy: float | None = None
    appearance_grade: float | None = None
    raw_results_dir: str | None = None
    error: str | None = None


@dataclass
class SampleRecord:
    id: str
    instruction: str
    application_type: str | None = None
    primary_category: str | None = None
    harness: HarnessRecord = field(default_factory=HarnessRecord)
    package: PackageRecord = field(default_factory=PackageRecord)
    bench: BenchRecord = field(default_factory=BenchRecord)


@dataclass
class Manifest:
    run_id: str
    created_at: str
    jsonl_source: str
    strata: str | None
    samples: list[SampleRecord]

    def pending_harness_samples(self) -> list[SampleRecord]:
        return [s for s in self.samples if s.harness.status != "completed"]


def new_manifest(
    *,
    run_id: str,
    jsonl_source: str,
    strata: str | None,
    samples: list[Sample],
) -> Manifest:
    records = [
        SampleRecord(
            id=s.id,
            instruction=s.instruction,
            application_type=s.application_type,
            primary_category=s.primary_category,
            harness=HarnessRecord(workdir=f"samples/{s.id}"),
        )
        for s in samples
    ]
    return Manifest(
        run_id=run_id,
        created_at=datetime.now().isoformat(),
        jsonl_source=jsonl_source,
        strata=strata,
        samples=records,
    )


class ManifestStore:
    """Atomic read/write of manifest.json."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def save(self, manifest: Manifest) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(manifest)
        # Atomic write: tmp file in same dir, then os.replace
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def load(self) -> Manifest:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        samples = [_sample_from_dict(s) for s in raw["samples"]]
        return Manifest(
            run_id=raw["run_id"],
            created_at=raw["created_at"],
            jsonl_source=raw["jsonl_source"],
            strata=raw.get("strata"),
            samples=samples,
        )

    def reset_running_to_pending(self, manifest: Manifest) -> None:
        for sample in manifest.samples:
            if sample.harness.status == "running":
                sample.harness.status = "pending"
        self.save(manifest)


def _sample_from_dict(d: dict[str, Any]) -> SampleRecord:
    return SampleRecord(
        id=d["id"],
        instruction=d.get("instruction", ""),
        application_type=d.get("application_type"),
        primary_category=d.get("primary_category"),
        harness=HarnessRecord(**d.get("harness", {})),
        package=PackageRecord(**d.get("package", {})),
        bench=BenchRecord(**d.get("bench", {})),
    )
