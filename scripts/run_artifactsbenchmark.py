from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from src.agents.generator import run_generator
from src.agents.evaluator import run_evaluator
from src.agents.visual_review import apply_dedicated_visual_review, render_feedback_from_grades
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.runtime import start_app_stack
from src.orchestration.target_profile import detect_target_profile, target_profile_guidance


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def prepare_contract(row: dict[str, Any], workdir: Path) -> FileComm:
    workdir.mkdir(parents=True, exist_ok=True)
    file_comm = FileComm(workdir / ".harness")
    file_comm.reset_run_artifacts()
    target_profile = detect_target_profile(row["question"])
    file_comm.write_target_profile(target_profile)
    # ArtifactsBench's candidate model is evaluated from the public question only.
    # The per-sample checklist is private judge context and must not enter any
    # generator/evaluator artifact in the harness.
    acceptance_criteria = [
        "Implement every requirement explicitly stated in the user question.",
        "Provide a polished, runnable artifact whose primary flows work without external services.",
        "Use readable, maintainable source with no dead, duplicate, or unrelated implementation.",
        "Make device-, upload-, network-, payment-, or hardware-dependent flows demonstrable with deterministic local fallback data.",
        "Handle relevant loading, empty, invalid-input, failure, and recovery states without blocking the demo.",
    ]
    file_comm.write_spec(
        "# ArtifactsBenchmark Task\n\n"
        "## Product Overview\n" + row["question"] + "\n\n"
        "## Target Users\nUsers described by the benchmark request.\n\n"
        "## Feature Descriptions\nInfer and implement the explicit product requirements from the user question. Do not invent unrelated scope. Make the core workflow visibly demonstrable.\n\n"
        "## Technical Architecture\nUse a client-side Vite artifact as the browser preview. `npm run dev -- --host 127.0.0.1 --port 5173 --strictPort` must work exactly. Use a root `index.html`; do not use `serve`, fixed-port wrappers, or remote assets.\n\n"
        + target_profile_guidance(target_profile) + "\n"
        "## Visual Design Direction\nProduce a polished, task-appropriate visual design.\n"
    )
    file_comm.write_design_tokens({
        "theme_name": "benchmark-directed",
        "color": {}, "typography": {}, "spacing": {}, "radius": {}, "motion": {},
        "style_rules": ["Follow the visual requirements in the user request."],
        "anti_patterns": ["generic unfinished scaffold", "remote asset dependency"],
        "visual_experiment": {
            "design_hypothesis": "A task-specific visual system improves benchmark fidelity.",
            "reason_for_image_first": "No image generation is required for this benchmark run.",
            "desired_break_from_web_templates": ["task-specific composition"],
            "visual_opportunities_beyond_css": ["inline SVG and canvas where appropriate"],
            "forbidden_generic_patterns": ["unfinished centered demo card"],
        },
    })
    file_comm.write_feature_list({"features": [{
        "id": "F001", "name": "Complete benchmark artifact", "priority": "high",
        "depends_on": [], "description": row["question"],
        "acceptance_criteria": acceptance_criteria, "status": "in_progress", "sprint": 1,
    }]})
    deliverables = ["Vite-compatible runnable preview"]
    if target_profile["profile"] != "web":
        deliverables.append(f"Readable {target_profile['label']} source under frontend/submission")
    file_comm.write_sprint_plan({"total_sprints": 1, "sprints": [{
        "number": 1, "title": "Complete benchmark artifact",
        "goal": row["question"], "feature_ids": ["F001"],
        "deliverables": deliverables, "exit_criteria": acceptance_criteria,
    }]})
    generic_ui_checks = [
        ("Core workflow", "The main user workflow is visible, interactive, and completes successfully."),
        ("Requirement fidelity", "All requirements explicitly stated in the question are represented."),
        ("Demo resilience", "The initial demo remains useful when external capabilities are unavailable."),
        ("Visual quality", "The artifact is polished, legible, responsive, and task-appropriate."),
        ("Failure recovery", "Relevant invalid or failed states provide clear feedback and recovery."),
    ]
    file_comm.write_ui_verification_plan({"sprints": [{"sprint": 1, "checks": [
        {"id": f"UI-{i+1:03d}", "feature_id": "F001", "task": task,
         "expected_result": expected, "critical": True, "category": "general-quality"}
        for i, (task, expected) in enumerate(generic_ui_checks)
    ]}]})
    file_comm.write_progress("# Progress Log\n\n## Benchmark\n- status: ready\n")
    file_comm.write_accepted_sprints({"accepted": [], "current_target": 1, "last_evaluated_round": 0})
    return file_comm


