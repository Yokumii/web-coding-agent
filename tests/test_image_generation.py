from __future__ import annotations

import base64
from pathlib import Path

from src.agents.image_generation import (
    _build_images_generations_payload,
    _build_images_generations_url,
    _extract_image_bytes,
)
from src.config import HarnessConfig


def test_build_images_generations_url_handles_base_and_v1_urls():
    assert (
        _build_images_generations_url("https://www.right.codes/draw")
        == "https://www.right.codes/draw/v1/images/generations"
    )
    assert (
        _build_images_generations_url("https://www.right.codes/draw/v1")
        == "https://www.right.codes/draw/v1/images/generations"
    )


def test_build_images_generations_payload_encodes_reference_images(tmp_path: Path):
    reference = tmp_path / "concept.png"
    reference.write_bytes(b"concept")

    payload = _build_images_generations_payload(
        config=HarnessConfig(
            design_image_model="gpt-image-2",
            design_image_size="1024x1024",
        ),
        prompt="Make a background.",
        reference_images=[reference],
    )

    assert payload["model"] == "gpt-image-2"
    assert payload["prompt"] == "Make a background."
    assert payload["image"] == [base64.b64encode(b"concept").decode("ascii")]
    assert payload["size"] == "1024x1024"
    assert payload["response_format"] == "url"


def test_extract_image_bytes_supports_b64_json():
    assert _extract_image_bytes(
        {"data": [{"b64_json": base64.b64encode(b"png").decode("ascii")}]}
    ) == b"png"
