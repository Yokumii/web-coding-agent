from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from src.bench.manifest import Manifest, SampleRecord


_GRADE_RE = re.compile(r"Grade.*?(\d)", flags=re.IGNORECASE | re.DOTALL)


def parse_first_grade_int(text: str) -> int:
    """Mirror webgen's compute_grade.first_grade_int — first digit after 'Grade'."""
    if not text:
        return 0
    m = _GRADE_RE.search(text)
    return int(m.group(1)) if m else 0


def parse_ui_accuracy(
    extracted_dir: Path,
    *,
    app_id: str,
    sub_count: int,
) -> float | None:
    """Read interact_messages.json per sub-task; YES=1, PARTIAL=0.5, else 0.

    webvoyager names task dirs `task{web_name}` where `web_name` = app_id
    (zero-padded 6-digit string), and tasks_test_with_answer.jsonl gives each
    sub-task id `{app_id}_{sub_idx}` — so the on-disk dir is `task{app_id}_{sub_idx}`.

    Missing sub-task directories count as 0 (matches webgen's "start_failed" semantics).
    Returns None when sub_count == 0.
    """
    if sub_count <= 0:
        return None

    results_dir = extracted_dir / "results"
    score = 0.0
    for sub_idx in range(sub_count):
        task_dir = results_dir / f"task{app_id}_{sub_idx}"
        msg_path = task_dir / "interact_messages.json"
        if not msg_path.is_file():
            continue
        try:
            data = json.loads(msg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        text = ""
        for msg in reversed(data):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                text = msg.get("content") or ""
                break
        if "YES" in text:
            score += 1.0
        elif "PARTIAL" in text:
            score += 0.5
        # "NO" or other → 0
    return score / sub_count


def parse_appearance_grade(
    extracted_dir: Path,
    *,
    app_id: str,
) -> float | None:
    """Find shots/result.json under <extracted>/<app_id>/[wrapper/]shots/result.json.

    `app_id` is webgen's positional identifier (e.g. "000001"), NOT our sample.id.
    See "Critical contract" at top of plan.
    webgen's unzip can produce a wrapper dir (single subdirectory under <app_id>).
    Returns None if not found.
    """
    sample_root = extracted_dir / app_id
    if not sample_root.is_dir():
        return None

    candidates = [sample_root / "shots" / "result.json"]
    # Search one level deeper for wrapped zip layouts
    for child in sample_root.iterdir():
        if child.is_dir():
            candidates.append(child / "shots" / "result.json")

    for path in candidates:
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            return float(parse_first_grade_int(payload.get("model_output", "")))
    return None


def aggregate(
    *,
    manifest: Manifest,
    bench_input_dir: Path,
    sample_raw_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Walk manifest samples, compute per-sample numbers, build summary dict.

    Mutates manifest.samples[*].bench.ui_accuracy / .appearance_grade in place
    so caller can persist via ManifestStore.

    The position of each sample within `manifest.samples` (0-based) plus 1 is
    the webgen "app id" used to name zip files and locate output dirs. See
    "Critical contract" at top of plan.
    """
    extracted = bench_input_dir / "extracted"

    for idx_0based, sample in enumerate(manifest.samples):
        app_id = f"{idx_0based + 1:06d}"
        raw = sample_raw_by_id.get(sample.id, {})
        sub_count = len(raw.get("ui_instruct") or [])
        ui_acc = parse_ui_accuracy(
            extracted, app_id=app_id, sub_count=sub_count,
        )
        app_grade = parse_appearance_grade(extracted, app_id=app_id)

        # Tag missing artifacts as errored
        if ui_acc is None and app_grade is None:
            if sample.bench.status != "errored":
                sample.bench.status = "errored"

        sample.bench.ui_accuracy = ui_acc if ui_acc is not None else 0.0
        sample.bench.appearance_grade = app_grade if app_grade is not None else 1.0
        sample.bench.raw_results_dir = "bench_input/extracted/results"

    return _build_summary(manifest)


def _build_summary(manifest: Manifest) -> dict[str, Any]:
    samples = manifest.samples
    n = len(samples)
    completed = sum(1 for s in samples if s.harness.status == "completed")
    errored = sum(1 for s in samples if s.harness.status == "errored")

    ui_vals = [s.bench.ui_accuracy or 0.0 for s in samples]
    app_vals = [s.bench.appearance_grade or 0.0 for s in samples]
    cost_total = sum((s.harness.cost_usd or 0.0) for s in samples)

    by_verdict_buckets: dict[str, list[SampleRecord]] = defaultdict(list)
    for s in samples:
        key = s.harness.last_verdict or "unknown"
        by_verdict_buckets[key].append(s)

    by_verdict = {
        verdict: {
            "n": len(group),
            "ui_acc_mean": mean([s.bench.ui_accuracy or 0.0 for s in group]) if group else 0.0,
            "app_mean": mean([s.bench.appearance_grade or 0.0 for s in group]) if group else 0.0,
        }
        for verdict, group in by_verdict_buckets.items()
    }

    return {
        "run_id": manifest.run_id,
        "totals": {
            "samples": n,
            "harness_completed": completed,
            "harness_errored": errored,
            "ui_accuracy_mean": mean(ui_vals) if ui_vals else 0.0,
            "appearance_grade_mean": mean(app_vals) if app_vals else 0.0,
            "total_cost_usd": cost_total,
        },
        "by_verdict": by_verdict,
        "samples": [_sample_projection(s) for s in samples],
    }


def _sample_projection(s: SampleRecord) -> dict[str, Any]:
    return {
        "id": s.id,
        "application_type": s.application_type,
        "primary_category": s.primary_category,
        "harness": {
            "status": s.harness.status,
            "last_verdict": s.harness.last_verdict,
            "rounds": s.harness.rounds,
            "cost_usd": s.harness.cost_usd,
            "error": s.harness.error,
        },
        "package": {"status": s.package.status},
        "bench": {
            "status": s.bench.status,
            "ui_accuracy": s.bench.ui_accuracy,
            "appearance_grade": s.bench.appearance_grade,
        },
    }


def render_markdown(summary: dict[str, Any]) -> str:
    totals = summary["totals"]
    lines = [
        f"# WebGen-Bench summary — {summary['run_id']}",
        "",
        "## Totals",
        "",
        f"- samples: {totals['samples']}",
        f"- harness completed / errored: {totals['harness_completed']} / {totals['harness_errored']}",
        f"- ui_accuracy_mean: {totals['ui_accuracy_mean']:.3f}",
        f"- appearance_grade_mean: {totals['appearance_grade_mean']:.2f}",
        f"- total_cost_usd: {totals['total_cost_usd']:.2f}",
        "",
        "## By verdict",
        "",
        "| verdict | n | ui_acc_mean | app_mean |",
        "|---|---|---|---|",
    ]
    for verdict, stats in summary["by_verdict"].items():
        lines.append(
            f"| {verdict} | {stats['n']} | "
            f"{stats['ui_acc_mean']:.3f} | {stats['app_mean']:.2f} |"
        )
    lines += [
        "",
        "## Per sample",
        "",
        "| id | verdict | rounds | cost_usd | ui_acc | app_grade |",
        "|---|---|---|---|---|---|",
    ]
    for s in summary["samples"]:
        h = s["harness"]; b = s["bench"]
        lines.append(
            f"| {s['id']} | {h.get('last_verdict') or '-'} | "
            f"{h.get('rounds') if h.get('rounds') is not None else '-'} | "
            f"{(h.get('cost_usd') or 0.0):.2f} | "
            f"{(b.get('ui_accuracy') or 0.0):.3f} | "
            f"{(b.get('appearance_grade') or 0.0):.2f} |"
        )
    return "\n".join(lines) + "\n"
