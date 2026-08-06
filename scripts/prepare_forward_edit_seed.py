#!/usr/bin/env python3
"""Create a fresh, immutable accepted baseline for one forward edit case."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path


_EXTERNAL_URL = re.compile(r"https?://[^\s\"'<>)}]+")
_SCAN_SUFFIXES = {".html", ".css", ".json", ".js", ".ts", ".jsx", ".tsx"}


def external_asset_urls(project_dir: Path) -> list[str]:
    """Inventory remote references for provenance; reverse sources may contain them."""
    values: set[str] = set()
    for path in project_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _SCAN_SUFFIXES:
            continue
        for value in _EXTERNAL_URL.findall(path.read_text(errors="ignore")):
            values.add(value.rstrip(",;"))
    return sorted(values)


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=directory, text=True, check=True,
        capture_output=True,
    ).stdout.strip()


def prepare_seed(source_frontend: Path, target_workdir: Path, source_evaluation: Path) -> str:
    """Copy a verified app into a new workdir with exactly one baseline commit."""
    source_frontend = source_frontend.resolve()
    target_workdir = target_workdir.resolve()
    source_evaluation = source_evaluation.resolve()
    if not source_frontend.is_dir():
        raise ValueError(f"source frontend does not exist: {source_frontend}")
    if not source_evaluation.is_file():
        raise ValueError(f"source evaluation does not exist: {source_evaluation}")
    if target_workdir.exists():
        raise ValueError(f"refusing to overwrite existing workdir: {target_workdir}")
    external_urls = external_asset_urls(source_frontend)

    frontend = target_workdir / "frontend"
    shutil.copytree(source_frontend, frontend, ignore=shutil.ignore_patterns(".git"))
    _git(frontend, "init", "-b", "main")
    _git(frontend, "add", "--all")
    _git(frontend, "commit", "-m", "chore: accepted forward-edit baseline")
    baseline = _git(frontend, "rev-parse", "HEAD")
    payload = {
        "status": "ok",
        "source_frontend": str(source_frontend),
        "source_evaluation": str(source_evaluation),
        "baseline_commit": baseline,
        # Do not reject external assets: the reverse-built WebCompass source
        # projects use the same pattern (e.g. fonts and image CDN URLs).  The
        # list makes this environment dependency explicit and replayable.
        "asset_policy": "match_reverse_source",
        "external_asset_urls": external_urls,
    }
    (target_workdir / "seed_manifest.json").write_text(json.dumps(payload, indent=2) + "\n")
    return baseline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-frontend", type=Path, required=True)
    parser.add_argument("--source-evaluation", type=Path, required=True)
    parser.add_argument("--target-workdir", type=Path, required=True)
    args = parser.parse_args()
    baseline = prepare_seed(args.source_frontend, args.target_workdir, args.source_evaluation)
    print(json.dumps({"status": "ok", "baseline_commit": baseline}))


if __name__ == "__main__":
    main()
