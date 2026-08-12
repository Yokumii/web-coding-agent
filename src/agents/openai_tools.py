from __future__ import annotations

import asyncio
import json
import re
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.utils.bash_policy import validate_bash_command, validate_bash_command_readonly


@dataclass
class ToolResult:
    ok: bool
    output: str
    changed: bool = False


class OpenAIToolExecutor:
    def __init__(self, *, workdir: Path, allow_bash: bool, allow_playwright: bool = False,
                 bash_profile: str = "full", frontend_port: int = 5173,
                 command_timeout: float = 120, mutation_policy: Any | None = None):
        self.workdir = workdir.resolve()
        self.allow_bash = allow_bash
        self.allow_playwright = allow_playwright
        self.bash_profile = bash_profile
        self.frontend_port = frontend_port
        self.command_timeout = command_timeout
        self.mutation_policy = mutation_policy
        self._playwright = None
        self._browser_instance = None
        self._page = None

    def _is_forward_static_seed(self) -> bool:
        """Return whether this forward case originated as a plain static site."""
        manifest = self.workdir / "seed_manifest.json"
        if not manifest.is_file():
            return False
        try:
            source = Path(json.loads(manifest.read_text(encoding="utf-8"))["source_frontend"])
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return (source / "index.html").is_file() and not (source / "package.json").is_file()

    def _validate_static_seed_write(self, path: Path) -> None:
        """Keep a static seed dependency-free instead of changing its project class."""
        if not self._is_forward_static_seed():
            return
        relative = path.relative_to(self.workdir).as_posix()
        forbidden = {
            "frontend/package.json", "frontend/server.js", "frontend/package-lock.json",
            "frontend/pnpm-lock.yaml", "frontend/yarn.lock",
        }
        if relative in forbidden:
            raise ValueError(
                "This forward seed is a plain static site. Do not add package managers, "
                "lockfiles, or a custom server; edit its existing HTML/CSS/JS instead."
            )

    async def close(self) -> None:
        if self._browser_instance is not None:
            await self._browser_instance.close()
        if self._playwright is not None:
            await self._playwright.stop()

    def _path(self, value: str) -> Path:
        path = (self.workdir / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        try:
            path.relative_to(self.workdir)
        except ValueError as exc:
            raise ValueError(f"path escapes workdir: {value}") from exc
        return path

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        if self.mutation_policy is not None:
            denial = self.mutation_policy.check(name, args)
            if denial:
                return ToolResult(False, str(denial))
        result = await self._execute_unobserved(name, args)
        if self.mutation_policy is not None:
            self.mutation_policy.observe_result(
                name, args, ok=result.ok, output=result.output
            )
        return result

    async def _execute_unobserved(
        self, name: str, args: dict[str, Any]
    ) -> ToolResult:
        try:
            if name == "read_file":
                content = self._path(args["path"]).read_text(errors="replace")
                start_line = args.get("start_line")
                end_line = args.get("end_line")
                if start_line is not None or end_line is not None:
                    start = max(1, int(start_line or 1))
                    end = max(start, int(end_line or start + 199))
                    content = "".join(content.splitlines(keepends=True)[start - 1:end])
                # A page's primary source file often needs one coherent read
                # before a model can make a safe localized patch.  This stays
                # well below the previous unbounded response while avoiding a
                # counterproductive sequence of dozens of tiny range reads.
                max_chars = 32_000
                if len(content) > max_chars:
                    content = (
                        content[:max_chars]
                        + "\n\n[read_file truncated at 32000 characters; use start_line/end_line "
                        "or an allowlisted sed command to inspect another focused range.]\n"
                    )
                return ToolResult(True, content)
            if name == "write_file":
                path = self._path(args["path"]); before = path.read_text(errors="replace") if path.exists() else None
                self._validate_static_seed_write(path)
                path.parent.mkdir(parents=True, exist_ok=True); path.write_text(args["content"])
                return ToolResult(True, f"wrote {args['path']}", before != args["content"])
            if name == "apply_patch":
                path = self._path(args["path"]); content = path.read_text()
                self._validate_static_seed_write(path)
                old = args["old_text"]
                if old not in content: raise ValueError("old_text not found")
                path.write_text(content.replace(old, args["new_text"], 1))
                return ToolResult(True, f"patched {args['path']}", True)
            if name == "list_files":
                base = self._path(args.get("path", ".")); pattern = args.get("glob", "**/*")
                return ToolResult(True, "\n".join(str(p.relative_to(self.workdir)) for p in list(base.glob(pattern))[:1000]))
            if name == "search_files":
                base = self._path(args.get("path", "."))
                proc = await asyncio.create_subprocess_exec("rg", "-n", "--", args["query"], str(base), cwd=self.workdir,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(proc.communicate(), self.command_timeout)
                return ToolResult(proc.returncode in {0, 1}, out.decode(errors="replace")[:100_000])
            if name == "run_command":
                if not self.allow_bash: raise ValueError("Bash tool is disabled")
                # stdout/stderr are already combined by the executor. Drop only these
                # redundant, semantically inert redirections while continuing to reject
                # every file-writing redirection in the shared Bash policy.
                command = re.sub(r"\s+2>&1(?:\s*$)", "", args["command"])
                command = re.sub(r"\s+2>/dev/null(?:\s*$)", "", command)
                (validate_bash_command_readonly if self.bash_profile == "read_only" else validate_bash_command)(command)
                proc = await asyncio.create_subprocess_shell(command, cwd=self.workdir,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True)
                try: out, _ = await asyncio.wait_for(proc.communicate(), min(float(args.get("timeout_seconds", self.command_timeout)), self.command_timeout))
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await asyncio.wait_for(proc.wait(), 5)
                    raise ValueError("command timed out")
                return ToolResult(proc.returncode == 0, out.decode(errors="replace")[-100_000:], True)
            if name.startswith("browser_"):
                return await self._browser(name, args)
            raise ValueError(f"unknown tool: {name}")
        except Exception as exc:
            return ToolResult(False, f"{type(exc).__name__}: {exc}")

    async def _browser(self, name: str, args: dict[str, Any]) -> ToolResult:
        if not self.allow_playwright: raise ValueError("browser tools are disabled")
        from urllib.parse import urlparse
        url = args.get("url", f"http://localhost:{self.frontend_port}")
        parsed = urlparse(url)
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or (parsed.port and parsed.port != self.frontend_port):
            raise ValueError("browser URL must use the configured localhost frontend port")
        if name == "browser_screenshot":
            args = dict(args)
            args["path"] = str(self._path(args["path"]))
        if self._page is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            from src.utils.playwright_browser import launch_chromium
            self._browser_instance = await launch_chromium(self._playwright, headless=True)
            self._page = await self._browser_instance.new_page()
        # Preserve browser state between tool calls. Models normally repeat the
        # current URL on click/snapshot/evaluate; navigating again here would
        # reload the SPA and erase the very interaction state being tested.
        current_url = self._page.url.rstrip("/")
        requested_url = url.rstrip("/")
        if self._page.url == "about:blank" or current_url != requested_url:
            await self._page.goto(url, wait_until="networkidle", timeout=int(self.command_timeout * 1000))
        if name == "browser_click":
            await self._page.click(args["selector"], force=bool(args.get("force", False)))
            return ToolResult(True, "clicked")
        if name == "browser_set_viewport":
            width = int(args["width"])
            height = int(args["height"])
            if not (240 <= width <= 3840 and 240 <= height <= 2160):
                raise ValueError("viewport dimensions must be within 240..3840 by 240..2160")
            await self._page.set_viewport_size({"width": width, "height": height})
            await self._page.wait_for_timeout(100)
            return ToolResult(True, f"viewport set to {width}x{height}")
        if name == "browser_fill":
            await self._page.fill(args["selector"], args["value"])
            return ToolResult(True, "filled")
        if name == "browser_key_press":
            count = int(args.get("count", 1))
            if not 1 <= count <= 30:
                raise ValueError("keyboard count must be between 1 and 30")
            for _ in range(count):
                await self._page.keyboard.press(str(args["key"]))
            return ToolResult(True, f"pressed {args['key']} x{count}")
        if name == "browser_screenshot":
            position = args.get("position")
            if position in {"top", "middle", "bottom"}:
                max_scroll = await self._page.evaluate(
                    "Math.max(0, document.documentElement.scrollHeight - window.innerHeight)"
                )
                target = {"top": 0, "middle": max_scroll / 2, "bottom": max_scroll}[position]
                await self._page.evaluate("y => window.scrollTo(0, y)", target)
                await self._page.wait_for_timeout(150)
            await self._page.screenshot(path=args["path"], full_page=args.get("full_page", False))
            return ToolResult(True, f"saved {args['path']}", True)
        if name == "browser_evaluate":
            return ToolResult(True, json.dumps(await self._page.evaluate(args["expression"]), ensure_ascii=False))
        return ToolResult(True, (await self._page.locator("body").inner_text())[:50_000])


def openai_tool_schemas(*, allow_bash: bool, allow_playwright: bool) -> list[dict[str, Any]]:
    specs = [
        ("read_file", "Read a UTF-8 text file. Large output is capped; use line bounds for focused inspection.", {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
        ("write_file", "Create or overwrite a text file", {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        ("apply_patch", "Replace one exact text occurrence", {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, ["path", "old_text", "new_text"]),
        ("list_files", "List files", {"path": {"type": "string"}, "glob": {"type": "string"}}, []),
        ("search_files", "Search file contents", {"query": {"type": "string"}, "path": {"type": "string"}}, ["query"]),
    ]
    if allow_bash: specs.append(("run_command", "Run an allowlisted foreground command", {"command": {"type": "string"}, "timeout_seconds": {"type": "number"}}, ["command"]))
    if allow_playwright:
        specs += [
            ("browser_snapshot", "Open a local URL and return visible text", {"url": {"type": "string"}}, ["url"]),
            ("browser_set_viewport", "Set the browser viewport for responsive checks", {"url": {"type": "string"}, "width": {"type": "integer"}, "height": {"type": "integer"}}, ["url", "width", "height"]),
            ("browser_click", "Open a local URL and click a selector; use force only after confirming the target is visible but an unrelated overlay intercepts it", {"url": {"type": "string"}, "selector": {"type": "string"}, "force": {"type": "boolean"}}, ["url", "selector"]),
            ("browser_fill", "Open a local URL and fill a selector", {"url": {"type": "string"}, "selector": {"type": "string"}, "value": {"type": "string"}}, ["url", "selector", "value"]),
            ("browser_key_press", "Send a keyboard key to the active page. Use count to send repeated keys efficiently, for example Tab x16.", {"url": {"type": "string"}, "key": {"type": "string"}, "count": {"type": "integer"}}, ["url", "key"]),
            ("browser_screenshot", "Screenshot a local URL", {"url": {"type": "string"}, "path": {"type": "string"}, "full_page": {"type": "boolean"}, "position": {"type": "string", "enum": ["top", "middle", "bottom"]}}, ["url", "path"]),
            ("browser_evaluate", "Evaluate JavaScript on a local page", {"url": {"type": "string"}, "expression": {"type": "string"}}, ["url", "expression"]),
        ]
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": {"type": "object", "properties": p, "required": r}}} for n,d,p,r in specs]
