#!/usr/bin/env python3
"""Run one real DashScope chat-completions probe without printing credentials."""
from __future__ import annotations

import argparse
import json
import os

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    base_url = os.environ["OPENAI_AGENT_BASE_URL"].rstrip("/")
    api_key = os.environ["OPENAI_AGENT_API_KEY"]
    payload = {
        "model": os.environ.get("GENERATOR_MODEL", "qwen3.6-plus"),
        "messages": [{"role": "user", "content": "Reply with exactly PONG."}],
        "enable_thinking": False,
        "stream": args.stream,
    }
    with httpx.Client(timeout=60, verify=os.getenv("SSL_NO_VERIFY") != "1") as client:
        if not args.stream:
            response = client.post(
                base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"}, json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"].get("content", "")
            print(json.dumps({"status": "ok", "stream": False, "content": content.strip()}))
            return
        with client.stream(
            "POST", base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"}, json=payload,
        ) as response:
            response.raise_for_status()
            chunks = []
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                event = json.loads(data)
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {}).get("content")
                if delta:
                    chunks.append(delta)
            print(json.dumps({"status": "ok", "stream": True, "content": "".join(chunks).strip()}))


if __name__ == "__main__":
    main()
