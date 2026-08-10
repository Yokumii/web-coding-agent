from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.orchestration.minimality_runtime import (
    browser_target_outcome,
    record_round_build_destination,
    record_round_build_source,
)


def _git(frontend: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=frontend, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_round_build_map_keeps_the_original_source_across_resume(tmp_path: Path):
    frontend = tmp_path / "frontend"
    harness = tmp_path / ".harness"
    frontend.mkdir()
    harness.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test")
    _git(frontend, "config", "user.email", "test@example.com")
    (frontend / "index.html").write_text("before")
    _git(frontend, "add", "index.html")
    _git(frontend, "commit", "-m", "base")
    source = _git(frontend, "rev-parse", "HEAD")

    record_round_build_source(harness, frontend, round_num=1, sprint_num=1, mode="generate")
    (frontend / "index.html").write_text("after")
    _git(frontend, "add", "index.html")
    _git(frontend, "commit", "-m", "feat: edit")
    destination = record_round_build_destination(harness, frontend, round_num=1)
    record_round_build_source(harness, frontend, round_num=1, sprint_num=1, mode="repair")

    payload = json.loads((harness / "round_build_map.json").read_text())
    assert payload["1"]["source_commit"] == source
    assert payload["1"]["destination_commit"] == destination
    assert payload["1"]["mode"] == "generate"


def test_browser_target_outcome_requires_executable_assertions():
    checks = [{"id": "c1", "actions": [{"action": "evaluate", "expression": "true"}]}]
    evidence = {"checks": [{"check_id": "c1", "status": "ok", "steps": [
        {"action": "evaluate", "ok": True, "output": True}
    ]}]}

    outcome = browser_target_outcome(checks, evidence)

    assert outcome.status == "ok"
    assert outcome.target_passed is True


def test_browser_target_outcome_rejects_click_only_contract():
    checks = [{"id": "c1", "actions": [{"action": "click", "selector": "#x"}]}]
    evidence = {"checks": [{"check_id": "c1", "status": "ok", "steps": [
        {"action": "click", "ok": True}
    ]}]}

    outcome = browser_target_outcome(checks, evidence)

    assert outcome.status == "infrastructure_error"
    assert outcome.evidence["reason"] == "target_contract_has_no_assertion"
