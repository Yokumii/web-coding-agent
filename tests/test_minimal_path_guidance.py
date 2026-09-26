from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.orchestration.minimal_path_guidance import (
    EditScopeState,
    ensure_minimal_path_plan,
    post_edit_scope_sanity,
)


def _project(tmp_path: Path) -> tuple[Path, Path]:
    frontend = tmp_path / "frontend"
    harness = tmp_path / ".harness"
    frontend.mkdir()
    harness.mkdir()
    (frontend / "index.html").write_text(
        '<main id="target"></main><script src="app.js"></script>', encoding="utf-8"
    )
    (frontend / "app.js").write_text(
        "document.querySelector('#target').textContent = 'before';\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=frontend, check=True)
    subprocess.run(["git", "add", "."], cwd=frontend, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "seed"],
        cwd=frontend,
        check=True,
    )
    (harness / "ui_verification_plan.json").write_text(json.dumps({
        "sprints": [{"sprint": 1, "checks": [{
            "id": "target", "route": "/", "category": "functionality",
            "actions": [{"action": "assert_visible", "selector": "#target"}],
        }]}]
    }))
    return frontend, harness


def test_recommended_scope_is_advisory_and_records_observed_dependencies(tmp_path: Path):
    _frontend, harness = _project(tmp_path)
    plan = ensure_minimal_path_plan(
        workdir=tmp_path, harness_dir=harness, round_num=1, sprint_num=1,
        mode="generate", max_patch_lines=20, max_touched_files=2,
    )
    state = EditScopeState(tmp_path, plan)

    assert plan["scope_mode"] == "recommended"
    assert state.check("apply_patch", {"path": "frontend/unplanned.js"}) is None
    state.observe_result(
        "apply_patch", {"path": "frontend/unplanned.js"}, ok=True, output="patched"
    )
    persisted = json.loads((harness / "recommended_scope_state_round_1.json").read_text())
    assert persisted["touched_paths"] == ["frontend/unplanned.js"]


def test_repair_scope_remains_advisory(tmp_path: Path):
    _frontend, harness = _project(tmp_path)
    plan = ensure_minimal_path_plan(
        workdir=tmp_path, harness_dir=harness, round_num=2, sprint_num=1,
        mode="repair", max_patch_lines=20, max_touched_files=2,
    )
    state = EditScopeState(tmp_path, plan)

    assert state.check("apply_patch", {"path": "frontend/index.html"}) is None
    assert state.validate_guarded_shared_file(
        "frontend/app.js", before="old", after="new"
    ) is None


def test_scope_sanity_flags_only_extreme_rewrite(tmp_path: Path):
    frontend, harness = _project(tmp_path)
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, check=True, capture_output=True, text=True
    ).stdout.strip()
    (harness / "round_build_map.json").write_text(json.dumps({"1": {"source_commit": baseline}}))
    plan = ensure_minimal_path_plan(
        workdir=tmp_path, harness_dir=harness, round_num=1, sprint_num=1,
        mode="generate", max_patch_lines=5, max_touched_files=1,
    )
    for index in range(4):
        (frontend / f"extra-{index}.js").write_text("\n".join(f"const v{line}={line};" for line in range(30)))
    subprocess.run(["git", "add", "."], cwd=frontend, check=True)

    result = post_edit_scope_sanity(
        workdir=tmp_path, harness_dir=harness, round_num=1, plan=plan
    )

    assert result["status"] == "issue"
    assert any("无关文件" in issue or "重写" in issue for issue in result["issues"])
