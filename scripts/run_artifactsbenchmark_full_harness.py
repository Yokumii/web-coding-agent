from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path

from src.config import HarnessConfig
from src.orchestration.harness import run_harness
from src.orchestration.file_comm import FileComm


def load_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def completed_result(row: dict, case_dir: Path) -> dict | None:
    comm = FileComm(case_dir / ".harness")
    state = comm.read_state() or {}
    plan = comm.read_sprint_plan() or {}
    accepted = state.get("accepted_sprints") or []
    total = int(plan.get("total_sprints") or 0)
    if state.get("last_verdict") == "completed" or (total and len(accepted) >= total):
        return {
            "index": int(row["index"]),
            "status": "completed",
            "total_sprints": total,
            "accepted_sprints": accepted,
            "state": state,
        }
    return None


async def run_case(row: dict, root: Path, base_config: HarnessConfig) -> dict:
    index = int(row["index"])
    case_dir = root / f"index_{index:04d}"
    existing = completed_result(row, case_dir)
    if existing:
        return existing
    config = replace(base_config, frontend_port=23000 + index)
    started = time.monotonic()
    try:
        await run_harness(
            row["question"],
            case_dir.resolve(),
            config,
            resume=(case_dir / ".harness" / "harness_state.json").exists(),
            keep_frontend=True,
        )
        result = completed_result(row, case_dir)
        if result is None:
            state = FileComm(case_dir / ".harness").read_state() or {}
            result = {"index": index, "status": "incomplete", "state": state}
    except Exception as exc:
        result = {"index": index, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--model", default="qwen3.7-max")
    parser.add_argument("--vision-model", default="qwen3-vl-plus")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    config = HarnessConfig(
        agent_runtime="openai",
        planner_model=args.model,
        generator_model=args.model,
        evaluator_model=args.model,
        evaluator_mode="full",
        evaluator_vision_model=args.vision_model,
        evaluator_vision_api_key=os.environ["EVALUATOR_VISION_API_KEY"],
        evaluator_vision_base_url=os.environ["EVALUATOR_VISION_BASE_URL"],
        evaluator_vision_endpoint_type="openai",
        planner_scope_mode="query-aligned",
        final_project_mode=True,
        playwright_headless=True,
        max_rounds=6,
        max_budget_usd=200.0,
        agent_phase_timeout_seconds=1800,
        agent_request_timeout_seconds=420,
        agent_max_tool_calls=120,
    )
    rows = load_rows(args.queries)
    semaphore = asyncio.Semaphore(max(1, args.workers))

    async def limited(row: dict) -> dict:
        async with semaphore:
            return await run_case(row, args.output, config)

    results_path = args.output / "harness_results.jsonl"
    results: dict[int, dict] = {}
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            if line.strip():
                item = json.loads(line)
                results[int(item["index"])] = item
    tasks = [asyncio.create_task(limited(row)) for row in rows]
    for task in asyncio.as_completed(tasks):
        result = await task
        results[int(result["index"])] = result
        results_path.write_text("".join(
            json.dumps(item, ensure_ascii=False) + "\n"
            for _, item in sorted(results.items())
        ))
        print(json.dumps({k: result.get(k) for k in ("index", "status", "elapsed_seconds", "error")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
