from __future__ import annotations

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
