"""Translate ArtifactsBenchmark's model-marker transport to OpenAI chat completions."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from openai import OpenAI


def convert_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for message in messages:
        content = []
        for item in message.get("content", []):
            if item.get("type") == "text":
                content.append({"type": "text", "text": item.get("value", "")})
            elif item.get("type") == "image_url":
                content.append({"type": "image_url", "image_url": {"url": item.get("value", "")}})
        converted.append({"role": message.get("role", "user"), "content": content})
    return converted


def make_handler(base_url: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            timeout = float(request.get("timeout", 3000))
            response = OpenAI(
                api_key=request["api_key"],
                base_url=base_url,
                timeout=timeout,
            ).chat.completions.create(
                model=request["model_marker"],
                messages=convert_messages(request["messages"]),
                max_tokens=8192,
            )
            body = json.dumps(
                {"answer": [{"value": response.choices[0].message.content or ""}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.base_url)).serve_forever()


if __name__ == "__main__":
    main()
