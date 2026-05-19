from __future__ import annotations

import shutil
from pathlib import Path


def expose_local_claude_skills(workdir: Path, source_skills_dir: Path) -> None:
    """将仓库内置的 Claude skills 映射到当前 agent 的工作目录。

    优先创建符号链接，便于本地调试时共用同一份 skills 内容。
    如果文件系统不支持符号链接，则退回到复制目录。
    """
    if not source_skills_dir.is_dir():
        return

    claude_dir = workdir / ".claude"
    skills_dir = claude_dir / "skills"
    if skills_dir.exists() or skills_dir.is_symlink():
        return

    claude_dir.mkdir(parents=True, exist_ok=True)
    try:
        skills_dir.symlink_to(source_skills_dir, target_is_directory=True)
    except OSError:
        shutil.copytree(source_skills_dir, skills_dir)
