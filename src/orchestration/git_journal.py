from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CommitResult:
    success: bool
    commit_hash: str | None
    message: str
    error: str | None = None
    was_empty: bool = False


def is_git_available() -> bool:
    """True iff `git` is resolvable on PATH."""
    return shutil.which("git") is not None


_DEFAULT_GITIGNORE = """\
node_modules/
dist/
build/
.next/
.vite/
.cache/
.parcel-cache/
.turbo/
.svelte-kit/
*.log
.DS_Store
"""


async def _run_git(*args: str, cwd: Path) -> tuple[int, str, str]:
    """Run `git <args>` in cwd, capturing stdout/stderr as text.

    Subprocess startup failures (OSError: git binary missing, cwd does
    not exist, EMFILE, ...) are converted to `(1, "", "subprocess error: ...")`
    so callers can treat them as ordinary git failures rather than having
    to wrap each call in their own try/except.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
    except OSError as exc:
        return 1, "", f"subprocess error: {exc}"
    return (
        proc.returncode if proc.returncode is not None else 0,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def ensure_repo(frontend_dir: Path, *, default_branch: str = "main") -> None:
    """Ensure `frontend_dir` is a git repo with sensible local config.

    - If `frontend_dir` does not exist, raises FileNotFoundError.
    - If `.git` is already there, no-op.
    - Otherwise: `git init -b <default_branch>` (falling back to `git init`
      + `git checkout -b` for older gits), set local user.name/email, and
      write a default `.gitignore` only if the user has not provided one.
    """
    if not frontend_dir.exists():
        raise FileNotFoundError(f"frontend dir does not exist: {frontend_dir}")

    if (frontend_dir / ".git").exists():
        return

    rc, _out, err = await _run_git("init", "-b", default_branch, cwd=frontend_dir)
    if rc != 0:
        rc2, _out2, err2 = await _run_git("init", cwd=frontend_dir)
        if rc2 != 0:
            raise RuntimeError(f"git init failed: {err.strip() or err2.strip()}")
        # Best-effort branch rename; non-fatal if it fails on detached HEAD pre-commit gits.
        await _run_git("checkout", "-b", default_branch, cwd=frontend_dir)

    await _run_git("config", "user.name", "web-coding-agent harness", cwd=frontend_dir)
    await _run_git("config", "user.email", "harness@local", cwd=frontend_dir)

    gitignore = frontend_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_DEFAULT_GITIGNORE)


def build_commit_message(
    *,
    round_n: int,
    sprint_num: int,
    mode: str,
    prior_grade: dict[str, Any] | None = None,
    accepted: list[int] | None = None,
) -> str:
    """Build the multi-line commit message for a generator round.

    Format:
        round 03 / sprint_2 (repair): generator output

        target: sprint_2
        accepted: [1]
        prior grade: design_quality=7 functionality=5 originality=6 craft=7 passed=False

    Best-effort render: if the grade JSON renames `criteria` or `overall_passed`,
    or if a criterion's value is not a dict, the prior-grade line silently
    degrades (empty scores or `passed=None`). The commit still lands; readers
    relying on this line for forensic info should treat its absence/emptiness
    as "schema drifted, check `grade_round_*.json`".
    """
    sprint_id = f"sprint_{sprint_num}"
    title = f"round {round_n:02d} / {sprint_id} ({mode}): generator output"

    body: list[str] = ["", f"target: {sprint_id}"]
    if accepted is not None:
        body.append(f"accepted: {list(accepted)}")
    if isinstance(prior_grade, dict):
        criteria = prior_grade.get("criteria")
        if not isinstance(criteria, dict):
            criteria = {}
        scores = " ".join(
            f"{name}={(value if isinstance(value, dict) else {}).get('score')}"
            for name, value in criteria.items()
        )
        passed = prior_grade.get("overall_passed")
        body.append(f"prior grade: {scores} passed={passed}".rstrip())

    return title + "\n" + "\n".join(body) + "\n"


async def commit_round(
    frontend_dir: Path,
    *,
    round_n: int,
    sprint_num: int,
    mode: str,
    prior_grade: dict[str, Any] | None = None,
    accepted: list[int] | None = None,
) -> CommitResult:
    """Stage everything in `frontend_dir` and commit, allowing empty commits.

    Returns a CommitResult — never raises. On any failure path
    (`git` not on PATH, dir missing, init failure, commit failure)
    `success=False` and `error` carries the reason.
    """
    message = build_commit_message(
        round_n=round_n,
        sprint_num=sprint_num,
        mode=mode,
        prior_grade=prior_grade,
        accepted=accepted,
    )

    if not is_git_available():
        return CommitResult(success=False, commit_hash=None, message=message, error="git not on PATH")

    if not frontend_dir.exists():
        return CommitResult(
            success=False,
            commit_hash=None,
            message=message,
            error=f"frontend dir missing: {frontend_dir}",
        )

    try:
        await ensure_repo(frontend_dir)
    except Exception as exc:  # noqa: BLE001 — we deliberately swallow into CommitResult
        return CommitResult(
            success=False, commit_hash=None, message=message, error=f"ensure_repo failed: {exc}"
        )

    rc, _out, err = await _run_git("add", "-A", cwd=frontend_dir)
    if rc != 0:
        return CommitResult(
            success=False, commit_hash=None, message=message, error=f"git add failed: {err.strip()}"
        )

    rc, status_out, _err = await _run_git("status", "--porcelain", cwd=frontend_dir)
    was_empty = (rc == 0 and not status_out.strip())

    rc, _out, err = await _run_git(
        "-c", "commit.gpgsign=false",
        "commit", "--allow-empty", "-m", message,
        cwd=frontend_dir,
    )
    if rc != 0:
        return CommitResult(
            success=False,
            commit_hash=None,
            message=message,
            error=f"git commit failed: {err.strip()}",
            was_empty=was_empty,
        )

    rc, hash_out, _err = await _run_git("rev-parse", "HEAD", cwd=frontend_dir)
    commit_hash = hash_out.strip() if rc == 0 else None

    return CommitResult(
        success=True,
        commit_hash=commit_hash,
        message=message,
        was_empty=was_empty,
    )
