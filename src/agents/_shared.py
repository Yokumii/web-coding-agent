from __future__ import annotations

import shutil
from pathlib import Path


def expose_local_claude_skills(workdir: Path, source_skills_dir: Path) -> None:
    """将仓库内置 skills 实体复制到 agent 工作目录。

    Native tools 会拒绝解析后逃出 workdir 的路径，因此不能使用指向仓库目录的
    绝对符号链接。旧运行留下的 symlink 会在这里自动迁移为普通目录。
    """
    if not source_skills_dir.is_dir():
        return

    claude_dir = workdir / ".claude"
    skills_dir = claude_dir / "skills"
    claude_dir.mkdir(parents=True, exist_ok=True)
    if skills_dir.is_symlink():
        skills_dir.unlink()
    shutil.copytree(source_skills_dir, skills_dir, dirs_exist_ok=True)
