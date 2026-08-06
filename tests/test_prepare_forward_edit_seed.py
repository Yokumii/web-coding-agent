from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.prepare_forward_edit_seed import prepare_seed


def test_prepare_seed_copies_verified_frontend_with_single_baseline(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text("<main>accepted</main>")
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text(json.dumps({"functional_passed": True}))
    target = tmp_path / "edit_case"

    baseline = prepare_seed(source, target, evaluation)

    assert (target / "frontend" / "index.html").read_text() == "<main>accepted</main>"
    assert json.loads((target / "seed_manifest.json").read_text())["baseline_commit"] == baseline
    subjects = subprocess.run(
        ["git", "log", "--format=%s"], cwd=target / "frontend", text=True,
        check=True, capture_output=True,
    ).stdout.splitlines()
    assert subjects == ["chore: accepted forward-edit baseline"]


def test_prepare_seed_records_external_assets_without_rejecting_reverse_style_source(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text('<script src="https://cdn.example/app.js"></script>')
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text("{}")

    prepare_seed(source, tmp_path / "target", evaluation)

    manifest = json.loads((tmp_path / "target" / "seed_manifest.json").read_text())
    assert manifest["asset_policy"] == "match_reverse_source"
    assert manifest["external_asset_urls"] == ["https://cdn.example/app.js"]
