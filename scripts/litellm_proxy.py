#!/usr/bin/env python3
"""轻量 Anthropic → OpenAI 翻译代理。

Claude Code SDK 发 Anthropic Messages 格式请求到本地代理，
代理用 LiteLLM 翻译后转发到 OpenAI 兼容端点（如 ppapi.ai/qwen）。

用法:
  python scripts/litellm_proxy.py --port 4000 \
    --model openai/qwen3.7-max \
    --api-key sk-xxx \
    --api-base https://app-hk.ppapi.ai/v1
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.utils.llm_client import completion, LLMClientError


class AnthropicProxyHandler(BaseHTTPRequestHandler):
    model: str = ""
    api_key: str = ""
    api_base: str = ""

    def do_POST(self):
        if "/v1/messages" not in self.path:
            self._error(404, f"not found: {self.path}")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        messages = self._convert_messages(body)
        model = self.__class__.model
        max_tokens = body.get("max_tokens", 4096)
        temperature = body.get("temperature", 0.0)

        try:
            result = completion(
                messages=messages,
                model=model,
                api_key=self.__class__.api_key,
                api_base=self.__class__.api_base,
                max_tokens=max_tokens,
                temperature=temperature,
                timeout=300.0,
            )
        except LLMClientError as e:
            self._error(502, str(e))
            return

        response = {
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
        }

        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _convert_messages(self, body: dict) -> list[dict[str, Any]]:
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
        return messages

    def _error(self, code: int, message: str):
        payload = json.dumps({"error": {"type": "proxy_error", "message": message}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print(f"[proxy] {fmt % args}", file=sys.stderr)


def run_proxy(port: int, model: str, api_key: str, api_base: str):
    AnthropicProxyHandler.model = model
    AnthropicProxyHandler.api_key = api_key
    AnthropicProxyHandler.api_base = api_base

    server = HTTPServer(("127.0.0.1", port), AnthropicProxyHandler)
    print(f"Anthropic→OpenAI proxy on http://127.0.0.1:{port}", file=sys.stderr)
    print(f"  model:    {model}", file=sys.stderr)
    print(f"  api_base: {api_base}", file=sys.stderr)
    print(f"  使用方式: ANTHROPIC_BASE_URL=http://127.0.0.1:{port}", file=sys.stderr)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Anthropic→OpenAI 翻译代理")
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument("--model", default="openai/qwen3.7-max")
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--api-base", default="https://app-hk.ppapi.ai/v1")
    args = parser.parse_args()
    run_proxy(args.port, args.model, args.api_key, args.api_base)


if __name__ == "__main__":
    main()
