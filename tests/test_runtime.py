from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

from src.agents.sdk_runner import build_playwright_mcp_args
from src.config import HarnessConfig
from src.orchestration import runtime
from src.orchestration.runtime import (
    ManagedProcess,
    RunningAppStack,
    build_frontend_command,
    ensure_port_available,
    start_app_stack,
    start_process,
    stop_process,
    wait_for_http,
)


def test_build_frontend_command_defaults_to_npm(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    command = build_frontend_command(tmp_path, 5173)
    assert command[:3] == ["npm", "run", "dev"]
    assert command[-3:] == ["--port", "5173", "--strictPort"]


def test_build_frontend_command_uses_python_static_server_without_package_json(tmp_path: Path):
    (tmp_path / "index.html").write_text("<main>static</main>")

    assert build_frontend_command(tmp_path, 5173) == [
        "python3", "-m", "http.server", "5173", "--bind", "127.0.0.1",
    ]


def test_build_frontend_command_uses_static_server_for_empty_agent_npm_stub(tmp_path: Path):
    (tmp_path / "index.html").write_text("<main>static</main>")
    (tmp_path / "package.json").write_text('{"scripts": {"dev": "node dev-server.js"}}')

    assert build_frontend_command(tmp_path, 5173) == [
        "python3", "-m", "http.server", "5173", "--bind", "127.0.0.1",
    ]


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
    params = build_playwright_mcp_args(HarnessConfig(playwright_headless=False))
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
        (4321, runtime.FORCE_KILL_SIGNAL),
    ]


def test_find_listening_pids_windows_parses_netstat(monkeypatch):
    class FakeResult:
        returncode = 0
        stderr = ""
        stdout = "\n".join(
            [
                "  Proto  Local Address          Foreign Address        State           PID",
                "  TCP    127.0.0.1:5173         0.0.0.0:0              LISTENING       4321",
                "  TCP    127.0.0.1:3000         0.0.0.0:0              LISTENING       9999",
                "  TCP    [::]:5173              [::]:0                 LISTENING       4321",
            ]
        )

    monkeypatch.setattr(runtime, "IS_WINDOWS", True)
    monkeypatch.setattr("src.orchestration.runtime.subprocess.run", lambda *args, **kwargs: FakeResult())

    assert runtime.find_listening_pids(5173) == [4321]


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

    def fake_start_process(
        *, name: str, command: list[str], cwd: Path, log_path: Path,
        env_overrides: dict[str, str] | None = None,
    ):
        calls.append((name, "start"))
        calls.append(("env", env_overrides))
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
        ("env", {"HOST": "127.0.0.1", "PORT": "5173"}),
        ("wait_for_http", "http://127.0.0.1:5173"),
    ]


# --- Popen / stop signal the whole process group ---


def test_start_process_launches_in_new_session(monkeypatch, tmp_path: Path):
    captured: dict = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            self.pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr("src.orchestration.runtime.subprocess.Popen", FakePopen)

    log_path = tmp_path / "frontend.log"
    process = start_process(
        name="frontend",
        command=["echo", "hi"],
        cwd=tmp_path,
        log_path=log_path,
        env_overrides={"PORT": "4321"},
    )

    assert process.process.pid == 4242
    assert captured["kwargs"].get("start_new_session") is (not runtime.IS_WINDOWS)
    assert captured["kwargs"]["env"]["PORT"] == "4321"


@pytest.mark.anyio
async def test_stop_process_sigterms_the_process_group(monkeypatch, tmp_path: Path):
    if runtime.IS_WINDOWS:
        pytest.skip("POSIX process-group signaling is unavailable on Windows")
    sent: list[tuple] = []

    class FakeProc:
        def __init__(self):
            self.pid = 9001
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def wait(self, timeout=None):
            self._alive = False
            return 0

        def terminate(self):
            sent.append(("terminate",))
            self._alive = False

        def kill(self):
            sent.append(("kill",))
            self._alive = False

    monkeypatch.setattr("src.orchestration.runtime.os.getpgid", lambda pid: pid)
    monkeypatch.setattr(
        "src.orchestration.runtime.os.killpg",
        lambda pgid, sig: sent.append(("killpg", pgid, sig)),
    )

    log_path = tmp_path / "log.txt"
    log_file = log_path.open("w")
    managed = ManagedProcess("frontend", FakeProc(), log_path, log_file)

    await stop_process(managed)

    assert ("killpg", 9001, signal.SIGTERM) in sent
    # The single-process fallback must NOT be used when killpg succeeded.
    assert ("terminate",) not in sent


@pytest.mark.anyio
async def test_stop_process_escalates_to_sigkill_after_timeout(monkeypatch, tmp_path: Path):
    if runtime.IS_WINDOWS:
        pytest.skip("POSIX process-group signaling is unavailable on Windows")
    sent: list[tuple] = []
    wait_calls = {"count": 0}

    class FakeProc:
        def __init__(self):
            self.pid = 7777
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def wait(self, timeout=None):
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                # First wait (after SIGTERM): pretend the process is stubborn.
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout or 5)
            self._alive = False
            return 0

        def terminate(self):
            sent.append(("terminate",))

        def kill(self):
            sent.append(("kill",))
            self._alive = False

    monkeypatch.setattr("src.orchestration.runtime.os.getpgid", lambda pid: pid)
    monkeypatch.setattr(
        "src.orchestration.runtime.os.killpg",
        lambda pgid, sig: sent.append(("killpg", pgid, sig)),
    )

    log_path = tmp_path / "log.txt"
    log_file = log_path.open("w")
    managed = ManagedProcess("frontend", FakeProc(), log_path, log_file)

    await stop_process(managed)

    assert ("killpg", 7777, signal.SIGTERM) in sent
    assert ("killpg", 7777, runtime.FORCE_KILL_SIGNAL) in sent


@pytest.mark.anyio
async def test_stop_process_falls_back_to_direct_child_on_windows(monkeypatch, tmp_path: Path):
    sent: list[tuple] = []

    class FakeProc:
        def __init__(self):
            self.pid = 9001
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def wait(self, timeout=None):
            self._alive = False
            return 0

        def terminate(self):
            sent.append(("terminate",))
            self._alive = False

        def kill(self):
            sent.append(("kill",))
            self._alive = False

    monkeypatch.setattr(runtime, "IS_WINDOWS", True)

    log_path = tmp_path / "log.txt"
    log_file = log_path.open("w")
    managed = ManagedProcess("frontend", FakeProc(), log_path, log_file)

    await stop_process(managed)

    assert sent == [("terminate",)]


# --- dev server env must not leak API keys / tokens ---


def test_start_process_strips_sensitive_env_vars(monkeypatch, tmp_path: Path):
    captured: dict = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["env"] = kwargs.get("env")
            self.pid = 1

        def poll(self):
            return None

    # Pretend the harness was started with a typical .env loaded.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("EVALUATOR_VISION_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    monkeypatch.setenv("MY_PASSWORD", "pw")
    monkeypatch.setenv("DB_PASSPHRASE", "pp")
    monkeypatch.setenv("MY_CREDENTIAL", "cred")
    # Benign variables must pass through.
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("FORCE_COLOR", "1")

    monkeypatch.setattr("src.orchestration.runtime.subprocess.Popen", FakePopen)

    log_path = tmp_path / "x.log"
    start_process(name="frontend", command=["echo", "x"], cwd=tmp_path, log_path=log_path)

    env = captured["env"]
    assert env is not None
    for blocked in (
        "ANTHROPIC_API_KEY",
        "EVALUATOR_VISION_API_KEY",
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "MY_PASSWORD",
        "DB_PASSPHRASE",
        "MY_CREDENTIAL",
    ):
        assert blocked not in env, f"{blocked} must be stripped from dev server env"
    for kept in ("PATH", "CI", "TZ", "FORCE_COLOR"):
        assert kept in env, f"{kept} must remain available to the dev server"
    assert env["PYTHONUNBUFFERED"] == "1"
