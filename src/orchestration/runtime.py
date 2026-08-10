from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from src.config import HarnessConfig
from src.utils.logger import get_logger

logger = get_logger(__name__)

HOST = "127.0.0.1"
IS_WINDOWS = sys.platform == "win32"
FORCE_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


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


def _frontend_url(port: int) -> str:
    return f"http://{HOST}:{port}"


def build_frontend_command(frontend_dir: Path, port: int) -> list[str]:
    """根据锁文件类型选择前端开发命令。"""
    if _forward_seed_is_static(frontend_dir):
        return ["python3", "-m", "http.server", str(port), "--bind", HOST]
    if (frontend_dir / "pnpm-lock.yaml").exists():
        return ["pnpm", "dev", "--host", HOST, "--port", str(port), "--strictPort"]
    if (frontend_dir / "yarn.lock").exists():
        return ["yarn", "dev", "--host", HOST, "--port", str(port), "--strictPort"]
    # WebCompass includes plain HTML/CSS/JS projects without a Node manifest.
    # Serving those files directly keeps the forward harness on the same source
    # distribution as the reverse-built corpus.
    package_json = frontend_dir / "package.json"
    if (frontend_dir / "index.html").is_file() and _is_static_html_project(package_json):
        return ["python3", "-m", "http.server", str(port), "--bind", HOST]
    return ["npm", "run", "dev", "--", "--host", HOST, "--port", str(port), "--strictPort"]


def _is_static_html_project(package_json: Path) -> bool:
    """Return true for plain projects, including agent-added empty npm stubs."""
    if not package_json.is_file():
        return True
    try:
        package = json.loads(package_json.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return not package.get("dependencies") and not package.get("devDependencies")


def _forward_seed_is_static(frontend_dir: Path) -> bool:
    """Keep a forward edit's server choice anchored to its immutable seed."""
    manifest_path = frontend_dir.parent / "seed_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    source = Path(str(manifest.get("source_frontend") or ""))
    return source.is_dir() and (source / "index.html").is_file() and not (source / "package.json").is_file()


def find_listening_pids(port: int) -> list[int]:
    """查找当前监听指定端口的进程 PID。"""
    if IS_WINDOWS:
        return _find_listening_pids_windows(port)

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


def _find_listening_pids_windows(port: int) -> list[int]:
    """通过 netstat 查找 Windows 下监听指定端口的进程 PID。"""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("`netstat` is required to inspect occupied frontend ports") from exc

    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Failed to inspect port {port} with netstat: {stderr}")

    pids: list[int] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        protocol, local_address, _remote_address, state, pid_text = parts[:5]
        if protocol.upper() != "TCP" or state.upper() != "LISTENING":
            continue
        if ":" not in local_address or not pid_text.isdigit():
            continue
        if local_address.rsplit(":", 1)[-1] == str(port):
            pids.append(int(pid_text))
    return sorted(set(pids))


def wait_for_port_release(port: int, timeout_secs: float = 5.0, poll_interval: float = 0.2) -> bool:
    """轮询等待端口释放。"""
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        if not find_listening_pids(port):
            return True
        time.sleep(poll_interval)
    return not find_listening_pids(port)


def _terminate_pids(port: int, pids: list[int], sig: signal.Signals) -> None:
    """向占用端口的一组进程发送终止信号。"""
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
    """确保前端端口可用，必要时回收残留进程。"""
    pids = find_listening_pids(port)
    if not pids:
        return

    _terminate_pids(port, pids, signal.SIGTERM)
    if wait_for_port_release(port):
        return

    remaining_pids = find_listening_pids(port)
    if remaining_pids:
        _terminate_pids(port, remaining_pids, FORCE_KILL_SIGNAL)
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
    """启动前端开发服务，并等待 HTTP 探活成功。"""
    frontend_dir = workdir / "frontend"
    if not frontend_dir.exists():
        raise FileNotFoundError(f"Frontend directory not found: {frontend_dir}")

    logs_dir = harness_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    processes: list[ManagedProcess] = []
    frontend_url = _frontend_url(config.frontend_port)
    try:
        # 端口回收可能阻塞数秒，这里放到工作线程中执行。
        await asyncio.to_thread(ensure_port_available, config.frontend_port)
        frontend = start_process(
            name="frontend",
            command=build_frontend_command(frontend_dir, config.frontend_port),
            cwd=frontend_dir,
            log_path=logs_dir / f"frontend_round_{round_num}.log",
            env_overrides={"HOST": HOST, "PORT": str(config.frontend_port)},
        )
        processes.append(frontend)
        await wait_for_http(
            name="frontend",
            url=frontend_url,
            managed=frontend,
        )
    except Exception:
        stack = RunningAppStack(
            frontend_url=frontend_url,
            processes=processes,
        )
        await stack.close()
        raise

    logger.info(f"[bold]App stack ready[/] — frontend: {frontend_url}")
    return RunningAppStack(
        frontend_url=frontend_url,
        processes=processes,
    )


def start_process(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    log_path: Path,
    env_overrides: dict[str, str] | None = None,
) -> ManagedProcess:
    """以独立会话启动子进程，并将日志落到文件。"""
    log_file = log_path.open("w", encoding="utf-8")
    env = _build_subprocess_env()
    if env_overrides:
        env.update(env_overrides)
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
        # POSIX 下新建 session 以便按进程组整体回收；Windows 退化为主进程终止。
        start_new_session=not IS_WINDOWS,
    )
    logger.info(f"[bold]Starting {name}[/] — {' '.join(command)}")
    return ManagedProcess(name=name, process=process, log_path=log_path, log_file=log_file)


