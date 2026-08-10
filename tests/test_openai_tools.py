from pathlib import Path

import pytest

from src.agents.openai_tools import OpenAIToolExecutor, openai_tool_schemas
from src.agents.generator import _validate_generator_outputs
from src.orchestration.file_comm import FileComm


@pytest.mark.anyio
async def test_write_file_overwrites_without_prior_read(tmp_path: Path):
    target = tmp_path / "a.txt"
    target.write_text("old")
    tools = OpenAIToolExecutor(workdir=tmp_path, allow_bash=False)

    result = await tools.execute("write_file", {"path": "a.txt", "content": "new"})

    assert result.ok and target.read_text() == "new"


@pytest.mark.anyio
async def test_static_forward_seed_rejects_runtime_scaffold(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text("<main>seed</main>")
    (tmp_path / "seed_manifest.json").write_text('{"source_frontend": "' + str(source) + '"}')
    tools = OpenAIToolExecutor(workdir=tmp_path, allow_bash=False)

    rejected = await tools.execute("write_file", {"path": "frontend/package.json", "content": "{}"})
    allowed = await tools.execute("write_file", {"path": "frontend/main.js", "content": "ok"})

    assert not rejected.ok and "plain static site" in rejected.output
    assert allowed.ok


def test_static_forward_seed_is_a_valid_generator_output(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text("<main>seed</main>")
    (tmp_path / "seed_manifest.json").write_text('{"source_frontend": "' + str(source) + '"}')
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<main>edited</main>")

    _validate_generator_outputs(FileComm(tmp_path), tmp_path, "done")


@pytest.mark.anyio
async def test_tools_reject_path_escape_and_disallowed_command(tmp_path: Path):
    tools = OpenAIToolExecutor(workdir=tmp_path, allow_bash=True)

    escaped = await tools.execute("read_file", {"path": "../secret"})
    command = await tools.execute("run_command", {"command": "curl example.com"})

    assert not escaped.ok and "escapes workdir" in escaped.output
    assert not command.ok and "allowlist" in command.output


@pytest.mark.anyio
async def test_read_file_caps_whole_file_and_supports_focused_line_range(tmp_path: Path):
    (tmp_path / "large.txt").write_text("".join(f"line-{index}\n" for index in range(4_000)))
    tools = OpenAIToolExecutor(workdir=tmp_path, allow_bash=False)

    whole = await tools.execute("read_file", {"path": "large.txt"})
    focused = await tools.execute(
        "read_file", {"path": "large.txt", "start_line": 1200, "end_line": 1202}
    )

    assert whole.ok and "truncated at 32000" in whole.output
    assert focused.ok and focused.output == "line-1199\nline-1200\nline-1201\n"


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


def test_browser_schema_exposes_responsive_viewport_control():
    tools = openai_tool_schemas(allow_bash=False, allow_playwright=True)
    viewport = next(item["function"] for item in tools if item["function"]["name"] == "browser_set_viewport")

    assert viewport["parameters"]["required"] == ["url", "width", "height"]
    assert viewport["parameters"]["properties"]["width"]["type"] == "integer"


def test_browser_schema_exposes_keyboard_action():
    tools = openai_tool_schemas(allow_bash=False, allow_playwright=True)
    keyboard = next(item["function"] for item in tools if item["function"]["name"] == "browser_key_press")

    assert keyboard["parameters"]["required"] == ["url", "key"]
    assert keyboard["parameters"]["properties"]["count"]["type"] == "integer"
