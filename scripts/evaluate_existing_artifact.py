from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from src.agents.evaluator import run_evaluator
from src.agents.visual_review import apply_dedicated_visual_review, render_feedback_from_grades
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.runtime import start_app_stack


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--model", default="qwen3.7-max")
    parser.add_argument("--vision-model")
    parser.add_argument("--visual-only", action="store_true")
    parser.add_argument("--round", type=int, default=2)
    args = parser.parse_args()
    workdir = args.workdir.resolve()
    file_comm = FileComm(workdir / ".harness")
    config = HarnessConfig(
        agent_runtime="openai",
        openai_api_key=os.environ["OPENAI_AGENT_API_KEY"],
        openai_base_url=os.environ["OPENAI_AGENT_BASE_URL"],
        evaluator_model=args.model,
        evaluator_mode="full",
        playwright_headless=True,
        agent_phase_timeout_seconds=900,
        agent_request_timeout_seconds=300,
        agent_max_tool_calls=120,
        evaluator_vision_model=args.vision_model or os.getenv("EVALUATOR_VISION_MODEL") or args.model,
        evaluator_vision_api_key=os.getenv("EVALUATOR_VISION_API_KEY") or os.environ["OPENAI_AGENT_API_KEY"],
        evaluator_vision_base_url=os.getenv("EVALUATOR_VISION_BASE_URL") or os.environ["OPENAI_AGENT_BASE_URL"],
        evaluator_vision_endpoint_type="openai",
    )
    started = time.monotonic()
    if args.visual_only:
        grades = file_comm.read_grades(args.round)
        if not grades:
            raise RuntimeError(f"missing grades for round {args.round}")
        passed = bool(grades.get("overall_passed"))
        output = {"functional_passed": passed, "functional_grades": grades, "functional_reused": True}
    else:
        stack = await start_app_stack(workdir, file_comm.dir, config, args.round)
        try:
            passed, grades, stats = await run_evaluator(
                config, file_comm, workdir, args.round, stack.frontend_url
            )
        finally:
            await stack.close()
        output = {"functional_passed": passed, "functional_grades": grades, "functional_stats": stats.to_dict()}
    manifest = file_comm.read_visual_manifest(args.round)
    sprint_num = int(grades.get("sprint") or 1)
    sprint = next(
        item for item in (file_comm.read_sprint_plan() or {})["sprints"]
        if int(item["number"]) == sprint_num
    )
    try:
        merged, visual_stats = await apply_dedicated_visual_review(
            config=config, file_comm=file_comm, workdir=workdir, round_num=args.round,
            sprint_num=sprint_num, sprint_context=sprint, grades=grades, manifest=manifest,
        )
        appearance = (merged.get("phase_results") or {}).get("appearance")
        file_comm.write_grades(args.round, merged)
        file_comm.write_feedback(args.round, render_feedback_from_grades(merged))
        output.update(visual_status="ok" if appearance == "pass" else "failed", final_grades=merged, visual_stats=visual_stats.to_dict() if visual_stats else None)
    except Exception as exc:
        output.update(visual_status="error", visual_error=f"{type(exc).__name__}: {exc}")
    output["elapsed_seconds"] = round(time.monotonic() - started, 3)
    path = workdir / f"full_evaluation_round_{args.round}.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"functional_passed": passed, "visual_status": output["visual_status"], "elapsed_seconds": output["elapsed_seconds"]}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
