"""集中定义 Bash 指令校验规则，供 agent 权限边界共用。"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

_DISALLOWED_SHELL_SNIPPETS = (">", "<", "$(", "`", "&", "\n", "\r")
_DISALLOWED_READONLY_SHELL_OPERATORS = ("&&", "||", "|", ";", "&")
_SHELL_OPERATOR_PATTERN = re.compile(r"(\&\&|\|\||[|;&])")
_ALLOWED_BASH_COMMANDS = {
    "cat",
    "cd",
    "cp",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "mkdir",
    "mv",
    "node",
    "npm",
    "npx",
    "pnpm",
    "pwd",
    "pytest",
    "python",
    "python3",
    "rg",
    "sed",
    "sort",
    "tail",
    "touch",
    "tsc",
    "uv",
    "uvicorn",
    "vite",
    "wc",
    "which",
    "yarn",
}

# harness 允许的 git 子命令集合。
_GIT_ALLOWED_SUBCOMMANDS = frozenset({
    "status",
    "diff",
    "log",
    "show",
    "add",
    "commit",
    "rev-parse",
    "branch",
    "ls-files",
    "stash",
})

# 只读 Bash 配置可使用的 git 子命令子集。
_GIT_READONLY_SUBCOMMANDS = frozenset({
    "status",
    "diff",
    "log",
    "show",
    "rev-parse",
    "branch",
    "ls-files",
})

# 只读 Bash 配置允许的可执行命令集合。
_READONLY_ALLOWED_BASH_COMMANDS = frozenset({
    "cat",
    "find",
    "git",
    "grep",
    "head",
    "ls",
    "node",
    "npm",
    "npx",
    "pnpm",
    "pwd",
    "python",
    "python3",
    "rg",
    "sort",
    "tail",
    "wc",
    "which",
    "yarn",
})

# 这些解释器参数会把可执行代码直接塞进命令行，需在只读模式下禁止。
_INTERPRETER_INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--eval", "-i"})

# 只读 Bash 配置允许的包管理器子命令。
_PKG_MANAGER_READONLY_SUBCOMMANDS = frozenset({
    "list",
    "ls",
    "view",
    "info",
    "outdated",
})

# 这些 find 参数可触发执行、删除或额外输出文件，应统一拦截。
_FORBIDDEN_FIND_FLAGS = frozenset({
    "-exec",
    "-execdir",
    "-delete",
    "-print0",
    "-fprint",
    "-fprintf",
    "-fprint0",
    "-fls",
    "-ok",
    "-okdir",
})

_DISALLOWED_SHELL_MESSAGE_MAP = {
    "\n": "newline",
    "\r": "newline",
    "$(": "$(",
    "`": "`",
    ">": ">",
    "<": "<",
}


def _reject_disallowed_shell_snippets(command: str) -> None:
    """拦截明确禁止的 shell 片段，并维持稳定错误文案。"""
    for snippet, label in _DISALLOWED_SHELL_MESSAGE_MAP.items():
        if snippet in command:
            raise ValueError(f"shell control operator not allowed: {label}")
    if re.search(r"(^|[^&])&([^&]|$)", command):
        raise ValueError("shell control operator not allowed: &")


def _validate_relative_tokens(tokens: list[str]) -> None:
    """校验命令参数中的路径相关 token 不会逃逸出 workdir。"""
    for token in tokens:
        if token.startswith("~"):
            raise ValueError(f"path shortcuts not allowed in bash command: {token}")
        token_path = Path(token)
        if token_path.is_absolute():
            raise ValueError(f"absolute paths not allowed in bash command: {token}")
        if ".." in token_path.parts:
            raise ValueError(f"path escapes workdir in bash command: {token}")


def validate_bash_command(command: str) -> list[str]:
    """校验通用 Bash 指令，返回首段命令的 argv。"""
    stripped = command.strip()
    if not stripped:
        raise ValueError("empty command")

    _reject_disallowed_shell_snippets(stripped)

    segments = _split_bash_segments(stripped)
    if not segments:
        raise ValueError("empty command")

    for argv in segments:
        _validate_bash_argv(argv)
    return segments[0]


def _split_bash_segments(command: str) -> list[list[str]]:
    """按 shell 运算符拆分命令链，并保留每段 argv。"""
    parts = _SHELL_OPERATOR_PATTERN.split(command)
    segments: list[list[str]] = []
    for part in parts:
        stripped = part.strip()
        if not stripped or stripped in {"&&", "||", "|", ";", "&"}:
            continue
        argv = shlex.split(stripped)
        if not argv:
            raise ValueError("empty command")
        segments.append(argv)
    return segments


def _validate_bash_argv(argv: list[str]) -> None:
    """校验单段 Bash 命令的可执行文件与参数。"""
    executable = argv[0]
    if executable not in _ALLOWED_BASH_COMMANDS:
        raise ValueError(f"command not in allowlist: {executable}")

    if executable == "git":
        _validate_git_argv(argv)
    elif executable == "find":
        _validate_find_argv(argv)

    _validate_relative_tokens(argv[1:])


def _validate_git_argv(argv: list[str]) -> None:
    """校验 git 子命令是否位于允许集合内。"""
    if len(argv) < 2:
        raise ValueError("git requires a subcommand")
    # 禁止在子命令前塞入配置参数，避免绕过子命令白名单。
    if argv[1].startswith("-"):
        raise ValueError(f"git flags before the subcommand are not allowed: {argv[1]}")
    subcommand = argv[1]
    if subcommand not in _GIT_ALLOWED_SUBCOMMANDS:
        raise ValueError(f"git subcommand not allowed: {subcommand}")


def _validate_find_argv(argv: list[str]) -> None:
    """校验 find 参数，阻断执行、删除与文件输出能力。"""
    for token in argv[1:]:
        if token in _FORBIDDEN_FIND_FLAGS:
            raise ValueError(f"find flag not allowed: {token}")
        # `-fprint*` 家族统一禁止，避免遗漏变体。
        if token.startswith("-fprint"):
            raise ValueError(f"find flag not allowed: {token}")


def validate_bash_command_readonly(command: str) -> list[str]:
    """校验只读 Bash 指令，额外阻断改写文件与执行脚本的能力。"""
    for snippet in _DISALLOWED_READONLY_SHELL_OPERATORS:
        if snippet in command:
            raise ValueError(f"shell control operator not allowed: {snippet}")

    argv = validate_bash_command(command)
    executable = argv[0]
    if executable not in _READONLY_ALLOWED_BASH_COMMANDS:
        raise ValueError(
            f"command not allowed in read-only bash profile: {executable}"
        )

    if executable == "git":
        if len(argv) < 2:
            raise ValueError("git requires a subcommand")
        if argv[1] not in _GIT_READONLY_SUBCOMMANDS:
            raise ValueError(
                f"git subcommand not allowed in read-only bash profile: {argv[1]}"
            )
    elif executable in {"python", "python3", "node"}:
        for token in argv[1:]:
            if token in _INTERPRETER_INLINE_CODE_FLAGS:
                raise ValueError(
                    f"inline code flag not allowed in read-only bash profile: {token}"
                )
    elif executable in {"npm", "pnpm", "yarn", "npx"}:
        # 跳过前置 flag，读取第一个非 flag token 作为子命令。
        subcommand = next((tok for tok in argv[1:] if not tok.startswith("-")), None)
        if subcommand is None:
            raise ValueError(
                f"{executable} requires a read-only subcommand "
                f"({sorted(_PKG_MANAGER_READONLY_SUBCOMMANDS)})"
            )
        if subcommand not in _PKG_MANAGER_READONLY_SUBCOMMANDS:
            raise ValueError(
                f"{executable} subcommand not allowed in read-only bash profile: "
                f"{subcommand}"
            )

    return argv
