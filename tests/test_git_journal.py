from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from src.orchestration.git_journal import (
    CommitResult,
    build_commit_message,
    commit_round,
    ensure_repo,
    is_git_available,
)


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_is_git_available_returns_true_when_git_on_path():
    assert is_git_available() is True


async def _read_git_config(repo: Path, key: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "config", "--get", key,
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, _ = await proc.communicate()
    return stdout_b.decode().strip()


@pytest.mark.anyio
async def test_ensure_repo_initializes_when_missing(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()

    await ensure_repo(frontend)

    assert (frontend / ".git").is_dir()
    assert (await _read_git_config(frontend, "user.name")).startswith("web-coding-agent")
    assert (await _read_git_config(frontend, "user.email")) == "harness@local"


@pytest.mark.anyio
async def test_ensure_repo_is_idempotent(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()

    await ensure_repo(frontend)
    # Second call must not raise and must leave the existing config untouched.
    await ensure_repo(frontend)

    assert (frontend / ".git").is_dir()


@pytest.mark.anyio
async def test_ensure_repo_writes_default_gitignore_when_missing(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()

    await ensure_repo(frontend)

    gitignore = frontend / ".gitignore"
    assert gitignore.is_file()
    assert "node_modules" in gitignore.read_text()


@pytest.mark.anyio
async def test_ensure_repo_does_not_overwrite_existing_gitignore(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / ".gitignore").write_text("custom-thing\n")

    await ensure_repo(frontend)

    assert (frontend / ".gitignore").read_text() == "custom-thing\n"


@pytest.mark.anyio
async def test_ensure_repo_raises_when_dir_missing(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        await ensure_repo(tmp_path / "does-not-exist")
