from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from src.config import HarnessConfig
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
        default=3,
        help="Max build-evaluate cycles (default: 3)",
    )
    parser.add_argument(
        "--max-budget",
        type=float,
        default=150.0,
        help="Max total budget in USD (default: 150)",
    )
    parser.add_argument(
        "--planner-model",
        default=None,
        help="Model for planner agent (default: PLANNER_MODEL env or claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--generator-model",
        default=None,
        help="Model for generator agent (default: GENERATOR_MODEL env or claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--evaluator-model",
        default=None,
        help="Model for evaluator agent (default: EVALUATOR_MODEL env or claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Only run the planner, then stop",
    )
    parser.add_argument(
        "--playwright-headless",
        action="store_true",
        help="Run Playwright in headless mode",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last checkpoint in workdir",
    )
    return parser


def build_config(args: argparse.Namespace) -> HarnessConfig:
    kwargs: dict = {
        "max_budget_usd": args.max_budget,
        "max_rounds": args.max_rounds,
        "playwright_headless": args.playwright_headless,
    }
    if args.planner_model is not None:
        kwargs["planner_model"] = args.planner_model
    if args.generator_model is not None:
        kwargs["generator_model"] = args.generator_model
    if args.evaluator_model is not None:
        kwargs["evaluator_model"] = args.evaluator_model
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
    ))


if __name__ == "__main__":
    cli()
