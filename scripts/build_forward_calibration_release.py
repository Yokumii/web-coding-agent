#!/usr/bin/env python3
"""Build one immutable, audited v2 release from eligible forward trajectories.

The per-run exporter remains the authority for browser, scope, patch-replay, and
provenance gates.  This utility only aggregates records it declares eligible; it
never reinterprets failed runs as data and refuses to overwrite a release.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from export_trajectory_dataset import export_run, to_v2_records


def build_release(runs_root: Path, output_dir: Path) -> dict[str, Any]:
    if not runs_root.is_dir():
        raise ValueError(f"runs root does not exist: {runs_root}")
    if output_dir.exists():
        raise ValueError(f"refusing to overwrite existing release: {output_dir}")

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for run_dir in sorted(path for path in runs_root.glob("edit_*") if path.is_dir()):
        try:
            records = export_run(run_dir)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            rejected.append({"run": run_dir.name, "reason": f"export_error: {exc}"})
            continue
        selected = [
            record for record in records
            if record.get("task") in {"text-editing", "text-repair"}
            and record.get("quality", {}).get("tier") == "benchmark_aligned"
        ]
        if not selected:
            rejected.append({"run": run_dir.name, "reason": "no_benchmark_aligned_record"})
            continue
        for record in selected:
            instance_id = str(record.get("instance_id", ""))
            if not instance_id or instance_id in seen_ids:
                raise ValueError(f"duplicate or missing instance_id: {instance_id!r}")
            seen_ids.add(instance_id)
            accepted.append(record)

    output_dir.mkdir(parents=True)
    records_path = output_dir / "records.jsonl"
    records_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in accepted),
        encoding="utf-8",
    )
    v2_dir = output_dir / "v2"
    v2_dir.mkdir()
    v2 = to_v2_records(accepted)
    for name, rows in v2.items():
        (v2_dir / f"{name}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
    manifest = {
        "schema": "forward-calibration-release-v1",
        "runs_root": str(runs_root),
        "record_count": len(accepted),
        "counts": {
            task: sum(record["task"] == task for record in accepted)
            for task in ("text-editing", "text-repair")
        },
        "accepted_instance_ids": [record["instance_id"] for record in accepted],
        "rejected_runs": rejected,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build_release(args.runs_root, args.output_dir)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
