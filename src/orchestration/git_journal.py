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
    """检查当前环境是否能在 PATH 中找到 `git`。"""
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


def _clear_current_task_cancellation() -> int:
    task = asyncio.current_task()
    if task is None:
        return 0

    cleared = 0
    while task.cancelling():
        task.uncancel()
        cleared += 1
    return cleared


async def _run_git(*args: str, cwd: Path) -> tuple[int, str, str]:
    """在指定目录执行 `git` 命令，并返回退出码与标准输出。"""
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
    """确保 `frontend_dir` 已初始化为可提交的本地 Git 仓库。"""
    if not frontend_dir.exists():
        raise FileNotFoundError(f"frontend dir does not exist: {frontend_dir}")

    if (frontend_dir / ".git").exists():
        return

    rc, _out, err = await _run_git("init", "-b", default_branch, cwd=frontend_dir)
    if rc != 0:
        rc2, _out2, err2 = await _run_git("init", cwd=frontend_dir)
        if rc2 != 0:
            raise RuntimeError(f"git init failed: {err.strip() or err2.strip()}")
        # 旧版 Git 可能尚未创建默认分支，这里尽力补一次 checkout。
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
    """构造 generator 每轮提交使用的多行 commit message。"""
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


async def _commit_round_once(
    frontend_dir: Path,
    *,
    round_n: int,
    sprint_num: int,
    mode: str,
    prior_grade: dict[str, Any] | None = None,
    accepted: list[int] | None = None,
) -> CommitResult:
    """在 `frontend_dir` 内暂存全部修改并提交，允许空提交。"""
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
    except Exception as exc:  # noqa: BLE001 - 这里统一折叠为 CommitResult 失败信息。
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


async def commit_round(
    frontend_dir: Path,
    *,
    round_n: int,
    sprint_num: int,
    mode: str,
    prior_grade: dict[str, Any] | None = None,
    accepted: list[int] | None = None,
) -> CommitResult:
    """执行一轮提交，并吸收外层任务可能泄漏的取消信号。"""
    commit_task = asyncio.create_task(
        _commit_round_once(
            frontend_dir,
            round_n=round_n,
            sprint_num=sprint_num,
            mode=mode,
            prior_grade=prior_grade,
            accepted=accepted,
        ),
        name="commit_round",
    )

    try:
        while True:
            try:
                return await asyncio.shield(commit_task)
            except asyncio.CancelledError:
                _clear_current_task_cancellation()
                if commit_task.done():
                    break

        if commit_task.cancelled():
            return CommitResult(
                success=False,
                commit_hash=None,
                message=build_commit_message(
                    round_n=round_n,
                    sprint_num=sprint_num,
                    mode=mode,
                    prior_grade=prior_grade,
                    accepted=accepted,
                ),
                error="commit round cancelled before completion",
            )

        exc = commit_task.exception()
        if exc is not None:
            raise exc
        return commit_task.result()
    finally:
        if not commit_task.done():
            commit_task.cancel()
