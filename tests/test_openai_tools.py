from pathlib import Path

import pytest

from src.agents.openai_tools import OpenAIToolExecutor


@pytest.mark.anyio
async def test_write_file_overwrites_without_prior_read(tmp_path: Path):
    target = tmp_path / "a.txt"
    target.write_text("old")
    tools = OpenAIToolExecutor(workdir=tmp_path, allow_bash=False)

    result = await tools.execute("write_file", {"path": "a.txt", "content": "new"})

    assert result.ok and target.read_text() == "new"


@pytest.mark.anyio
async def test_tools_reject_path_escape_and_disallowed_command(tmp_path: Path):
    tools = OpenAIToolExecutor(workdir=tmp_path, allow_bash=True)

    escaped = await tools.execute("read_file", {"path": "../secret"})
    command = await tools.execute("run_command", {"command": "curl example.com"})

    assert not escaped.ok and "escapes workdir" in escaped.output
    assert not command.ok and "allowlist" in command.output


@pytest.mark.anyio
async def test_command_drops_redundant_stderr_merge(tmp_path: Path):
    tools = OpenAIToolExecutor(workdir=tmp_path, allow_bash=True)
    result = await tools.execute("run_command", {"command": "node --version 2>&1"})
    assert result.ok


@pytest.mark.anyio
async def test_command_timeout_kills_entire_process_group(tmp_path: Path):
    tools = OpenAIToolExecutor(workdir=tmp_path, allow_bash=True, command_timeout=0.05)
    result = await tools.execute(
        "run_command",
        {"command": "node -e setInterval\\(function\\(\\)\\{\\},1000\\)"},
    )
    assert not result.ok and "timed out" in result.output
