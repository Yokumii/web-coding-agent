from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_BUDGET_USD = 150.0
DEFAULT_PLANNER_BUDGET_USD = 2.0
DEFAULT_GENERATOR_BUDGET_USD = 80.0
DEFAULT_EVALUATOR_BUDGET_USD = 10.0
DEFAULT_MAX_ROUNDS = 3
DEFAULT_GENERATOR_MAX_TURNS = 200
DEFAULT_EVALUATOR_MAX_TURNS = 120
DEFAULT_MAX_DELIVERABLES_PER_SPRINT = 5
DEFAULT_MAX_EXIT_CRITERIA_PER_SPRINT = 5
DEFAULT_FRONTEND_PORT = 5173
DEFAULT_BACKEND_PORT = 8000
DEFAULT_PLAYWRIGHT_HEADLESS = False
DEFAULT_VISION_ENDPOINT_TYPE = "anthropic"
DEFAULT_VISION_MAX_TOKENS = 1200
DEFAULT_VISION_MAX_RETRIES = 3
DEFAULT_VISION_RETRY_BASE_DELAY_SECONDS = 2.0
DEFAULT_SDK_MAX_BUFFER_SIZE = 8 * 1024 * 1024


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} must be one of 1/0/true/false/yes/no/on/off, got {raw!r}"
    )


@dataclass
class HarnessConfig:
    """harness 运行期配置，来源依次为显式参数、环境变量与内置默认值。"""

    api_key: str = field(default_factory=lambda: _env_str("ANTHROPIC_API_KEY"))
    base_url: str = field(default_factory=lambda: _env_str("ANTHROPIC_BASE_URL"))

    planner_model: str = field(
        default_factory=lambda: _env_str("PLANNER_MODEL", DEFAULT_MODEL)
    )
    generator_model: str = field(
        default_factory=lambda: _env_str("GENERATOR_MODEL", DEFAULT_MODEL)
    )
    evaluator_model: str = field(
        default_factory=lambda: _env_str("EVALUATOR_MODEL", DEFAULT_MODEL)
    )
    evaluator_vision_model: str = field(
        default_factory=lambda: _env_str(
            "EVALUATOR_VISION_MODEL",
            _env_str("EVALUATOR_MODEL", DEFAULT_MODEL),
        )
    )
    evaluator_vision_api_key: str = field(
        default_factory=lambda: _env_str(
            "EVALUATOR_VISION_API_KEY",
            _env_str("ANTHROPIC_API_KEY"),
        )
    )
    evaluator_vision_base_url: str = field(
        default_factory=lambda: _env_str(
            "EVALUATOR_VISION_BASE_URL",
            _env_str("ANTHROPIC_BASE_URL"),
        )
    )
    evaluator_vision_endpoint_type: str = field(
        default_factory=lambda: _env_str(
            "EVALUATOR_VISION_ENDPOINT_TYPE",
            DEFAULT_VISION_ENDPOINT_TYPE,
        )
    )
    evaluator_vision_max_tokens: int = field(
        default_factory=lambda: _env_int(
            "EVALUATOR_VISION_MAX_TOKENS",
            DEFAULT_VISION_MAX_TOKENS,
        )
    )
    evaluator_vision_max_retries: int = field(
        default_factory=lambda: _env_int(
            "EVALUATOR_VISION_MAX_RETRIES",
            DEFAULT_VISION_MAX_RETRIES,
        )
    )
    evaluator_vision_retry_base_delay_seconds: float = field(
        default_factory=lambda: _env_float(
            "EVALUATOR_VISION_RETRY_BASE_DELAY",
            DEFAULT_VISION_RETRY_BASE_DELAY_SECONDS,
        )
    )
    sdk_max_buffer_size: int = field(
        default_factory=lambda: _env_int(
            "SDK_MAX_BUFFER_SIZE",
            DEFAULT_SDK_MAX_BUFFER_SIZE,
        )
    )

    max_budget_usd: float = field(
        default_factory=lambda: _env_float("MAX_BUDGET_USD", DEFAULT_MAX_BUDGET_USD)
    )
    planner_budget_usd: float = DEFAULT_PLANNER_BUDGET_USD
    generator_budget_usd: float = DEFAULT_GENERATOR_BUDGET_USD
    evaluator_budget_usd: float = DEFAULT_EVALUATOR_BUDGET_USD

    max_rounds: int = field(
        default_factory=lambda: _env_int("MAX_ROUNDS", DEFAULT_MAX_ROUNDS)
    )
    generator_max_turns: int = DEFAULT_GENERATOR_MAX_TURNS
    evaluator_max_turns: int = DEFAULT_EVALUATOR_MAX_TURNS

    max_deliverables_per_sprint: int = field(
        default_factory=lambda: _env_int(
            "MAX_DELIVERABLES_PER_SPRINT",
            DEFAULT_MAX_DELIVERABLES_PER_SPRINT,
        )
    )
    max_exit_criteria_per_sprint: int = field(
        default_factory=lambda: _env_int(
            "MAX_EXIT_CRITERIA_PER_SPRINT",
            DEFAULT_MAX_EXIT_CRITERIA_PER_SPRINT,
        )
    )

    frontend_port: int = field(
        default_factory=lambda: _env_int("FRONTEND_PORT", DEFAULT_FRONTEND_PORT)
    )
    backend_port: int = DEFAULT_BACKEND_PORT
    playwright_headless: bool = field(
        default_factory=lambda: _env_bool(
            "PLAYWRIGHT_HEADLESS",
            DEFAULT_PLAYWRIGHT_HEADLESS,
        )
    )

    def get_client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return kwargs
