from __future__ import annotations

import signal
from pathlib import Path

import pytest

from src.agents.sdk_runner import build_playwright_mcp_args
from src.config import HarnessConfig
from src.orchestration.runtime import (
    ManagedProcess,
    RunningAppStack,
    build_frontend_command,
    ensure_port_available,
    start_app_stack,
    wait_for_http,
)


def test_build_frontend_command_defaults_to_npm(tmp_path: Path):
    command = build_frontend_command(tmp_path, 5173)
    assert command[:3] == ["npm", "run", "dev"]
    assert command[-3:] == ["--port", "5173", "--strictPort"]


def test_build_frontend_command_prefers_pnpm_lockfile(tmp_path: Path):
    (tmp_path / "pnpm-lock.yaml").write_text("")
    command = build_frontend_command(tmp_path, 5173)
    assert command == [
        "pnpm",
        "dev",
        "--host",
        "127.0.0.1",
        "--port",
        "5173",
        "--strictPort",
    ]


def test_playwright_mcp_params_default_to_isolated_mode():
    params = build_playwright_mcp_args(HarnessConfig())
    assert params == ["@playwright/mcp@latest", "--isolated"]


def test_playwright_mcp_params_include_headless_flag():
    params = build_playwright_mcp_args(HarnessConfig(playwright_headless=True))
    assert params == ["@playwright/mcp@latest", "--isolated", "--headless"]


def test_ensure_port_available_terminates_listener(monkeypatch):
    states = iter([[4321], []])
    sent_signals: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr("src.orchestration.runtime.find_listening_pids", lambda port: next(states))
    monkeypatch.setattr("src.orchestration.runtime.os.kill", lambda pid, sig: sent_signals.append((pid, sig)))
    monkeypatch.setattr("src.orchestration.runtime.wait_for_port_release", lambda port: True)

    ensure_port_available(5173)

    assert sent_signals == [(4321, signal.SIGTERM)]


def test_ensure_port_available_escalates_to_sigkill(monkeypatch):
    pid_snapshots = iter([[4321], [4321], []])
    sent_signals: list[tuple[int, signal.Signals]] = []
    release_attempts = iter([False, True])

    monkeypatch.setattr("src.orchestration.runtime.find_listening_pids", lambda port: next(pid_snapshots))
    monkeypatch.setattr("src.orchestration.runtime.os.kill", lambda pid, sig: sent_signals.append((pid, sig)))
    monkeypatch.setattr(
        "src.orchestration.runtime.wait_for_port_release",
        lambda port: next(release_attempts),
    )

    ensure_port_available(5173)

    assert sent_signals == [
        (4321, signal.SIGTERM),
        (4321, signal.SIGKILL),
    ]


class DummyProcess:
    def poll(self):
        return None


class DummyLogFile:
    def close(self):
        return None


class DummyManagedProcess:
    def poll(self):
        return None


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_wait_for_http_rejects_404(monkeypatch, tmp_path: Path):
    managed = ManagedProcess(
        name="frontend",
        process=DummyProcess(),
        log_path=tmp_path / "frontend.log",
        log_file=DummyLogFile(),
    )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr("src.orchestration.runtime.fetch_status_code", lambda url: 404)
    monkeypatch.setattr("src.orchestration.runtime.asyncio.sleep", fake_sleep)

    with pytest.raises(TimeoutError):
        await wait_for_http(
            name="frontend",
            url="http://127.0.0.1:5173",
            managed=managed,
            timeout_secs=0,
        )


@pytest.mark.anyio
async def test_start_app_stack_requires_only_frontend(monkeypatch, tmp_path: Path):
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    harness_dir = tmp_path / ".harness"
    calls: list[tuple[str, int | str]] = []

    def fake_start_process(*, name: str, command: list[str], cwd: Path, log_path: Path):
        calls.append((name, "start"))
        return ManagedProcess(
            name=name,
            process=DummyManagedProcess(),
            log_path=log_path,
            log_file=DummyLogFile(),
        )

    async def fake_wait_for_http(**kwargs):
        calls.append(("wait_for_http", kwargs["url"]))
        return None

    def fake_ensure_port_available(port: int):
        calls.append(("ensure_port_available", port))

    monkeypatch.setattr("src.orchestration.runtime.ensure_port_available", fake_ensure_port_available)
    monkeypatch.setattr("src.orchestration.runtime.start_process", fake_start_process)
    monkeypatch.setattr("src.orchestration.runtime.wait_for_http", fake_wait_for_http)

    stack = await start_app_stack(tmp_path, harness_dir, HarnessConfig(), round_num=1)

    assert isinstance(stack, RunningAppStack)
    assert calls == [
        ("ensure_port_available", 5173),
        ("frontend", "start"),
        ("wait_for_http", "http://127.0.0.1:5173"),
    ]
