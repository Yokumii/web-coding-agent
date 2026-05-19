import pytest

from src.utils.bash_policy import (
    validate_bash_command,
    validate_bash_command_readonly,
)


@pytest.mark.parametrize(
    "command,expected",
    [
        ("echo hi > out.txt", "shell control operator not allowed: >"),
        ("printf 'x\\n'", "command not in allowlist: printf"),
        ("pwd\nls", "shell control operator not allowed: newline"),
        ("npm run dev &", "shell control operator not allowed: &"),
    ],
)
def test_validate_bash_command_rejects_disallowed_shell_forms(command, expected):
    with pytest.raises(ValueError, match=expected):
        validate_bash_command(command)


def test_validate_bash_command_rejects_path_escape():
    with pytest.raises(ValueError, match="path escapes workdir"):
        validate_bash_command("cat ../secret.txt")


def test_validate_bash_command_readonly_rejects_inline_python():
    with pytest.raises(ValueError, match="inline code flag not allowed"):
        validate_bash_command_readonly("python3 -c 'print(1)'")


def test_validate_bash_command_readonly_rejects_mutating_package_subcommand():
    with pytest.raises(ValueError, match="npm subcommand not allowed"):
        validate_bash_command_readonly("npm --prefix frontend run build")


def test_validate_bash_command_readonly_rejects_find_exec():
    with pytest.raises(ValueError, match="find flag not allowed: -exec"):
        validate_bash_command_readonly("find frontend -exec")


def test_validate_bash_command_readonly_accepts_rg_search():
    assert validate_bash_command_readonly("rg -n pattern frontend/src") == [
        "rg",
        "-n",
        "pattern",
        "frontend/src",
    ]
