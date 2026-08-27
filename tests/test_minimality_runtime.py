from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from src.orchestration.minimality_runtime import (
    browser_target_outcome,
    certify_commit_pair,
    certify_round_minimality,
    record_round_build_destination,
    record_round_build_source,
)
from src.config import HarnessConfig


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
    assert payload["1"]["trajectory_role"] == "generate_root"


def test_later_generate_sprint_gets_an_edit_certificate(monkeypatch, tmp_path: Path):
    harness = tmp_path / ".harness"
    harness.mkdir()
    (harness / "minimality_policy.json").write_text(json.dumps({
        "enabled": True, "max_atomic_changes": 12,
    }))
    (harness / "round_build_map.json").write_text(json.dumps({
        "2": {
            "round": 2, "sprint": 2, "mode": "generate",
            "trajectory_role": "incremental_edit",
            "source_commit": "accepted-sprint-one",
            "destination_commit": "accepted-sprint-two",
        }
    }))
    (harness / "edit_dom_source_sprint_2.json").write_text(json.dumps({
        "version": 3, "routes": ["/"], "roots": [],
    }))

    calls: list[dict] = []

    async def fake_certify_commit_pair(**kwargs):
        calls.append(kwargs)
        return {"status": "certified"}

    monkeypatch.setattr(
        "src.orchestration.minimality_runtime.certify_commit_pair",
        fake_certify_commit_pair,
    )

    result = asyncio.run(certify_round_minimality(
        run_dir=tmp_path,
        config=HarnessConfig(),
        round_num=2,
        sprint_num=2,
        checks=[],
    ))

    assert result == {
        "status": "ok", "certificates": {"edit": {"status": "certified"}}
    }
    assert calls[0]["kind"] == "edit"
    assert calls[0]["source_commit"] == "accepted-sprint-one"
    assert calls[0]["destination_commit"] == "accepted-sprint-two"


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


def test_visual_review_covers_style_atom_missing_from_functional_oracle(
    monkeypatch, tmp_path: Path
):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test")
    _git(frontend, "config", "user.email", "test@example.com")
    (frontend / "index.html").write_text('<link rel="stylesheet" href="page.css">')
    _git(frontend, "add", "index.html")
    _git(frontend, "commit", "-m", "chore: baseline")
    source = _git(frontend, "rev-parse", "HEAD")
    (frontend / "page.css").write_text("main { display: grid; }\n")
    _git(frontend, "add", "page.css")
    _git(frontend, "commit", "-m", "feat: style page")
    destination = _git(frontend, "rev-parse", "HEAD")

    async def fake_certify(patches, oracle, *, max_atoms):
        del oracle, max_atoms
        return {
            "schema_version": "counterfactual-patch-certificate-v1",
            "status": "non_minimal",
            "reason": "original_candidate_contains_removable_changes",
            "original_change_ids": [patch.change_id for patch in patches],
            "kept_change_ids": [],
            "redundant_change_ids": [patch.change_id for patch in patches],
            "necessity": [],
            "oracle_attempts": [],
        }

    monkeypatch.setattr(
        "src.orchestration.minimality_runtime.certify_patch_minimality",
        fake_certify,
    )

    certificate = asyncio.run(
        certify_commit_pair(
            run_dir=tmp_path,
            config=HarnessConfig(),
            round_num=1,
            kind="edit",
            source_commit=source,
            destination_commit=destination,
            checks=[{"id": "UI-1", "category": "visual", "actions": []}],
            baseline=None,
            scope=None,
            max_atoms=4,
            visual_accepted=True,
        )
    )

    assert certificate["status"] == "certified"
    assert certificate["visual_role_evidence"]["style_change_ids"] == ["p001"]
    assert certificate["necessity"][0]["failure_dimension"] == (
        "accepted_visual_contract_source_role"
    )
