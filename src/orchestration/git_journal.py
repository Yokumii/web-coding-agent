from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path


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


async def ensure_repo(frontend_dir: Path, *, default_branch: str = "main") -> None:
    raise NotImplementedError


def build_commit_message(
    *,
    round_n: int,
    sprint_num: int,
    mode: str,
    prior_grade: dict | None = None,
    accepted: list | None = None,
) -> str:
    raise NotImplementedError


async def commit_round(
    frontend_dir: Path,
    *,
    round_n: int,
    sprint_num: int,
    mode: str,
    prior_grade: dict | None = None,
    accepted: list | None = None,
) -> CommitResult:
    raise NotImplementedError
