from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.bench.cli import build_parser, run_webgen


def _make_test_jsonl(path: Path, ids: list[str]) -> None:
    rows = []
    for idx in ids:
        rows.append({
            "id": idx,
            "instruction": f"build {idx}",
            "Category": {"primary_category": "Data Management"},
            "application_type": "Dashboard",
            "ui_instruct": [{"task": "x", "expected_result": "y", "task_category": {}}],
        })
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_parser_accepts_minimum_args() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "webgen",
        "--jsonl", "test.jsonl",
        "--runs-dir", "runs/r1",
    ])
    assert args.subcommand == "webgen"
    assert args.jsonl == "test.jsonl"
    assert args.runs_dir == "runs/r1"
    assert args.skip_harness is False
    assert args.resume is False


def test_parser_accepts_strategy_flags() -> None:
    parser = build_parser()
    args = parser.parse_args([
        "webgen",
        "--jsonl", "t.jsonl",
        "--runs-dir", "runs/r1",
        "--strata", "primary_category",
        "--per-stratum", "2",
        "--seed", "7",
        "--skip-harness",
        "--resume",
        "--harness-args", "--max-rounds 3 --max-budget 30",
    ])
    assert args.strata == "primary_category"
    assert args.per_stratum == 2
    assert args.seed == 7
    assert args.skip_harness is True
    assert args.resume is True
    assert args.harness_args == "--max-rounds 3 --max-budget 30"


def test_run_webgen_end_to_end_with_mocks(tmp_path: Path, monkeypatch) -> None:
    """End-to-end happy path with all subprocesses stubbed."""
    jsonl = tmp_path / "test.jsonl"
    _make_test_jsonl(jsonl, ["000001", "000002"])
    runs_dir = tmp_path / "runs" / "r1"

    # Stub harness_runner: pretend each sample produced a frontend/.harness
    def fake_run_harness(sample, *, run_dir, project_root, extra_args, log_dir):
        from src.bench.manifest import HarnessRecord
        sub = run_dir / "samples" / sample.id
        (sub / "frontend").mkdir(parents=True)
        (sub / "frontend" / "package.json").write_text(
            json.dumps({"scripts": {"dev": "vite"}})
        )
        (sub / "frontend" / "package-lock.json").write_text("")
        return HarnessRecord(
            status="completed", workdir=f"samples/{sample.id}",
            last_verdict="completed", rounds=2, cost_usd=5.0,
        )

    monkeypatch.setattr("src.bench.cli.run_harness_for_sample", fake_run_harness)

    # Stub bench_runner: return 0, write fake raw artifacts
    def fake_ui_eval(*, webgen_dir, in_dir, test_file, env, log_path):
        extracted = Path(in_dir) / "extracted"
        for sample_idx in (1, 2):
            d = extracted / "results" / f"task_{sample_idx}_0"
            d.mkdir(parents=True)
            (d / "interact_messages.json").write_text(json.dumps([
                {"role": "assistant", "content": "YES"},
            ]))
        return 0

    def fake_eval_appearance(*, webgen_dir, in_dir, test_file, env, log_path):
        extracted = Path(in_dir) / "extracted"
        for sample_id in ("000001", "000002"):
            d = extracted / sample_id / "shots"
            d.mkdir(parents=True)
            (d / "result.json").write_text(json.dumps({"model_output": "Grade: 4"}))
        return 0

    monkeypatch.setattr("src.bench.cli.run_ui_eval", fake_ui_eval)
    monkeypatch.setattr("src.bench.cli.run_eval_appearance", fake_eval_appearance)
    monkeypatch.setattr("src.bench.cli.ensure_uv_synced", lambda webgen_dir: None)
    monkeypatch.setattr("src.bench.cli.check_dependencies", lambda *a, **kw: [])

    parser = build_parser()
    args = parser.parse_args([
        "webgen",
        "--jsonl", str(jsonl),
        "--runs-dir", str(runs_dir),
        "--all",
    ])
    rc = run_webgen(args, project_root=tmp_path,
                    webgen_dir=tmp_path / "webgen-fake")
    assert rc == 0

    # Manifest written, both samples completed, summary present
    manifest_path = runs_dir / "manifest.json"
    assert manifest_path.is_file()
    raw = json.loads(manifest_path.read_text())
    assert len(raw["samples"]) == 2
    assert all(s["harness"]["status"] == "completed" for s in raw["samples"])
    assert all(s["package"]["status"] == "done" for s in raw["samples"])
    assert all(s["bench"]["ui_accuracy"] == 1.0 for s in raw["samples"])
    assert all(s["bench"]["appearance_grade"] == 4.0 for s in raw["samples"])

    summary_path = runs_dir / "summary.json"
    assert summary_path.is_file()
    summary = json.loads(summary_path.read_text())
    assert summary["totals"]["samples"] == 2
    assert summary["totals"]["ui_accuracy_mean"] == 1.0
    assert (runs_dir / "summary.md").is_file()


def test_run_webgen_zip_named_by_position_not_sample_id(tmp_path: Path, monkeypatch) -> None:
    """webgen requires app id = jsonl row number, not original sample id."""
    jsonl = tmp_path / "test.jsonl"
    # Non-contiguous sample ids
    _make_test_jsonl(jsonl, ["000005", "000023"])
    runs_dir = tmp_path / "runs" / "r1"

    def fake_run_harness(sample, *, run_dir, project_root, extra_args, log_dir):
        from src.bench.manifest import HarnessRecord
        sub = run_dir / "samples" / sample.id
        (sub / "frontend").mkdir(parents=True)
        (sub / "frontend" / "package.json").write_text(
            json.dumps({"scripts": {"dev": "vite"}})
        )
        return HarnessRecord(status="completed", workdir=f"samples/{sample.id}",
                             last_verdict="completed", rounds=1, cost_usd=1.0)

    monkeypatch.setattr("src.bench.cli.run_harness_for_sample", fake_run_harness)
    monkeypatch.setattr("src.bench.cli.run_ui_eval", lambda **kw: 0)
    monkeypatch.setattr("src.bench.cli.run_eval_appearance", lambda **kw: 0)
    monkeypatch.setattr("src.bench.cli.ensure_uv_synced", lambda webgen_dir: None)
    monkeypatch.setattr("src.bench.cli.check_dependencies", lambda *a, **kw: [])

    parser = build_parser()
    args = parser.parse_args([
        "webgen", "--jsonl", str(jsonl), "--runs-dir", str(runs_dir), "--all",
    ])
    rc = run_webgen(args, project_root=tmp_path,
                    webgen_dir=tmp_path / "webgen-fake")
    assert rc == 0

    bench_input = runs_dir / "bench_input"
    # Zip files named by 1-based row index, not sample.id
    assert (bench_input / "000001.zip").is_file()
    assert (bench_input / "000002.zip").is_file()
    assert not (bench_input / "000005.zip").exists()
    assert not (bench_input / "000023.zip").exists()
    # Chat json mirrors the same naming
    assert (bench_input / "000001.json").is_file()
    assert (bench_input / "000002.json").is_file()
    # _meta still records the original sample id for traceability
    payload = json.loads((bench_input / "000001.json").read_text())
    assert payload["_meta"]["sample_id"] == "000005"
    assert payload["_meta"]["app_id"] == "000001"


