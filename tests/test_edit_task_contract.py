from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.orchestration.edit_task_contract import (
    EditTaskContractError,
    prepare_edit_task_contract,
)


def test_explicit_edit_mode_freezes_existing_frontend_without_replacing_it(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "catalog.html").write_text("<main>accepted seed</main>\n")

    contract = prepare_edit_task_contract(
        tmp_path, requested_target_routes=["/catalog.html"]
    )

    assert (frontend / "catalog.html").read_text() == "<main>accepted seed</main>\n"
    assert contract["task_mode"] == "edit"
    assert contract["requested_target_routes"] == ["/catalog.html"]
    seed = json.loads((tmp_path / "seed_manifest.json").read_text())
    assert seed["baseline_commit"] == contract["baseline_commit"]
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=frontend, text=True,
        capture_output=True, check=True,
    ).stdout == ""


def test_explicit_edit_mode_rejects_dirty_seed(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    (frontend / "index.html").write_text("before")
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "seed"],
        cwd=frontend, check=True, capture_output=True,
    )
    (frontend / "index.html").write_text("dirty")

    with pytest.raises(EditTaskContractError, match="clean frontend worktree"):
        prepare_edit_task_contract(tmp_path, requested_target_routes=["/"])


def test_prepared_seed_rejects_head_that_advanced_past_declared_baseline(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    (frontend / "index.html").write_text("before")
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "seed"],
        cwd=frontend, check=True, capture_output=True,
    )
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, text=True,
        check=True, capture_output=True,
    ).stdout.strip()
    (frontend / "index.html").write_text("later")
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "later"],
        cwd=frontend, check=True, capture_output=True,
    )
    (tmp_path / "seed_manifest.json").write_text(json.dumps({"baseline_commit": baseline}))

    with pytest.raises(EditTaskContractError, match="not the current frontend HEAD"):
        prepare_edit_task_contract(tmp_path, requested_target_routes=["/"])