# 环境变量名中包含这些片段时，视为敏感信息并剔除。
_SENSITIVE_ENV_TOKENS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSPHRASE",
    "CREDENTIAL",
)
# 这些前缀也一律视为敏感信息，即使变量名本身没有出现 KEY/TOKEN 等字样。
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
    """判断环境变量名是否属于敏感信息。"""
    upper = name.upper()
    if any(token in upper for token in _SENSITIVE_ENV_TOKENS):
        return True
    if any(upper.startswith(prefix) for prefix in _SENSITIVE_ENV_PREFIXES):
        return True
    return False


def _build_subprocess_env() -> dict[str, str]:
    """构造交给前端开发服务的环境变量，主动剔除敏感项。"""
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
    """轮询目标地址，直到服务就绪或超时。"""
    deadline = time.monotonic() + timeout_secs
    last_error = "service did not respond"
    last_log_at = 0.0

    logger.info(f"[bold]Waiting for {name}[/] — {url}")

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

        now = time.monotonic()
        if now - last_log_at >= 5.0:
            remaining = max(0.0, deadline - now)
            logger.info(
                f"[dim]Waiting for {name}[/] — last_status={last_error}; "
                f"{remaining:.0f}s remaining"
            )
            last_log_at = now

        await asyncio.sleep(1)

    raise TimeoutError(f"Timed out waiting for {name} at {url}: {last_error}")


def fetch_status_code(url: str) -> int:
    """读取本地就绪探针的 HTTP 状态码，不继承外部代理设置。"""
    try:
        # The harness only probes its own loopback app.  Respecting HTTP_PROXY
        # here can route 127.0.0.1 through a corporate proxy and turn a healthy
        # app into a false startup timeout.
        opener = build_opener(ProxyHandler({}))
        with opener.open(url, timeout=2) as response:
            return getattr(response, "status", 200)
    except URLError as exc:  # pragma: no cover - thin wrapper around stdlib
        raise RuntimeError(str(exc)) from exc


async def stop_process(managed: ManagedProcess) -> None:
    """优雅停止子进程，必要时升级为强制终止。"""
    process = managed.process
    try:
        if process.poll() is None:
            _signal_process_tree(process, signal.SIGTERM)
            try:
                await asyncio.to_thread(process.wait, 5)
            except subprocess.TimeoutExpired:
                if IS_WINDOWS:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                else:
                    _signal_process_tree(process, FORCE_KILL_SIGNAL)
                await asyncio.to_thread(process.wait, 5)
    finally:
        managed.log_file.close()


def _signal_process_tree(process: subprocess.Popen[str], sig: int) -> None:
    """优先向整个进程组发信号，失败时回退到主进程。"""
    if IS_WINDOWS:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        return

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
        if sig == FORCE_KILL_SIGNAL:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        pass