def test_run_webgen_resume_skips_completed(tmp_path: Path, monkeypatch) -> None:
    jsonl = tmp_path / "test.jsonl"
    _make_test_jsonl(jsonl, ["000001", "000002"])
    runs_dir = tmp_path / "runs" / "r1"

    # Pre-seed manifest: 000001 completed
    from src.bench.manifest import (
        BenchRecord, HarnessRecord, ManifestStore, PackageRecord,
        SampleRecord, new_manifest,
    )
    from src.bench.sampler import load_jsonl

    samples = load_jsonl(jsonl)
    manifest = new_manifest(run_id="r1", jsonl_source=str(jsonl),
                            strata=None, samples=samples)
    manifest.samples[0].harness = HarnessRecord(
        status="completed", workdir="samples/000001",
        last_verdict="completed", rounds=1, cost_usd=2.0,
    )
    runs_dir.mkdir(parents=True)
    ManifestStore(runs_dir / "manifest.json").save(manifest)
    # Pre-create completed sample's frontend
    (runs_dir / "samples" / "000001" / "frontend").mkdir(parents=True)
    (runs_dir / "samples" / "000001" / "frontend" / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}})
    )
    (runs_dir / "sampled.jsonl").write_text((tmp_path / "test.jsonl").read_text())

    calls = {"runs": []}

    def fake_run_harness(sample, **kw):
        from src.bench.manifest import HarnessRecord
        calls["runs"].append(sample.id)
        sub = kw["run_dir"] / "samples" / sample.id
        (sub / "frontend").mkdir(parents=True, exist_ok=True)
        (sub / "frontend" / "package.json").write_text(
            json.dumps({"scripts": {"dev": "vite"}})
        )
        return HarnessRecord(status="completed", workdir=f"samples/{sample.id}",
                             last_verdict="completed", rounds=1, cost_usd=3.0)

    monkeypatch.setattr("src.bench.cli.run_harness_for_sample", fake_run_harness)
    monkeypatch.setattr("src.bench.cli.run_ui_eval",
                        lambda **kw: 0)
    monkeypatch.setattr("src.bench.cli.run_eval_appearance",
                        lambda **kw: 0)
    monkeypatch.setattr("src.bench.cli.ensure_uv_synced", lambda webgen_dir: None)
    monkeypatch.setattr("src.bench.cli.check_dependencies", lambda *a, **kw: [])

    parser = build_parser()
    args = parser.parse_args([
        "webgen", "--jsonl", str(jsonl), "--runs-dir", str(runs_dir),
        "--all", "--resume",
    ])
    rc = run_webgen(args, project_root=tmp_path,
                    webgen_dir=tmp_path / "webgen-fake")
    assert rc == 0

    # Only 000002 was re-run
    assert calls["runs"] == ["000002"]


def test_run_webgen_skip_harness_only_packages_and_evaluates(tmp_path: Path, monkeypatch) -> None:
    jsonl = tmp_path / "test.jsonl"
    _make_test_jsonl(jsonl, ["000001"])
    runs_dir = tmp_path / "runs" / "r1"

    # Pre-seed: frontend exists but no manifest yet
    fe = runs_dir / "samples" / "000001" / "frontend"
    fe.mkdir(parents=True)
    (fe / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))

    calls = {"harness": 0, "ui": 0, "app": 0}
    monkeypatch.setattr("src.bench.cli.run_harness_for_sample",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not run")))
    monkeypatch.setattr("src.bench.cli.run_ui_eval",
                        lambda **kw: (calls.__setitem__("ui", calls["ui"] + 1) or 0))
    monkeypatch.setattr("src.bench.cli.run_eval_appearance",
                        lambda **kw: (calls.__setitem__("app", calls["app"] + 1) or 0))
    monkeypatch.setattr("src.bench.cli.ensure_uv_synced", lambda webgen_dir: None)
    monkeypatch.setattr("src.bench.cli.check_dependencies", lambda *a, **kw: [])

    parser = build_parser()
    args = parser.parse_args([
        "webgen", "--jsonl", str(jsonl), "--runs-dir", str(runs_dir),
        "--all", "--skip-harness",
    ])
    rc = run_webgen(args, project_root=tmp_path,
                    webgen_dir=tmp_path / "webgen-fake")
    assert rc == 0
    assert calls["ui"] == 1 and calls["app"] == 1


def test_run_webgen_aborts_on_missing_dependency(tmp_path: Path, monkeypatch) -> None:
    jsonl = tmp_path / "test.jsonl"
    _make_test_jsonl(jsonl, ["000001"])

    monkeypatch.setattr("src.bench.cli.check_dependencies",
                        lambda *a, **kw: ["pm2"])

    parser = build_parser()
    args = parser.parse_args([
        "webgen", "--jsonl", str(jsonl),
        "--runs-dir", str(tmp_path / "runs" / "r1"), "--all",
    ])
    rc = run_webgen(args, project_root=tmp_path,
                    webgen_dir=tmp_path / "webgen-fake")
    assert rc != 0
