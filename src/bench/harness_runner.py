from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.bench.manifest import HarnessRecord, SampleRecord


@dataclass
class HarnessSubprocessResult:
    returncode: int
    stderr: str


def run_harness_for_sample(
    sample: SampleRecord,
    *,
    run_dir: Path,
    project_root: Path,
    extra_args: list[str],
    log_dir: Path,
) -> HarnessRecord:
    """Run harness as subprocess for one sample, return updated HarnessRecord.

    Caller is responsible for persisting the result back into manifest.
    """
    workdir = (run_dir / "samples" / sample.id).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"harness_{sample.id}.log"

    cmd = [
        "uv", "run", "python", "-m", "src.main",
        sample.instruction,
        "--workdir", str(workdir),
        *list(extra_args),
    ]
    started_at = datetime.now().isoformat()
    proc = _invoke(cmd, cwd=project_root, log_path=log_path)
    finished_at = datetime.now().isoformat()

    state_path = workdir / ".harness" / "harness_state.json"

    if proc.returncode != 0:
        return HarnessRecord(
            status="errored",
            workdir=str(workdir.relative_to(run_dir)) if workdir.is_relative_to(run_dir) else str(workdir),
            error=_tail(proc.stderr, lines=50) or f"harness exit {proc.returncode}",
            started_at=started_at,
            finished_at=finished_at,
        )

    if not state_path.is_file():
        return HarnessRecord(
            status="errored",
            workdir=str(workdir.relative_to(run_dir)) if workdir.is_relative_to(run_dir) else str(workdir),
            error=f"harness exited 0 but harness_state.json not found at {state_path}",
            started_at=started_at,
            finished_at=finished_at,
        )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    costs = state.get("costs") or {}
    return HarnessRecord(
        status="completed",
        workdir=str(workdir.relative_to(run_dir)) if workdir.is_relative_to(run_dir) else str(workdir),
        last_verdict=state.get("last_verdict"),
        rounds=state.get("round_num"),
        cost_usd=float(sum(v for v in costs.values() if isinstance(v, (int, float)))),
        error=None,
        started_at=started_at,
        finished_at=finished_at,
    )


def _invoke(cmd: list[str], *, cwd: Path, log_path: Path) -> HarnessSubprocessResult:
    """Run subprocess, tee combined stdout/stderr to log_path. Replaceable in tests."""
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    stderr = proc.stderr or ""
    # Also append stderr to the log so users see it in one place.
    if stderr:
        with log_path.open("a", encoding="utf-8") as log:
            log.write("\n--- STDERR ---\n")
            log.write(stderr)
    return HarnessSubprocessResult(returncode=proc.returncode, stderr=stderr)


def _tail(text: str, *, lines: int) -> str:
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])
