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
        "--evaluator-vision-model",
        default=None,
        help=(
            "Model for the dedicated visual appearance scorer (default: "
            "EVALUATOR_VISION_MODEL env, then EVALUATOR_MODEL env, then "
            "claude-sonnet-4-6)"
        ),
    )
    parser.add_argument(
        "--playwright-headless",
        action="store_true",
        help="Run Playwright in headless mode",
    )
    parser.add_argument(
        "--frontend-port",
        type=int,
        default=None,
        help="Port for the frontend dev server (default: FRONTEND_PORT env or 5173)",
    )
    parser.add_argument(
        "--design-mode",
        choices=("text-only", "image-first"),
        default=None,
        help=(
            "Optional design-stage mode (default: DESIGN_MODE env or text-only). "
            "Use image-first to run the optional design checkpoint before build."
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
    # --plan-only and --resume are mutually exclusive: a checkpoint
    # resume that respected --plan-only would silently skip past the
    # planner and run the build/evaluate phases anyway, which is never
    # what the user wanted.
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
    if args.evaluator_vision_model is not None:
        kwargs["evaluator_vision_model"] = args.evaluator_vision_model
    if args.frontend_port is not None:
        kwargs["frontend_port"] = args.frontend_port
    if args.design_mode is not None:
        kwargs["design_mode"] = args.design_mode
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
