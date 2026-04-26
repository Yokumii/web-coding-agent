from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class HarnessConfig:
    api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("ANTHROPIC_BASE_URL", ""))

    planner_model: str = field(default_factory=lambda: os.getenv("PLANNER_MODEL", "claude-sonnet-4-6"))
    generator_model: str = field(default_factory=lambda: os.getenv("GENERATOR_MODEL", "claude-sonnet-4-6"))
    evaluator_model: str = field(default_factory=lambda: os.getenv("EVALUATOR_MODEL", "claude-sonnet-4-6"))
    evaluator_vision_model: str = field(
        default_factory=lambda: os.getenv(
            "EVALUATOR_VISION_MODEL",
            os.getenv("EVALUATOR_MODEL", "claude-sonnet-4-6"),
        )
    )
    evaluator_vision_api_key: str = field(
        default_factory=lambda: os.getenv(
            "EVALUATOR_VISION_API_KEY",
            os.getenv("ANTHROPIC_API_KEY", ""),
        )
    )
    evaluator_vision_base_url: str = field(
        default_factory=lambda: os.getenv(
            "EVALUATOR_VISION_BASE_URL",
            os.getenv("ANTHROPIC_BASE_URL", ""),
        )
    )
    evaluator_vision_endpoint_type: str = field(
        default_factory=lambda: os.getenv("EVALUATOR_VISION_ENDPOINT_TYPE", "anthropic")
    )
    evaluator_vision_max_tokens: int = field(
        default_factory=lambda: int(os.getenv("EVALUATOR_VISION_MAX_TOKENS", "1200"))
    )
    sdk_max_buffer_size: int = field(
        default_factory=lambda: int(os.getenv("SDK_MAX_BUFFER_SIZE", str(8 * 1024 * 1024)))
    )

    max_budget_usd: float = 150.0
    planner_budget_usd: float = 2.0
    generator_budget_usd: float = 80.0
    evaluator_budget_usd: float = 10.0

    max_rounds: int = 3
    generator_max_turns: int = 200
    evaluator_max_turns: int = 120

    frontend_port: int = 5173
    backend_port: int = 8000
    playwright_headless: bool = False

    def get_client_kwargs(self) -> dict:
        kwargs: dict = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs
