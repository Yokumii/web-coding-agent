from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.error import URLError
from urllib.request import urlopen

from src.config import HarnessConfig
from src.utils.logger import get_logger

logger = get_logger(__name__)

HOST = "127.0.0.1"


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[str]
    log_path: Path
    log_file: TextIO


@dataclass
class RunningAppStack:
    frontend_url: str
    processes: list[ManagedProcess]

    async def close(self) -> None:
        for managed in reversed(self.processes):
            await stop_process(managed)


def build_frontend_command(frontend_dir: Path, port: int) -> list[str]:
    if (frontend_dir / "pnpm-lock.yaml").exists():
        return ["pnpm", "dev", "--host", HOST, "--port", str(port), "--strictPort"]
    if (frontend_dir / "yarn.lock").exists():
        return ["yarn", "dev", "--host", HOST, "--port", str(port), "--strictPort"]
    return ["npm", "run", "dev", "--", "--host", HOST, "--port", str(port), "--strictPort"]


def find_listening_pids(port: int) -> list[int]:
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("`lsof` is required to inspect occupied frontend ports") from exc

    if result.returncode not in {0, 1}:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Failed to inspect port {port} with lsof: {stderr}")

    pids: list[int] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.isdigit():
            pids.append(int(stripped))
    return pids


def wait_for_port_release(port: int, timeout_secs: float = 5.0, poll_interval: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if not find_listening_pids(port):
            return True
        time.sleep(poll_interval)
    return not find_listening_pids(port)


def _terminate_pids(port: int, pids: list[int], sig: signal.Signals) -> None:
    signal_name = signal.Signals(sig).name
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise RuntimeError(
                f"Insufficient permission to stop PID {pid} occupying frontend port {port}"
            ) from exc
    logger.warning(
        f"[bold yellow]Port {port} occupied[/] — sent {signal_name} to PIDs: "
        + ", ".join(str(pid) for pid in pids)
    )


def ensure_port_available(port: int) -> None:
    pids = find_listening_pids(port)
    if not pids:
        return

    _terminate_pids(port, pids, signal.SIGTERM)
    if wait_for_port_release(port):
        return

    remaining_pids = find_listening_pids(port)
    if remaining_pids:
        _terminate_pids(port, remaining_pids, signal.SIGKILL)
        if wait_for_port_release(port):
            return

    stubborn_pids = find_listening_pids(port)
    pid_summary = ", ".join(str(pid) for pid in stubborn_pids) or "unknown"
    raise RuntimeError(f"Frontend port {port} is still occupied after termination attempts: {pid_summary}")

async def start_app_stack(
    workdir: Path,
    harness_dir: Path,
    config: HarnessConfig,
    round_num: int,
) -> RunningAppStack:
    frontend_dir = workdir / "frontend"
    if not frontend_dir.exists():
        raise FileNotFoundError(f"Frontend directory not found: {frontend_dir}")

    logs_dir = harness_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    processes: list[ManagedProcess] = []
    try:
        # ensure_port_available may sleep up to several seconds while waiting
        # for a stale dev server to release the port. Run it in a worker thread
        # so it does not block the async event loop (M2).
        await asyncio.to_thread(ensure_port_available, config.frontend_port)
        frontend = start_process(
            name="frontend",
            command=build_frontend_command(frontend_dir, config.frontend_port),
            cwd=frontend_dir,
            log_path=logs_dir / f"frontend_round_{round_num}.log",
        )
        processes.append(frontend)
        await wait_for_http(
            name="frontend",
            url=f"http://{HOST}:{config.frontend_port}",
            managed=frontend,
        )
    except Exception:
        stack = RunningAppStack(
            frontend_url=f"http://{HOST}:{config.frontend_port}",
            processes=processes,
        )
        await stack.close()
        raise

    logger.info(f"[bold]App stack ready[/] — frontend: http://{HOST}:{config.frontend_port}")
    return RunningAppStack(
        frontend_url=f"http://{HOST}:{config.frontend_port}",
        processes=processes,
    )


def start_process(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    log_path: Path,
) -> ManagedProcess:
    log_file = log_path.open("w", encoding="utf-8")
    env = _build_subprocess_env()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        # New POSIX session so we own the process group and stop_process
        # can SIGTERM/SIGKILL the whole tree (vite/esbuild/worker children
        # of `pnpm dev` would otherwise orphan; see audit H7).
        start_new_session=True,
    )
    logger.info(f"[bold]Starting {name}[/] — {' '.join(command)}")
    return ManagedProcess(name=name, process=process, log_path=log_path, log_file=log_file)


# Tokens whose presence anywhere in an env var name should keep that
# variable out of the dev server's environment (audit H2).
_SENSITIVE_ENV_TOKENS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSPHRASE",
    "CREDENTIAL",
)
# Provider-specific prefixes that are always blocked. Anything starting
# with one of these is treated as sensitive even if the suffix doesn't
# match the token list (e.g. ANTHROPIC_BASE_URL is not "secret" by name
# but still tells an attacker which endpoint to talk to).
_SENSITIVE_ENV_PREFIXES = (
    "ANTHROPIC_",
    "OPENAI_",
    "AWS_",
    "AZURE_",
    "GOOGLE_",
    "GH_",
    "GITHUB_",
)


def _is_sensitive_env_name(name: str) -> bool:
    upper = name.upper()
    if any(token in upper for token in _SENSITIVE_ENV_TOKENS):
        return True
    if any(upper.startswith(prefix) for prefix in _SENSITIVE_ENV_PREFIXES):
        return True
    return False


def _build_subprocess_env() -> dict[str, str]:
    """Strip secrets out of os.environ before handing it to the dev server.

    A frontend dev server is fully under the generator's control. Vite's
    define plugin or any third-party plugin can inline ``process.env``
    into the bundle, after which an evaluator screenshot or
    visual_capture HTML dump would exfiltrate the secret. We therefore
    drop any env var whose name looks key/token/secret-shaped (audit
    H2). The list is a deny-pattern rather than an allowlist because an
    allowlist breaks legit npm scripts that depend on locale, proxy,
    editor, or CI signals.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if not _is_sensitive_env_name(name)
    }
    env["PYTHONUNBUFFERED"] = "1"
    return env


async def wait_for_http(
    *,
    name: str,
    url: str,
    managed: ManagedProcess,
    timeout_secs: float = 90.0,
    ready_statuses: range = range(200, 300),
) -> None:
    deadline = time.monotonic() + timeout_secs
    last_error = "service did not respond"

    while time.monotonic() < deadline:
        return_code = managed.process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"{name} exited early with code {return_code}. See log: {managed.log_path}"
            )

        try:
            status_code = await asyncio.to_thread(fetch_status_code, url)
            if status_code in ready_statuses:
                return
            last_error = f"HTTP {status_code}"
        except Exception as exc:
            last_error = str(exc)

        await asyncio.sleep(1)

    raise TimeoutError(f"Timed out waiting for {name} at {url}: {last_error}")


def fetch_status_code(url: str) -> int:
    try:
        with urlopen(url, timeout=2) as response:
            return getattr(response, "status", 200)
    except URLError as exc:  # pragma: no cover - thin wrapper around stdlib
        raise RuntimeError(str(exc)) from exc


async def stop_process(managed: ManagedProcess) -> None:
    process = managed.process
    try:
        if process.poll() is None:
            _signal_process_tree(process, signal.SIGTERM)
            try:
                await asyncio.to_thread(process.wait, 5)
            except subprocess.TimeoutExpired:
                _signal_process_tree(process, signal.SIGKILL)
                await asyncio.to_thread(process.wait, 5)
    finally:
        managed.log_file.close()


def _signal_process_tree(process: subprocess.Popen[str], sig: int) -> None:
    """Signal the leader's whole process group.

    `start_process` launches in a new POSIX session so the process and
    its descendants share a process group (the leader's PID). Signalling
    the group cleans up children that `pnpm dev` / `npm run dev`
    routinely fork (vite, esbuild, workers). Falls back to signalling
    just the leader if we lost the group somehow.
    """
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, OSError):
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except (ProcessLookupError, PermissionError):
            pass

    try:
        if sig == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass
