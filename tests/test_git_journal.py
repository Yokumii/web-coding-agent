from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import anyio
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


async def _run_git_for_test(repo: Path, *args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(repo),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    return proc.returncode or 0, stdout_b.decode(), stderr_b.decode()


async def _git_log_subjects(repo: Path) -> list[str]:
    rc, out, err = await _run_git_for_test(repo, "log", "--format=%s")
    assert rc == 0, err
    return [line for line in out.splitlines() if line]


@pytest.mark.anyio
async def test_commit_round_happy_path_creates_commit(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>r1</h1>")

    result = await commit_round(
        frontend, round_n=1, sprint_num=1, mode="generate", prior_grade=None, accepted=[],
    )

    assert result.success is True
    assert result.commit_hash is not None
    assert len(result.commit_hash) in (40, 64)  # SHA-1 default, SHA-256 if init.defaultObjectFormat
    assert all(c in "0123456789abcdef" for c in result.commit_hash)
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


@pytest.mark.anyio
async def test_commit_round_returns_failure_when_subprocess_startup_raises(
    monkeypatch, tmp_path: Path,
):
    """If asyncio.create_subprocess_exec itself raises (e.g. git removed
    between is_git_available() and the actual call, or frontend_dir is
    a file masquerading as a dir), commit_round must still not raise."""
    frontend = tmp_path / "frontend"
    frontend.mkdir()

    real_create = asyncio.create_subprocess_exec

    async def boom(*args, **kwargs):
        raise FileNotFoundError("simulated: git binary disappeared")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    result = await commit_round(frontend, round_n=1, sprint_num=1, mode="generate")

    assert result.success is False
    # The exact error string is implementation-detail; assert it carries
    # the subprocess context plus the simulated FileNotFoundError message.
    assert "subprocess error" in (result.error or "") or "ensure_repo failed" in (result.error or "")

    # Restore so subsequent test setup/teardown is unaffected.
    monkeypatch.setattr(asyncio, "create_subprocess_exec", real_create)


@pytest.mark.anyio
async def test_commit_round_survives_leaked_anyio_cancellation(
    monkeypatch, tmp_path: Path,
):
    frontend = tmp_path / "frontend"
    frontend.mkdir()

    expected = CommitResult(
        success=True,
        commit_hash="abc1234",
        message="synthetic commit",
        was_empty=False,
    )

    async def fake_commit_round_once(*args, **kwargs):
        await asyncio.sleep(0.01)
        return expected

    monkeypatch.setattr("src.orchestration.git_journal._commit_round_once", fake_commit_round_once)

    with anyio.CancelScope() as scope:
        scope.cancel()
        result = await commit_round(frontend, round_n=1, sprint_num=1, mode="generate")

    assert result is expected
    assert asyncio.current_task() is not None
    assert asyncio.current_task().cancelling() == 0
