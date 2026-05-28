#!/usr/bin/env python3
"""并发批量执行 harness，每个 prompt 独立 workdir + 端口。

支持 --proxy 模式：自动启动本地 Anthropic→OpenAI 翻译代理，
使 Claude Code SDK 能通过 qwen 等 OpenAI 兼容模型运行。

输入格式:
  JSONL — 每行 {"prompt": "...", "workdir": "name"}  (workdir 可选)
  纯文本 — 每行一个 prompt

用法:
  # 用默认 Anthropic API
  python scripts/run_batch.py prompts.jsonl --workers 4

  # 用 qwen 模型（通过本地代理）
  python scripts/run_batch.py prompts.jsonl --workers 2 \
    --proxy --proxy-model openai/qwen3.7-max \
    --proxy-key sk-xxx --proxy-base https://app-hk.ppapi.ai/v1 \
    -- --max-rounds 5 --max-budget 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import HarnessConfig
from src.orchestration.harness import run_harness
from src.utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Anthropic → OpenAI 翻译代理
# ---------------------------------------------------------------------------

def _make_proxy_handler(model: str, api_key: str, api_base: str):
    from src.utils.llm_client import completion, LLMClientError

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if "/v1/messages" not in self.path:
                self._reply(404, {"error": {"message": f"not found: {self.path}"}})
                return

            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            messages: list[dict[str, Any]] = []
            if body.get("system"):
                sys_content = body["system"]
                if isinstance(sys_content, list):
                    sys_content = "\n".join(
                        b.get("text", "") for b in sys_content if isinstance(b, dict)
                    )
                messages.append({"role": "system", "content": sys_content})
            for msg in body.get("messages", []):
                messages.append({"role": msg["role"], "content": msg["content"]})

            try:
                result = completion(
                    messages=messages,
                    model=model,
                    api_key=api_key,
                    api_base=api_base,
                    max_tokens=body.get("max_tokens", 4096),
                    temperature=body.get("temperature", 0.0),
                    timeout=300.0,
                )
            except LLMClientError as e:
                self._reply(502, {"error": {"message": str(e)}})
                return

            self._reply(200, {
                "id": "msg_proxy",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": result.text}],
                "model": model,
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": result.usage.get("prompt_tokens", result.usage.get("input_tokens", 0)),
                    "output_tokens": result.usage.get("completion_tokens", result.usage.get("output_tokens", 0)),
                },
            })

        def _reply(self, code: int, data: dict):
            payload = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            pass  # suppress per-request logging

    return Handler


def start_proxy(port: int, model: str, api_key: str, api_base: str) -> HTTPServer:
    handler = _make_proxy_handler(model, api_key, api_base)
    server = HTTPServer(("127.0.0.1", port), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"[bold]Proxy started[/] http://127.0.0.1:{port} → {model} @ {api_base}")
    return server


# ---------------------------------------------------------------------------
# 输入解析
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^\w一-鿿]+", "-", text.strip().lower())
    slug = slug.strip("-")
    return slug[:max_len] or "task"


def parse_input_file(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    tasks: list[dict[str, str]] = []
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "prompt" in obj:
                tasks.append({
                    "prompt": obj["prompt"],
                    "workdir": obj.get("workdir", ""),
                })
                continue
        except json.JSONDecodeError:
            pass
        tasks.append({"prompt": line, "workdir": ""})
    return tasks


# ---------------------------------------------------------------------------
# 单任务 / 批量执行
# ---------------------------------------------------------------------------

def build_config_for_task(
    base_config: dict[str, Any],
    frontend_port: int,
) -> HarnessConfig:
    overrides = dict(base_config)
    overrides["frontend_port"] = frontend_port
    return HarnessConfig(**overrides)


async def run_single(
    index: int,
    prompt: str,
    workdir: Path,
    config: HarnessConfig,
    semaphore: asyncio.Semaphore,
    harness_kwargs: dict[str, Any],
) -> dict[str, Any]:
    async with semaphore:
        logger.info(f"[{index}] 开始: {prompt[:60]}... → {workdir.name} (port {config.frontend_port})")
        start = time.time()
        try:
            await run_harness(prompt, workdir, config, **harness_kwargs)
            elapsed = time.time() - start
            state = _read_final_state(workdir)
            logger.info(f"[{index}] 完成: {workdir.name} ({elapsed:.0f}s)")
            return {
                "index": index,
                "prompt": prompt[:200],
                "workdir": str(workdir),
                "status": "ok",
                "duration_s": round(elapsed, 1),
                "cost_usd": state.get("total_cost", 0),
                "accepted_sprints": len(state.get("accepted_sprints", [])),
            }
        except Exception as e:
            elapsed = time.time() - start
            logger.error(f"[{index}] 失败: {workdir.name} — {e}")
            return {
                "index": index,
                "prompt": prompt[:200],
                "workdir": str(workdir),
                "status": "error",
                "error": str(e),
                "duration_s": round(elapsed, 1),
            }


def _read_final_state(workdir: Path) -> dict[str, Any]:
    state_path = workdir / ".harness" / "harness_state.json"
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


async def run_batch(
    tasks: list[dict[str, str]],
    output_dir: Path,
    workers: int,
    base_port: int,
    config_overrides: dict[str, Any],
    harness_kwargs: dict[str, Any],
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(workers)

    coros = []
    for i, task in enumerate(tasks):
        prompt = task["prompt"]
        workdir_name = task["workdir"] or f"{i:03d}_{_slugify(prompt)}"
        workdir = output_dir / workdir_name
        config = build_config_for_task(config_overrides, base_port + i)

        coros.append(run_single(i, prompt, workdir, config, semaphore, harness_kwargs))

    results = await asyncio.gather(*coros)
    return list(results)


# ---------------------------------------------------------------------------
# CLI 参数解析
# ---------------------------------------------------------------------------

def parse_harness_args(extra_args: list[str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    i = 0
    while i < len(extra_args):
        arg = extra_args[i]
        if arg == "--max-rounds" and i + 1 < len(extra_args):
            overrides["max_rounds"] = int(extra_args[i + 1])
            i += 2
        elif arg == "--max-budget" and i + 1 < len(extra_args):
            overrides["max_budget_usd"] = float(extra_args[i + 1])
            i += 2
        elif arg == "--planner-model" and i + 1 < len(extra_args):
            overrides["planner_model"] = extra_args[i + 1]
            i += 2
        elif arg == "--generator-model" and i + 1 < len(extra_args):
            overrides["generator_model"] = extra_args[i + 1]
            i += 2
        elif arg == "--evaluator-model" and i + 1 < len(extra_args):
            overrides["evaluator_model"] = extra_args[i + 1]
            i += 2
        elif arg == "--design-mode" and i + 1 < len(extra_args):
            overrides["design_mode"] = extra_args[i + 1]
            i += 2
        elif arg == "--playwright-headless":
            overrides["playwright_headless"] = True
            i += 1
        elif arg == "--no-playwright-headless":
            overrides["playwright_headless"] = False
            i += 1
        elif arg == "--plan-only":
            overrides["_plan_only"] = True
            i += 1
        else:
            print(f"[WARN] 未知 harness 参数: {arg}", file=sys.stderr)
            i += 1
    return overrides


def main() -> None:
    parser = argparse.ArgumentParser(
        description="并发批量执行 harness",
        usage="%(prog)s INPUT [options] [-- harness_args...]",
    )
    parser.add_argument("input", type=Path, help="JSONL 或纯文本文件，每行一个 prompt")
    parser.add_argument("-w", "--workers", type=int, default=4, help="最大并发数 (默认 4)")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("batch_output"), help="输出根目录")
    parser.add_argument("--base-port", type=int, default=5200, help="起始端口号 (默认 5200)")

    proxy_group = parser.add_argument_group("proxy", "Anthropic→OpenAI 翻译代理 (用第三方模型)")
    proxy_group.add_argument("--proxy", action="store_true", help="启动本地翻译代理")
    proxy_group.add_argument("--proxy-port", type=int, default=4000, help="代理端口 (默认 4000)")
    proxy_group.add_argument("--proxy-model", default="openai/qwen3.7-max", help="LiteLLM 模型标识")
    proxy_group.add_argument("--proxy-key", default="", help="第三方 API key")
    proxy_group.add_argument("--proxy-base", default="https://app-hk.ppapi.ai/v1", help="第三方 API base URL")

    args, extra = parser.parse_known_args()
    if extra and extra[0] == "--":
        extra = extra[1:]

    tasks = parse_input_file(args.input)
    if not tasks:
        print("没有发现有效的 prompt，退出", file=sys.stderr)
        sys.exit(1)

    harness_overrides = parse_harness_args(extra)
    plan_only = harness_overrides.pop("_plan_only", False)

    harness_kwargs: dict[str, Any] = {}
    if plan_only:
        harness_kwargs["plan_only"] = True

    proxy_server = None
    if args.proxy:
        proxy_server = start_proxy(args.proxy_port, args.proxy_model, args.proxy_key, args.proxy_base)
        harness_overrides["api_key"] = "proxy-key"
        harness_overrides["base_url"] = f"http://127.0.0.1:{args.proxy_port}"

    print(f"共 {len(tasks)} 个任务，最大并发 {args.workers}，端口 {args.base_port}-{args.base_port + len(tasks) - 1}", file=sys.stderr)
    print(f"输出目录: {args.output_dir.resolve()}", file=sys.stderr)
    if args.proxy:
        print(f"代理: http://127.0.0.1:{args.proxy_port} → {args.proxy_model}", file=sys.stderr)
    if harness_overrides:
        safe = {k: v for k, v in harness_overrides.items() if "key" not in k.lower()}
        print(f"Harness 参数: {safe}", file=sys.stderr)

    start = time.time()
    try:
        results = asyncio.run(run_batch(
            tasks=tasks,
            output_dir=args.output_dir.resolve(),
            workers=args.workers,
            base_port=args.base_port,
            config_overrides=harness_overrides,
            harness_kwargs=harness_kwargs,
        ))
    finally:
        if proxy_server:
            proxy_server.shutdown()

    elapsed = time.time() - start
    success = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "error")
    total_cost = sum(r.get("cost_usd", 0) for r in results)

    summary = {
        "total": len(results),
        "success": success,
        "failed": failed,
        "total_cost_usd": round(total_cost, 2),
        "total_duration_s": round(elapsed, 1),
        "results": results,
    }

    result_path = args.output_dir / "batch_result.json"
    result_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'=' * 50}", file=sys.stderr)
    print(f"批量执行完成: {success}/{len(results)} 成功, {failed} 失败", file=sys.stderr)
    print(f"总耗时: {elapsed / 60:.1f} min, 总成本: ${total_cost:.2f}", file=sys.stderr)
    print(f"结果: {result_path}", file=sys.stderr)

    for r in results:
        status = "✓" if r["status"] == "ok" else "✗"
        line = f"  {status} [{r['index']}] {r['prompt'][:50]}"
        if r["status"] == "ok":
            line += f" — {r['duration_s']}s, ${r.get('cost_usd', 0):.2f}, {r.get('accepted_sprints', '?')} sprints"
        else:
            line += f" — {r.get('error', 'unknown')}"
        print(line, file=sys.stderr)


if __name__ == "__main__":
    main()
