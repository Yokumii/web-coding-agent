from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.bench.manifest import HarnessRecord, SampleRecord


@dataclass
class HarnessSubprocessResult:
    returncode: int
    stderr: str


async def arun_harness_for_sample(
    sample: SampleRecord,
    *,
    run_dir: Path,
    project_root: Path,
    extra_args: list[str],
    log_dir: Path,
) -> HarnessRecord:
    """Run harness for one sample as a subprocess."""
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
    proc = await _ainvoke(cmd, cwd=project_root, log_path=log_path)
    finished_at = datetime.now().isoformat()

    workdir_field = (
        str(workdir.relative_to(run_dir))
        if workdir.is_relative_to(run_dir) else str(workdir)
    )

    if proc.returncode != 0:
        return HarnessRecord(
            status="errored",
            workdir=workdir_field,
            error=_tail(proc.stderr, lines=50) or f"harness exit {proc.returncode}",
            started_at=started_at,
            finished_at=finished_at,
        )

    record = _load_record_from_state(
        workdir,
        run_dir=run_dir,
        started_at=started_at,
        finished_at=finished_at,
    )
    if record is None:
        state_path = workdir / ".harness" / "harness_state.json"
        return HarnessRecord(
            status="errored",
            workdir=workdir_field,
            error=f"harness exited 0 but harness_state.json not found at {state_path}",
            started_at=started_at,
            finished_at=finished_at,
        )
    return record


def _load_record_from_state(
    workdir: Path,
    *,
    run_dir: Path | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> HarnessRecord | None:
    """Read harness_state.json into a HarnessRecord. Returns None if file missing.

    Shared by arun_harness_for_sample (post-subprocess success path) and
    concurrent_runner.try_salvage_completed_record (resume salvage path).
    """
    state_path = workdir / ".harness" / "harness_state.json"
    if not state_path.is_file():
        return None
    state = json.loads(state_path.read_text(encoding="utf-8"))
    costs = state.get("costs") or {}
    workdir_field = (
        str(workdir.relative_to(run_dir))
        if run_dir is not None and workdir.is_relative_to(run_dir)
        else str(workdir)
    )
    return HarnessRecord(
        status="completed",
        workdir=workdir_field,
        last_verdict=state.get("last_verdict"),
        rounds=state.get("round_num"),
        cost_usd=float(sum(v for v in costs.values() if isinstance(v, (int, float)))),
        error=None,
        started_at=started_at,
        finished_at=finished_at,
    )


async def _ainvoke(
    cmd: list[str],
    *,
    cwd: Path,
    log_path: Path,
) -> HarnessSubprocessResult:
    """Async subprocess invocation. tee stdout to log_path, capture stderr.

    start_new_session=True puts the child in its own process group so cancellation
    can kill the whole tree (vite/esbuild children of pnpm dev, etc.).
    Replaceable in tests via monkeypatch.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=log,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, stderr_bytes = await proc.communicate()
        except asyncio.CancelledError:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    os.killpg(pgid, signal.SIGKILL)
                    await proc.wait()
            except (ProcessLookupError, PermissionError):
                pass
            raise
    stderr = (stderr_bytes or b"").decode("utf-8", errors="replace")
    if stderr:
        with log_path.open("a", encoding="utf-8") as log:
            log.write("\n--- STDERR ---\n")
            log.write(stderr)
    return HarnessSubprocessResult(returncode=proc.returncode or 0, stderr=stderr)


def _tail(text: str, *, lines: int) -> str:
    if not text:
        return ""
    return "\n".join(text.splitlines()[-lines:])
