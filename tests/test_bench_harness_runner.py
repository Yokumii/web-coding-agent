from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.bench.harness_runner import HarnessSubprocessResult, run_harness_for_sample
from src.bench.manifest import HarnessRecord, SampleRecord


def _record(idx: str = "000001") -> SampleRecord:
    return SampleRecord(
        id=idx,
        instruction=f"build {idx}",
        harness=HarnessRecord(workdir=f"samples/{idx}"),
    )


def _write_state(workdir: Path, *, last_verdict: str = "completed",
                 round_num: int = 3, costs: dict | None = None) -> None:
    harness_dir = workdir / ".harness"
    harness_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "last_completed_phase": f"evaluate_r{round_num}",
        "round_num": round_num,
        "prompt": "test",
        "costs": costs or {"planner": 1.0, "generator_r1": 4.5},
        "last_verdict": last_verdict,
        "timestamp": "2026-05-05T00:00:00",
    }
    (harness_dir / "harness_state.json").write_text(json.dumps(state))


def test_run_harness_for_sample_success(tmp_path: Path, monkeypatch) -> None:
    record = _record("000001")
    run_dir = tmp_path / "runs" / "r1"
    workdir = run_dir / "samples" / "000001"
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        # harness writes its state file
        wd = Path(cmd[cmd.index("--workdir") + 1])
        _write_state(wd, last_verdict="completed", round_num=2,
                     costs={"planner": 1.0, "generator_r1": 3.0})
        return HarnessSubprocessResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("src.bench.harness_runner._invoke", fake_run)

    result = run_harness_for_sample(
        record,
        run_dir=run_dir,
        project_root=tmp_path,
        extra_args=["--max-rounds", "3"],
        log_dir=log_dir,
    )

    assert result.status == "completed"
    assert result.last_verdict == "completed"
    assert result.rounds == 2
    assert result.cost_usd == pytest.approx(4.0)
    assert result.error is None


def test_run_harness_for_sample_subprocess_failure(tmp_path: Path, monkeypatch) -> None:
    record = _record("000002")
    run_dir = tmp_path / "runs" / "r1"
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True)

    def fake_run(cmd, **kwargs):
        return HarnessSubprocessResult(returncode=1, stdout="", stderr="boom\n")

    monkeypatch.setattr("src.bench.harness_runner._invoke", fake_run)

    result = run_harness_for_sample(
        record,
        run_dir=run_dir,
        project_root=tmp_path,
        extra_args=[],
        log_dir=log_dir,
    )

    assert result.status == "errored"
    assert result.error and "boom" in result.error
    assert result.last_verdict is None  # no state file


def test_run_harness_for_sample_missing_state(tmp_path: Path, monkeypatch) -> None:
    """Subprocess returns 0 but harness_state.json never appeared."""
    record = _record("000003")
    run_dir = tmp_path / "runs" / "r1"
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "src.bench.harness_runner._invoke",
        lambda cmd, **kw: HarnessSubprocessResult(returncode=0, stdout="", stderr=""),
    )

    result = run_harness_for_sample(
        record,
        run_dir=run_dir,
        project_root=tmp_path,
        extra_args=[],
        log_dir=log_dir,
    )

    assert result.status == "errored"
    assert result.error and "harness_state.json" in result.error


def test_run_harness_for_sample_log_path_captured(tmp_path: Path, monkeypatch) -> None:
    record = _record("000004")
    run_dir = tmp_path / "runs" / "r1"
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True)

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["log_path"] = kwargs.get("log_path")
        captured["cmd"] = list(cmd)
        wd = Path(cmd[cmd.index("--workdir") + 1])
        _write_state(wd)
        return HarnessSubprocessResult(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("src.bench.harness_runner._invoke", fake_run)

    run_harness_for_sample(
        record,
        run_dir=run_dir,
        project_root=tmp_path,
        extra_args=["--max-rounds", "3", "--max-budget", "30"],
        log_dir=log_dir,
    )

    assert captured["log_path"] == log_dir / "harness_000004.log"
    # extra_args appear after the prompt + --workdir
    assert captured["cmd"][-4:] == ["--workdir", str(run_dir / "samples" / "000004"),
                                    "--max-rounds", "3"] or "--max-budget" in captured["cmd"]