async def run_row(
    row: dict[str, Any],
    output_root: Path,
    config: HarnessConfig,
    *,
    reuse_frontend: bool = False,
) -> dict[str, Any]:
    case_dir = output_root / f"index_{int(row['index']):04d}"
    result_path = case_dir / "result.json"
    if result_path.exists():
        existing = json.loads(result_path.read_text())
        full_result_complete = (
            config.evaluator_mode != "full"
            or existing.get("visual_evaluator") is not None
        )
        if existing.get("status") == "ok" and full_result_complete:
            existing.pop("error", None)
            return existing
    file_comm = prepare_contract(row, case_dir)
    started = time.monotonic()
    result: dict[str, Any] = {"index": row["index"], "row_idx": row["row_idx"], "class": row.get("class"), "difficulty": row.get("difficulty")}
    try:
        stats = None
        if not reuse_frontend:
            stats = await run_generator(config, file_comm, case_dir, 1, 1, "generate")
        elif not (case_dir / "frontend" / "package.json").exists():
            raise FileNotFoundError("--reuse-frontend requires an existing frontend/package.json")
        stack = await start_app_stack(case_dir, file_comm.dir, config, 1)
        try:
            passed, grades, eval_stats = await run_evaluator(
                config, file_comm, case_dir, round_num=1, app_url=stack.frontend_url,
            )
        finally:
            await stack.close()
        visual_stats = None
        if config.evaluator_mode == "full":
            sprint = (file_comm.read_sprint_plan() or {})["sprints"][0]
            grades, visual_stats = await apply_dedicated_visual_review(
                config=config,
                file_comm=file_comm,
                workdir=case_dir,
                round_num=1,
                sprint_num=1,
                sprint_context=sprint,
                grades=grades,
                manifest=file_comm.read_visual_manifest(1),
            )
            passed = bool(grades.get("overall_passed"))
            file_comm.write_grades(1, grades)
            file_comm.write_feedback(1, render_feedback_from_grades(grades))
        result.update(
            status="ok" if passed else "evaluation_failed",
            generator=stats.to_dict() if stats else None,
            evaluator=eval_stats.to_dict(),
            visual_evaluator=visual_stats.to_dict() if visual_stats else None,
            grades=grades,
        )
        result.pop("error", None)
    except Exception as exc:
        result.update(status="error", error=f"{type(exc).__name__}: {exc}")
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    case_dir.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


async def async_main(args: argparse.Namespace) -> None:
    rows = load_rows(args.queries)
    if args.indices:
        selected_indices = {
            int(value.strip()) for value in args.indices.split(",") if value.strip()
        }
        rows = [row for row in rows if int(row["index"]) in selected_indices]
    if args.index is not None:
        rows = [row for row in rows if int(row["index"]) == args.index]
    if args.offset is not None:
        rows = rows[args.offset:]
    if args.limit is not None:
        rows = rows[:args.limit]
    config = HarnessConfig(
        agent_runtime="openai", planner_model=args.model, generator_model=args.model,
        evaluator_model=args.model, evaluator_mode=args.evaluator_mode, playwright_headless=True,
        evaluator_vision_model=args.vision_model or os.getenv("EVALUATOR_VISION_MODEL") or args.model,
        evaluator_vision_api_key=os.getenv("EVALUATOR_VISION_API_KEY") or os.getenv("OPENAI_AGENT_API_KEY", ""),
        evaluator_vision_base_url=os.getenv("EVALUATOR_VISION_BASE_URL") or os.getenv("OPENAI_AGENT_BASE_URL", ""),
        evaluator_vision_endpoint_type="openai",
        agent_phase_timeout_seconds=args.phase_timeout,
        agent_request_timeout_seconds=args.request_timeout,
        agent_max_tool_calls=args.max_tool_calls,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "results.jsonl"
    completed: dict[int, dict[str, Any]] = {}
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            if line.strip():
                item = json.loads(line)
                completed[int(item["index"])] = item
    semaphore = asyncio.Semaphore(max(1, args.workers))

    async def limited_run(row: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            row_config = replace(config, frontend_port=20_000 + int(row["index"]))
            return await run_row(
                row,
                args.output,
                row_config,
                reuse_frontend=args.reuse_frontend,
            )

    tasks = [asyncio.create_task(limited_run(row)) for row in rows]
    for task in asyncio.as_completed(tasks):
        result = await task
        completed[int(result["index"])] = result
        manifest.write_text("".join(
            json.dumps(item, ensure_ascii=False) + "\n"
            for _, item in sorted(completed.items())
        ))
        print(json.dumps({k: result.get(k) for k in ("index", "status", "elapsed_seconds", "error")}, ensure_ascii=False), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="qwen3.7-max")
    parser.add_argument("--index", type=int)
    parser.add_argument("--indices", help="Comma-separated benchmark indices")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--phase-timeout", type=int, default=900)
    parser.add_argument("--request-timeout", type=int, default=300)
    parser.add_argument("--max-tool-calls", type=int, default=120)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--evaluator-mode", choices=("full", "simple"), default="full")
    parser.add_argument("--vision-model")
    parser.add_argument("--reuse-frontend", action="store_true")
    asyncio.run(async_main(parser.parse_args()))


if __name__ == "__main__":
    main()
