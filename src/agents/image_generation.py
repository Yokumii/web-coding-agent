from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import request

from src.config import HarnessConfig


@dataclass(frozen=True)
class GeneratedImage:
    path: Path
    usage: dict[str, Any]


def _build_images_generations_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/images/generations"
    return f"{normalized}/v1/images/generations"


def _encode_image_reference(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _build_images_generations_payload(
    *,
    config: HarnessConfig,
    prompt: str,
    reference_images: list[Path] | None,
) -> dict[str, Any]:
    return {
        "model": config.design_image_model,
        "prompt": prompt,
        "image": [
            _encode_image_reference(path)
            for path in (reference_images or [])
        ],
        "size": config.design_image_size,
        "response_format": "url",
    }


def _extract_image_bytes(response_payload: dict[str, Any]) -> bytes:
    data = response_payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("image generation response did not contain data[0]")

    item = data[0]
    if isinstance(item.get("url"), str) and item["url"].strip():
        with request.urlopen(item["url"], timeout=90) as response:
            return response.read()
    if isinstance(item.get("b64_json"), str) and item["b64_json"].strip():
        return base64.b64decode(item["b64_json"])
    raise ValueError("image generation response did not include url or b64_json")


def _perform_image_generation_request(
    *,
    config: HarnessConfig,
    prompt: str,
    output_path: Path,
    reference_images: list[Path] | None = None,
) -> GeneratedImage:
    if not config.design_image_api_key:
        raise ValueError("missing design image API key")

    payload = _build_images_generations_payload(
        config=config,
        prompt=prompt,
        reference_images=reference_images,
    )
    http_request = request.Request(
        _build_images_generations_url(config.design_image_base_url),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {config.design_image_api_key}",
        },
        method="POST",
    )
    with request.urlopen(
        http_request,
        timeout=max(1, int(config.design_image_timeout_seconds)),
    ) as response:
        response_payload = json.loads(response.read().decode("utf-8"))

    image_bytes = _extract_image_bytes(response_payload)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_bytes)
    usage = response_payload.get("usage")
    return GeneratedImage(
        path=output_path,
        usage=usage if isinstance(usage, dict) else {},
    )


async def generate_image(
    *,
    config: HarnessConfig,
    prompt: str,
    output_path: Path,
    reference_images: list[Path] | None = None,
) -> GeneratedImage:
    return await asyncio.to_thread(
        _perform_image_generation_request,
        config=config,
        prompt=prompt,
        output_path=output_path,
        reference_images=reference_images,
    )
