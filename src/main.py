from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from src.config import (
    DEFAULT_FRONTEND_PORT,
    DEFAULT_MAX_BUDGET_USD,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MODEL,
    HarnessConfig,
)
from src.orchestration.harness import run_harness


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Frontend coding harness: Planner → Generator → Evaluator"
    )
    parser.add_argument(
        "prompt",
        help="1-4 sentence description of the app to build",
    )
    parser.add_argument(
        "--workdir",
        default="./workdir",
        help="Output directory for generated app (default: ./workdir)",
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=None,
        help=(
            "Max build-evaluate cycles (default: MAX_ROUNDS env or "
            f"{DEFAULT_MAX_ROUNDS})"
        ),
    )
    parser.add_argument(
        "--max-budget",
        type=float,
        default=None,
        help=(
            "Max total budget in USD (default: MAX_BUDGET_USD env or "
            f"{DEFAULT_MAX_BUDGET_USD:g})"
        ),
    )
    parser.add_argument(
        "--planner-model",
        default=None,
        help=(
            "Model for planner agent (default: PLANNER_MODEL env or "
            f"{DEFAULT_MODEL})"
        ),
    )
    parser.add_argument(
        "--generator-model",
        default=None,
        help=(
            "Model for generator agent (default: GENERATOR_MODEL env or "
            f"{DEFAULT_MODEL})"
        ),
    )
    parser.add_argument(
        "--evaluator-model",
        default=None,
        help=(
            "Model for evaluator agent (default: EVALUATOR_MODEL env or "
            f"{DEFAULT_MODEL})"
        ),
    )
    parser.add_argument(
        "--evaluator-vision-model",
        default=None,
        help=(
            "Model for the dedicated visual appearance scorer (default: "
            "EVALUATOR_VISION_MODEL env, then EVALUATOR_MODEL env, then "
            f"{DEFAULT_MODEL})"
        ),
    )
    parser.add_argument(
        "--playwright-headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run Playwright in headless mode (default: PLAYWRIGHT_HEADLESS env or false)",
    )
    parser.add_argument(
        "--frontend-port",
        type=int,
        default=None,
        help=(
            "Port for the frontend dev server (default: FRONTEND_PORT env or "
            f"{DEFAULT_FRONTEND_PORT})"
        ),
    )
    parser.add_argument(
        "--design-mode",
        choices=("text-only", "image-first"),
        default=None,
        help=(
            "Optional design-stage mode (default: DESIGN_MODE env or "
            "text-only). Use image-first to run the design checkpoint "
            "between plan and build."
        ),
    )
    parser.add_argument(
        "--planner-scope-mode",
        choices=("query-aligned", "expansive-data"),
        default=None,
        help=(
            "Planner scope strategy: query-aligned preserves benchmark intent; "
            "expansive-data restores the legacy 5-10 Sprint expansion used for "
            "training-data construction (default: PLANNER_SCOPE_MODE env or query-aligned)"
        ),
    )
    parser.add_argument(
        "--keep-frontend",
        action="store_true",
        help=(
            "Do not clear workdir/frontend/ on a fresh run. By default a fresh "
            "run (without --resume) wipes the previous prompt's frontend so "
            "the new generator does not 'repair' unrelated code. Use this to "
            "iterate on a hand-edited frontend."
        ),
    )
    parser.add_argument(
        "--final-project-mode",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Build toward one complete final website using a natural Sprint roadmap; "
            "intermediate states are for execution/recovery, not dataset extraction "
            "(default: FINAL_PROJECT_MODE env or false)"
        ),
    )
    # `--plan-only` 与 `--resume` 互斥；恢复执行时如果再带上前者，
    # 实际行为会绕过 planner 并继续后续阶段，与命令字面含义冲突。
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--plan-only",
        action="store_true",
        help="Only run the planner, then stop",
    )
    mode_group.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last checkpoint in workdir",
    )
    return parser


def build_config(args: argparse.Namespace) -> HarnessConfig:
    kwargs: dict = {}
    arg_to_config = {
        "max_budget": "max_budget_usd",
        "max_rounds": "max_rounds",
        "planner_model": "planner_model",
        "generator_model": "generator_model",
        "evaluator_model": "evaluator_model",
        "evaluator_vision_model": "evaluator_vision_model",
        "playwright_headless": "playwright_headless",
        "frontend_port": "frontend_port",
        "design_mode": "design_mode",
        "planner_scope_mode": "planner_scope_mode",
        "final_project_mode": "final_project_mode",
    }
    for arg_name, config_key in arg_to_config.items():
        value = getattr(args, arg_name)
        if value is not None:
            kwargs[config_key] = value
    return HarnessConfig(**kwargs)


def cli() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = build_config(args)

    workdir = Path(args.workdir).resolve()
    asyncio.run(run_harness(
        args.prompt, workdir, config,
        plan_only=args.plan_only,
        resume=args.resume,
        keep_frontend=args.keep_frontend,
    ))


if __name__ == "__main__":
    cli()
