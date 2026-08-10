#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time

import httpx

from src.task_generation.air_webcompass import parse_sse_content


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--trust-env", choices=("on", "off"), required=True)
    args = parser.parse_args()
    base_url = os.environ["AIR_API_BASE_URL"].rstrip("/")
    api_key = os.environ["AIR_API_KEY"]
    model = os.getenv("AIR_MODEL", "deepseek-chat")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly PONG."}],
        "max_tokens": 16,
        "temperature": 0,
        "stream": args.stream,
    }
    started = time.monotonic()
    with httpx.Client(
        timeout=httpx.Timeout(60),
        verify=os.getenv("SSL_NO_VERIFY") != "1",
        trust_env=args.trust_env == "on",
    ) as client:
        if args.stream:
            with client.stream(
                "POST", base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"}, json=payload,
            ) as response:
                response.raise_for_status()
                content = parse_sse_content(response.iter_lines())
        else:
            response = client.post(
                base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"}, json=payload,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
    print(json.dumps({
        "status": "ok",
        "model": model,
        "stream": args.stream,
        "trust_env": args.trust_env,
        "content": str(content).strip(),
        "duration_seconds": round(time.monotonic() - started, 3),
    }))


if __name__ == "__main__":
    main()

