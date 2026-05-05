from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

from src.bench.aggregator import aggregate, render_markdown
from src.bench.bench_runner import (
    ensure_uv_synced,
    map_vlm_env,
    run_eval_appearance,
    run_ui_eval,
)
from src.bench.harness_runner import run_harness_for_sample
from src.bench.manifest import (
    BenchRecord,
    HarnessRecord,
    ManifestStore,
    PackageRecord,
    new_manifest,
)
from src.bench.packager import package_frontend
from src.bench.sampler import (
    Sample,
    filter_by_ids,
    load_jsonl,
    stratified_sample,
    take_first_n,
    write_jsonl,
)


PROJECT_ROOT_DEFAULT = Path(__file__).resolve().parents[2]
WEBGEN_DIR_DEFAULT = (
    PROJECT_ROOT_DEFAULT / "awesome-web-bench" / "webgen-bench" / "WebGen-Bench"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness-bench",
        description="Run harness against benchmark suites",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    webgen = sub.add_parser("webgen", help="Run harness over WebGen-Bench")
    webgen.add_argument("--jsonl", required=True, help="Path to test.jsonl")
    webgen.add_argument("--runs-dir", required=True, help="Output run directory")

    sample_group = webgen.add_mutually_exclusive_group()
    sample_group.add_argument("--ids", help="Comma-separated sample ids to run")
    sample_group.add_argument("--limit", type=int, help="Take first N samples")
    sample_group.add_argument("--all", action="store_true",
                              help="Run every sample in --jsonl")

    webgen.add_argument("--strata", default="application_type",
                        choices=["application_type", "primary_category"])
    webgen.add_argument("--per-stratum", type=int, default=1)
    webgen.add_argument("--seed", type=int, default=42)

    webgen.add_argument("--skip-harness", action="store_true",
                        help="Treat all samples as harness-done; require frontend exists")
    webgen.add_argument("--resume", action="store_true",
                        help="Reuse existing manifest; skip already-completed harness samples")

    webgen.add_argument("--harness-args", default="",
                        help='Extra args forwarded to harness CLI, e.g. "--max-rounds 3"')

    webgen.add_argument("--vlm-api-key")
    webgen.add_argument("--vlm-base-url")
    webgen.add_argument("--vlm-model")

    webgen.add_argument("--webgen-dir", default=str(WEBGEN_DIR_DEFAULT),
                        help="Path to WebGen-Bench checkout")

    return parser


def cli() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(run_webgen(args, project_root=PROJECT_ROOT_DEFAULT,
                        webgen_dir=Path(args.webgen_dir)))


def check_dependencies(webgen_dir: Path) -> list[str]:
    """Return list of missing dependencies. Empty list = OK."""
    missing: list[str] = []
    if shutil.which("node") is None:
        missing.append("node")
    if shutil.which("pm2") is None and shutil.which("npx") is None:
        missing.append("pm2 (or npx for `npx pm2`)")
    if shutil.which("uv") is None:
        missing.append("uv")
    if sys.platform == "darwin" and shutil.which("lsof") is None:
        missing.append("lsof (macOS — harness/runtime.py needs it)")
    if not (webgen_dir / "uv.lock").is_file():
        missing.append(f"WebGen-Bench checkout at {webgen_dir}")
    return missing


def _validate_vlm_endpoint(args: argparse.Namespace) -> str | None:
    """Return error message if VLM config will not work with webgen, else None.

    webgen requires an OpenAI-compatible chat completions endpoint. If harness's
    EVALUATOR_VISION_ENDPOINT_TYPE is set to anything other than 'openai' (default
    'anthropic') and the user hasn't supplied --vlm-base-url override, abort.
    """
    if args.vlm_base_url:
        return None
    endpoint_type = os.environ.get(
        "EVALUATOR_VISION_ENDPOINT_TYPE", "anthropic"
    ).strip().lower()
    if endpoint_type and endpoint_type != "openai":
        return (
            f"EVALUATOR_VISION_ENDPOINT_TYPE={endpoint_type!r} is not "
            "OpenAI-compatible. webgen requires an OpenAI-compatible chat "
            "completions endpoint. Pass --vlm-base-url (and possibly "
            "--vlm-api-key/--vlm-model) explicitly."
        )
    return None


def _ensure_pm2_log_dir() -> None:
    """Auto-create ~/.pm2/logs (pm2 fails if the directory is missing)."""
    Path("~/.pm2/logs").expanduser().mkdir(parents=True, exist_ok=True)


def run_webgen(
    args: argparse.Namespace,
    *,
    project_root: Path,
    webgen_dir: Path,
) -> int:
    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    log_dir = runs_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    bench_input_dir = runs_dir / "bench_input"
    bench_input_dir.mkdir(exist_ok=True)

    missing = check_dependencies(webgen_dir)
    if missing:
        print(f"[harness-bench] missing dependencies: {', '.join(missing)}",
              file=sys.stderr)
        return 2

    vlm_err = _validate_vlm_endpoint(args)
    if vlm_err is not None:
        print(f"[harness-bench] {vlm_err}", file=sys.stderr)
        return 2

    _ensure_pm2_log_dir()

    manifest_path = runs_dir / "manifest.json"
    sampled_path = runs_dir / "sampled.jsonl"
    store = ManifestStore(manifest_path)

    if args.resume:
        if not manifest_path.is_file():
            print(
                f"[harness-bench] --resume given but no manifest at {manifest_path}",
                file=sys.stderr,
            )
            return 2
        manifest = store.load()
        store.reset_running_to_pending(manifest)
        sample_raws = _load_raw_index(sampled_path)
        if not sample_raws:
            print(
                f"[harness-bench] --resume: sampled.jsonl missing or empty at {sampled_path}",
                file=sys.stderr,
            )
            return 2
    else:
        all_samples = load_jsonl(Path(args.jsonl))
        picked = _pick_samples(all_samples, args)
        sample_raws = {s.id: s.raw for s in picked}
        write_jsonl(picked, sampled_path)

        manifest = new_manifest(
            run_id=f"webgen-{datetime.now().strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:6]}",
            jsonl_source=str(Path(args.jsonl).resolve()),
            strata=args.strata,
            samples=picked,
        )
        store.save(manifest)

    extra_args = shlex.split(args.harness_args) if args.harness_args else []

    if not args.skip_harness:
        for sample in list(manifest.samples):
            if sample.harness.status == "completed":
                continue
            sample.harness.status = "running"
            store.save(manifest)

            record = run_harness_for_sample(
                sample,
                run_dir=runs_dir,
                project_root=project_root,
                extra_args=extra_args,
                log_dir=log_dir,
            )
            sample.harness = record
            store.save(manifest)
    else:
        for sample in manifest.samples:
            sample.harness.status = "completed"
        store.save(manifest)

    for idx_0based, sample in enumerate(manifest.samples):
        if sample.package.status == "done":
            continue
        # webgen requires zip filenames keyed by 1-based jsonl row, NOT sample.id.
        app_id = f"{idx_0based + 1:06d}"
        frontend_dir = runs_dir / "samples" / sample.id / "frontend"
        if not (frontend_dir / "package.json").is_file():
            sample.package = PackageRecord(
                status="errored",
                error=f"frontend missing or no package.json at {frontend_dir}",
            )
            sample.bench = BenchRecord(status="errored",
                                        ui_accuracy=0.0, appearance_grade=1.0,
                                        error="skipped: package missing")
            store.save(manifest)
            continue

        zip_path = bench_input_dir / f"{app_id}.zip"
        json_path = bench_input_dir / f"{app_id}.json"
        try:
            package_frontend(frontend_dir, zip_path, json_path, meta={
                "harness_run_id": manifest.run_id,
                "harness_verdict": sample.harness.last_verdict,
                "harness_rounds": sample.harness.rounds,
                "harness_cost_usd": sample.harness.cost_usd,
                "sample_id": sample.id,
                "app_id": app_id,
                "generated_by": "harness-bench",
            })
            sample.package = PackageRecord(
                status="done",
                zip_path=str(zip_path.relative_to(runs_dir)),
                json_path=str(json_path.relative_to(runs_dir)),
            )
        except Exception as e:
            sample.package = PackageRecord(status="errored", error=str(e))
        store.save(manifest)

    runnable = [s for s in manifest.samples if s.package.status == "done"]
    if runnable:
        ensure_uv_synced(webgen_dir)
        env = map_vlm_env(dict(os.environ), overrides={
            "api_key": args.vlm_api_key,
            "base_url": args.vlm_base_url,
            "model": args.vlm_model,
        })

        ui_rc = run_ui_eval(
            webgen_dir=webgen_dir,
            in_dir=bench_input_dir,
            test_file=sampled_path,
            env=env,
            log_path=log_dir / "ui_eval.log",
        )
        app_rc = run_eval_appearance(
            webgen_dir=webgen_dir,
            in_dir=bench_input_dir,
            test_file=sampled_path,
            env=env,
            log_path=log_dir / "eval_appearance.log",
        )

        for sample in runnable:
            if ui_rc != 0 or app_rc != 0:
                sample.bench.status = "errored"
                sample.bench.error = (
                    f"ui_eval rc={ui_rc} eval_appearance rc={app_rc}"
                )
            else:
                sample.bench.status = "done"
        store.save(manifest)

    summary = aggregate(
        manifest=manifest,
        bench_input_dir=bench_input_dir,
        sample_raw_by_id=sample_raws,
    )
    store.save(manifest)

    (runs_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (runs_dir / "summary.md").write_text(render_markdown(summary), encoding="utf-8")

    print(f"[harness-bench] done. summary at {runs_dir / 'summary.md'}")
    return 0


def _pick_samples(all_samples: list[Sample], args: argparse.Namespace) -> list[Sample]:
    if args.ids:
        return filter_by_ids(all_samples, [s.strip() for s in args.ids.split(",") if s.strip()])
    if args.limit:
        return take_first_n(all_samples, args.limit)
    if args.all:
        return list(all_samples)
    return stratified_sample(
        all_samples, strata=args.strata,
        per_stratum=args.per_stratum, seed=args.seed,
    )


def _load_raw_index(sampled_path: Path) -> dict[str, dict]:
    if not sampled_path.is_file():
        return {}
    raws: dict[str, dict] = {}
    with sampled_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            raws[str(row.get("id"))] = row
    return raws
