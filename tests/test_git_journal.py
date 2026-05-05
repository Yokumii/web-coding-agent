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


def test_build_commit_message_generate_round_no_prior_grade():
    msg = build_commit_message(
        round_n=1,
        sprint_num=1,
        mode="generate",
        prior_grade=None,
        accepted=[],
    )
    lines = msg.splitlines()
    assert lines[0] == "round 01 / sprint_1 (generate): generator output"
    assert "target: sprint_1" in lines
    assert "accepted: []" in lines
    assert not any(line.startswith("prior grade:") for line in lines)


def test_build_commit_message_repair_round_with_prior_grade():
    prior = {
        "criteria": {
            "design_quality": {"score": 7},
            "functionality":  {"score": 5},
            "originality":    {"score": 6},
            "craft":          {"score": 7},
        },
        "overall_passed": False,
    }
    msg = build_commit_message(
        round_n=3,
        sprint_num=2,
        mode="repair",
        prior_grade=prior,
        accepted=[1],
    )
    lines = msg.splitlines()
    assert lines[0] == "round 03 / sprint_2 (repair): generator output"
    assert "target: sprint_2" in lines
    assert "accepted: [1]" in lines
    assert any(
        line.startswith("prior grade:")
        and "design_quality=7" in line
        and "functionality=5" in line
        and "passed=False" in line
        for line in lines
    )


def test_build_commit_message_unknown_mode_still_renders():
    # We do not validate mode strings here; the orchestrator already does.
    msg = build_commit_message(round_n=2, sprint_num=4, mode="custom", prior_grade=None, accepted=None)
    assert "round 02 / sprint_4 (custom): generator output" in msg


async def _git_log_subjects(repo: Path) -> list[str]:
    rc, out, err = await _run_git_for_test(repo, "log", "--format=%s")
    assert rc == 0, err
    return [line for line in out.splitlines() if line]


async def _run_git_for_test(repo: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    return proc.returncode or 0, stdout_b.decode(), stderr_b.decode()


@pytest.mark.anyio
async def test_commit_round_happy_path_creates_commit(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>r1</h1>")

    result = await commit_round(
        frontend, round_n=1, sprint_num=1, mode="generate", prior_grade=None, accepted=[],
    )

    assert result.success is True
    assert result.commit_hash is not None and len(result.commit_hash) == 40
    assert "round 01 / sprint_1 (generate)" in result.message
    assert result.was_empty is False

    subjects = await _git_log_subjects(frontend)
    assert subjects[0].startswith("round 01 / sprint_1 (generate)")


@pytest.mark.anyio
async def test_commit_round_allows_empty_when_no_changes(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>r1</h1>")

    first = await commit_round(frontend, round_n=1, sprint_num=1, mode="generate")
    assert first.success and first.was_empty is False

    # No file changes between rounds — must still produce an empty commit.
    second = await commit_round(frontend, round_n=2, sprint_num=1, mode="repair")
    assert second.success is True
    assert second.was_empty is True

    subjects = await _git_log_subjects(frontend)
    assert subjects[0].startswith("round 02 / sprint_1 (repair)")
    assert subjects[1].startswith("round 01 / sprint_1 (generate)")


@pytest.mark.anyio
async def test_commit_round_returns_failure_when_dir_missing(tmp_path: Path):
    result = await commit_round(
        tmp_path / "no-such", round_n=1, sprint_num=1, mode="generate",
    )
    assert result.success is False
    assert result.commit_hash is None
    assert "frontend dir missing" in (result.error or "")


@pytest.mark.anyio
async def test_commit_round_returns_failure_when_git_unavailable(monkeypatch, tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()

    monkeypatch.setattr("src.orchestration.git_journal.is_git_available", lambda: False)

    result = await commit_round(frontend, round_n=1, sprint_num=1, mode="generate")
    assert result.success is False
    assert result.error == "git not on PATH"
    assert not (frontend / ".git").exists()
