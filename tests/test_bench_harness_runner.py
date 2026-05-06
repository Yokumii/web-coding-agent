from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.bench.harness_runner import (
    HarnessSubprocessResult,
    _load_record_from_state,
    arun_harness_for_sample,
    run_harness_for_sample,
)
from src.bench.manifest import HarnessRecord, SampleRecord


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _record(idx: str = "000001") -> SampleRecord:
    return SampleRecord(
        id=idx,
        instruction=f"build {idx}",
        harness=HarnessRecord(workdir=f"samples/{idx}"),
    )


def _write_state(workdir: Path, *, last_verdict: str = "completed",
                 round_num: int = 3, costs: dict | None = None,
                 last_completed_phase: str | None = None) -> None:
    harness_dir = workdir / ".harness"
    harness_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "last_completed_phase": last_completed_phase or f"evaluate_r{round_num}",
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
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True)

    async def fake_ainvoke(cmd, **kwargs):
        wd = Path(cmd[cmd.index("--workdir") + 1])
        _write_state(wd, last_verdict="completed", round_num=2,
                     costs={"planner": 1.0, "generator_r1": 3.0})
        return HarnessSubprocessResult(returncode=0, stderr="")

    monkeypatch.setattr("src.bench.harness_runner._ainvoke", fake_ainvoke)

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

    async def fake_ainvoke(cmd, **kwargs):
        return HarnessSubprocessResult(returncode=1, stderr="boom\n")

    monkeypatch.setattr("src.bench.harness_runner._ainvoke", fake_ainvoke)

    result = run_harness_for_sample(
        record,
        run_dir=run_dir,
        project_root=tmp_path,
        extra_args=[],
        log_dir=log_dir,
    )

    assert result.status == "errored"
    assert result.error and "boom" in result.error
    assert result.last_verdict is None


def test_run_harness_for_sample_missing_state(tmp_path: Path, monkeypatch) -> None:
    record = _record("000003")
    run_dir = tmp_path / "runs" / "r1"
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True)

    async def fake_ainvoke(cmd, **kwargs):
        return HarnessSubprocessResult(returncode=0, stderr="")

    monkeypatch.setattr("src.bench.harness_runner._ainvoke", fake_ainvoke)

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

    async def fake_ainvoke(cmd, **kwargs):
        captured["log_path"] = kwargs.get("log_path")
        captured["cmd"] = list(cmd)
        wd = Path(cmd[cmd.index("--workdir") + 1])
        _write_state(wd)
        return HarnessSubprocessResult(returncode=0, stderr="")

    monkeypatch.setattr("src.bench.harness_runner._ainvoke", fake_ainvoke)

    run_harness_for_sample(
        record,
        run_dir=run_dir,
        project_root=tmp_path,
        extra_args=["--max-rounds", "3", "--max-budget", "30"],
        log_dir=log_dir,
    )

    assert captured["log_path"] == log_dir / "harness_000004.log"
    cmd_list = captured["cmd"]
    wd_idx = cmd_list.index("--workdir")
    assert cmd_list[wd_idx + 1] == str(run_dir / "samples" / "000004")
    assert cmd_list[wd_idx + 2:] == ["--max-rounds", "3", "--max-budget", "30"]
    assert cmd_list[wd_idx - 1] == "build 000004"


def test_load_record_from_state_returns_none_when_file_missing(tmp_path: Path) -> None:
    workdir = tmp_path / "samples" / "000001"
    workdir.mkdir(parents=True)
    assert _load_record_from_state(workdir, run_dir=tmp_path) is None


def test_load_record_from_state_parses_costs_and_verdict(tmp_path: Path) -> None:
    workdir = tmp_path / "samples" / "000001"
    workdir.mkdir(parents=True)
    _write_state(workdir, last_verdict="accepted_review", round_num=2,
                 costs={"planner": 1.5, "generator_r1": 3.5, "evaluator_r1": 2.0})
    record = _load_record_from_state(workdir, run_dir=tmp_path)
    assert record is not None
    assert record.status == "completed"
    assert record.last_verdict == "accepted_review"
    assert record.rounds == 2
    assert record.cost_usd == pytest.approx(7.0)
    assert record.workdir == "samples/000001"


@pytest.mark.anyio
async def test_arun_harness_for_sample_success(tmp_path: Path, monkeypatch) -> None:
    """Direct test of the async entrypoint that concurrent_runner uses."""
    record = _record("000005")
    run_dir = tmp_path / "runs" / "r1"
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True)

    async def fake_ainvoke(cmd, **kwargs):
        wd = Path(cmd[cmd.index("--workdir") + 1])
        _write_state(wd, last_verdict="failed_review", round_num=1,
                     costs={"planner": 0.5})
        return HarnessSubprocessResult(returncode=0, stderr="")

    monkeypatch.setattr("src.bench.harness_runner._ainvoke", fake_ainvoke)

    result = await arun_harness_for_sample(
        record,
        run_dir=run_dir,
        project_root=tmp_path,
        extra_args=["--frontend-port", "5174"],
        log_dir=log_dir,
    )

    assert result.status == "completed"
    assert result.last_verdict == "failed_review"
    assert result.rounds == 1
    assert result.cost_usd == pytest.approx(0.5)
