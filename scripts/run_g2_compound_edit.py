#!/usr/bin/env python3
"""Run one 0921 record (or prepared case) through sequential Skill-assisted Edit."""
from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.prepare_forward_edit_seed import compact_source_ui_contract
from scripts.run_batch import BatchTask, EditStep, run_batch
from scripts.export_trajectory_dataset import apply_patches, code_at_commit, make_patches
from src.agents.compound_edit_planner import plan_frozen_compound_edit
from src.config import HarnessConfig
from src.orchestration.frozen_compound_edit import read_frozen_compound_plan


async def _source_ui_contract(source: Path, evaluation: Path, config: HarnessConfig) -> dict:
    """Ground initial control visibility in the running source, not HTML guesses."""
    from playwright.async_api import async_playwright
    from src.utils.playwright_browser import launch_chromium
    from src.orchestration.runtime import build_frontend_command, start_process, stop_process, wait_for_http
    contract = compact_source_ui_contract(source, evaluation)
    server = start_process(name="source-observation", command=build_frontend_command(source, config.frontend_port),
                           cwd=source, log_path=evaluation.parent / "source_observation.log")
    url = f"http://127.0.0.1:{config.frontend_port}"
    try:
        await wait_for_http(name="source-observation", url=url, managed=server)
        async with async_playwright() as playwright:
            browser = await launch_chromium(playwright, headless=True)
            try:
                page = await browser.new_page()
                for surface in contract.get("pages", []):
                    await page.goto(url + surface["route"], wait_until="domcontentloaded")
                    for control in surface.get("controls", []):
                        locator = page.locator(control["selector"])
                        control["initially_visible"] = await locator.count() > 0 and await locator.first.is_visible()
                    surface["entry_transitions"] = []
                    entries = [c for c in surface.get("controls", [])
                               if c.get("initially_visible") and c.get("tag") == "button"][:8]
                    for entry in entries:
                        probe = await browser.new_page()
                        try:
                            await probe.goto(url + surface["route"], wait_until="domcontentloaded")
                            await probe.locator(entry["selector"]).first.click(timeout=3000)
                            await probe.wait_for_timeout(1000)
                            hidden = []
                            for control in entries:
                                target = probe.locator(control["selector"])
                                if not await target.count() or not await target.first.is_visible():
                                    hidden.append(control["selector"])
                            if hidden:
                                surface["entry_transitions"].append({"click": entry["selector"], "hides": hidden})
                        finally:
                            await probe.close()
            finally:
                await browser.close()
    finally:
        await stop_process(server)
    (evaluation.parent / "source_ui_contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n")
    return contract


def _read_case(path: Path) -> dict[str, Any]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))



def normalize_input_case(row: dict[str, Any]) -> dict[str, Any]:
    """Accept the official 0921 row without rewriting its code or instructions."""
    if "source_code" in row:
        return row
    instruction = row.get("instruction")
    if not isinstance(instruction, dict):
        raise ValueError("Expected 0921 instruction.src_code and instruction.description")
    status = (row.get("metadata") or {}).get("instruction_status")
    if status not in (None, "query_ready"):
        raise ValueError(f"0921 input is not query_ready: {status}")
    descriptions = instruction.get("description")
    if not isinstance(descriptions, list):
        raise ValueError("0921 description must be a list")
    declared = row.get("task_type")
    if declared is not None and declared != [item.get("task_type") for item in descriptions]:
        raise ValueError("0921 task types disagree with descriptions")
    return {**row, "source_code": instruction.get("src_code"), "descriptions": descriptions}

def _code_sha256(code: list[dict[str, str]]) -> str:
    payload = json.dumps(code, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _frozen_subtasks(case: dict[str, Any]) -> list[dict[str, str]]:
    descriptions = case.get("descriptions")
    if not isinstance(descriptions, list) or not 4 <= len(descriptions) <= 12:
        raise ValueError("case requires 4--12 frozen descriptions")
    output: list[dict[str, str]] = []
    for index, item in enumerate(descriptions, 1):
        if not isinstance(item, dict):
            raise ValueError(f"description {index} is not an object")
        task_type = str(item.get("task_type") or "").strip()
        instruction = str(item.get("description") or "").strip()
        if not task_type or not instruction:
            raise ValueError(f"description {index} is incomplete")
        output.append(
            {
                "id": f"q{index}",
                "task_type": task_type,
                "instruction": instruction,
            }
        )
    return output


def _materialize_source(
    code: list[dict[str, str]], destination: Path
) -> tuple[Path, str]:
    paths = [Path(str(item["path"])) for item in code]
    prefix = "code/" if paths and all(path.parts[:1] == ("code",) for path in paths) else ""
    source_root = destination / "source"
    project_root = source_root / "code" if prefix else source_root
    for item in code:
        relative = Path(str(item["path"]))
        target = (source_root / relative).resolve()
        target.relative_to(source_root.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        content = str(item["code"])
        if target.exists() and target.read_text(encoding="utf-8") != content:
            raise ValueError(f"materialized source changed: {relative}")
        target.write_text(content, encoding="utf-8")
    if not (project_root / "index.html").is_file():
        html = sorted(project_root.glob("*.html"))
        if not html:
            raise ValueError("source project has no root HTML entry")
    return project_root, prefix


def _prepare_source_dependencies(source: Path, evaluation: Path) -> None:
    """Prepare an isolated source once before browser observation."""
    package = source / "package.json"
    if not package.is_file() or (source / "node_modules").is_dir():
        return
    log = evaluation.parent / "dependency_install.log"
    env = {**os.environ, "npm_config_cache": "/tmp/webcoding_npm_cache"}
    with log.open("w", encoding="utf-8") as stream:
        import subprocess
        subprocess.run(
            ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT,
            timeout=120, check=True,
        )


def _provider_config(args: argparse.Namespace) -> HarnessConfig:
    profile = {}
    if getattr(args, "provider_profile", None):
        profile_path = Path.home() / ".config/webcoding" / f"{args.provider_profile}.json"
        if args.provider_profile != "experimental-luna":
            raise ValueError("Unknown provider profile")
        profile = json.loads(profile_path.read_text())
    key = profile.get("bearer_token") or os.environ.get("NJULINK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base = (profile.get("base_url") or os.environ.get("OPENAI_BASE_URL", "https://api.nju-link.com")).rstrip("/")
    model = args.model or profile.get("model") or "gpt-5.6-luna"
    wire_api = profile.get("wire_api") or os.environ.get("OPENAI_WIRE_API", "chat")
    if not urlsplit(base).path.strip("/") and wire_api != "responses":
        base += "/v1"
    if not key:
        raise ValueError("NJULINK_API_KEY is required")
    budget = float(args.budget_usd)
    return HarnessConfig(
        agent_runtime="openai",
        openai_api_key=key,
        openai_base_url=base,
        openai_wire_api=wire_api,
        openai_stream_read_retries=2 if args.provider_profile == "experimental-luna" else 0,
        openai_extra_headers=profile.get("http_headers", {}),
        planner_model=model,
        generator_model=model,
        evaluator_model=model,
        evaluator_vision_model=model,
        evaluator_vision_api_key=key,
        evaluator_vision_base_url=base,
        evaluator_vision_endpoint_type="openai",
        evaluator_vision_max_retries=0,
        evaluator_vision_timeout_seconds=args.request_timeout,
        evaluator_evidence_route="typed",
        evaluator_mode="typed",
        playwright_headless=True,
        frontend_port=args.port,
        max_budget_usd=budget,
        planner_budget_usd=budget,
        generator_budget_usd=budget,
        evaluator_budget_usd=budget,
        # Lightweight production permits one additional evidence-driven Repair:
        # implementation + at most two repairs, still bounded per case.
        edit_max_rounds=max(3, getattr(args, "debug_max_rounds", 3)),
        edit_full_replay_interval=1,
        edit_replay_all_accepted_checks=False,
        edit_originality_required=False,
        edit_collect_visual_failures_before_repair=False,
        edit_webcompass_defect_checks=False,
        lightweight_edit_production=True,
        edit_ignore_unstable_source_fragments=True,
        edit_frozen_compound_mode=True,
        edit_skills_enabled=True,
        # Reverse-built 0905 seeds commonly depend on remote fonts/assets. A
        # counterfactual source rerender can therefore drift even when source
        # bytes are unchanged. Keep the mutation-path guard (including its
        # whole-file overwrite denial and line/file budgets), and rely on the
        # frozen current + full historical browser replay for acceptance.
        minimality_guard_enabled=False,
        minimal_path_guidance_enabled=True,
        minimal_path_max_patch_lines=240,
        minimal_path_max_touched_files=12,
        agent_request_timeout_seconds=args.request_timeout,
    )


def _load_step_patches(
    record: dict[str, Any], *, subtasks: list[dict[str, str]], prefix: str
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, Any]]]:
    patches: list[dict[str, str]] = []
    initial: list[dict[str, str]] | None = None
    final: list[dict[str, str]] | None = None
    step_summaries: list[dict[str, Any]] = []
    for subtask, step in zip(subtasks, record.get("steps") or []):
        ground_truth = step.get("ground_truth") or {}
        frontend = Path(step["workdir"]) / "frontend"
        before = code_at_commit(frontend, ground_truth["source_commit"])
        after = code_at_commit(frontend, ground_truth["target_commit"])
        if initial is None:
            initial = before
        step_patches = make_patches(before, after, subtask["task_type"])
        if apply_patches(before, step_patches) != after:
            raise ValueError(f"{subtask['id']}: exported patches do not replay")
        for patch in step_patches:
            patch["path"] = prefix + patch["path"]
        patches.extend(step_patches)
        final = after
        step_summaries.append(
            {
                "id": subtask["id"],
                "task_type": subtask["task_type"],
                "status": step.get("status"),
                "cost_usd": step.get("cost_usd", 0),
                "patch_count": len(step_patches),
                "source_commit": ground_truth["source_commit"],
                "target_commit": ground_truth["target_commit"],
            }
        )
    if initial is None or final is None:
        raise ValueError("accepted chain has no exported steps")
    return patches, final, step_summaries


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    case = normalize_input_case(_read_case(args.case.resolve()))
    case_id = str(case.get("instance_id") or args.case.stem)
    code = case.get("source_code")
    if not isinstance(code, list) or not code:
        raise ValueError("case has no source_code")
    source_hash = _code_sha256(code)
    if case.get("source_code_sha256") not in (None, source_hash):
        raise ValueError("case source_code_sha256 mismatch")
    subtasks = _frozen_subtasks(case)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    input_manifest = output / "input_manifest.json"
    expected_manifest = {
        "case_id": case_id,
        "source_code_sha256": source_hash,
        "subtasks": subtasks,
    }
    if input_manifest.is_file() and json.loads(input_manifest.read_text()) != expected_manifest:
        raise ValueError("frozen case input changed; use a new output directory")
    input_manifest.write_text(
        json.dumps(expected_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    source, prefix = _materialize_source(code, output)
    evaluation = output / "source_evaluation.json"
    if not evaluation.exists():
        evaluation.write_text("{}\n", encoding="utf-8")
    _prepare_source_dependencies(source, evaluation)
    config = _provider_config(args)
    frozen_path = output / "frozen_compound_edit_plan.json"
    if frozen_path.is_file():
        frozen_plan, plan_hash = read_frozen_compound_plan(frozen_path)
        if frozen_plan["case_id"] != case_id or frozen_plan["source_code_sha256"] != source_hash:
            raise ValueError("frozen plan belongs to a different case/source")
        if [
            {key: item[key] for key in ("id", "task_type", "instruction")}
            for item in frozen_plan["subtasks"]
        ] != subtasks:
            raise ValueError("frozen plan subtask list changed")
        planning = {"plan": frozen_plan, "frozen_plan_sha256": plan_hash, "usage": {}}
    else:
        planning = await plan_frozen_compound_edit(
            config=config,
            case_id=case_id,
            source_code_sha256=source_hash,
            frozen_subtasks=subtasks,
            source_ui_contract=await _source_ui_contract(source, evaluation, config),
            output_path=frozen_path,
        )
    all_planned = planning["plan"]["subtasks"]
    limit = getattr(args, "max_steps", None)
    planned = all_planned[:limit] if limit else all_planned
    steps = tuple(
        EditStep(
            id=item["id"],
            prompt=item["instruction"],
            target_routes=tuple(item["target_routes"]),
            atomic_plan=item["atomic_plan"],
            chain_metadata={
                "edit_id": item["id"],
                "edit_index": index,
                "source_version": f"s{index - 1}",
                "target_version": f"s{index}",
                "task_type": item["task_type"],
                "instruction": item["instruction"],
                "depends_on": [planned[index - 2]["id"]] if index > 1 else [],
                "requires": [],
                "produces": [{"state": f"accepted-s{index}"}],
                "acceptance": ["current frozen browser flow and every prior flow pass"],
                "preserve": [
                    "all unrelated source, content, behavior, routes, and accepted capabilities"
                ],
                "frozen_plan_sha256": planning["frozen_plan_sha256"],
            },
        )
        for index, item in enumerate(planned, 1)
    )
    task = BatchTask(
        id=case_id,
        prompt="\n".join(item["instruction"] for item in planned),
        workdir="chain",
        task_mode="edit",
        seed_frontend=source,
        seed_evaluation=evaluation,
        edits=steps,
        sequence_metadata={
            "schema_version": "g2-frozen-compound-edit-v1",
            "case_id": case_id,
            "source_code_sha256": source_hash,
            "frozen_plan_sha256": planning["frozen_plan_sha256"],
            "subtasks": subtasks,
        },
    )
    results = await run_batch(
        [task],
        output_dir=output,
        results_path=output / "results.jsonl",
        workers=1,
        base_port=args.port,
        timeout_seconds=args.case_timeout,
        config_overrides={
            key: value
            for key, value in vars(config).items()
            if key != "frontend_port"
        },
        resume_harness=args.resume,
    )
    if not results:
        records = [
            json.loads(line)
            for line in (output / "results.jsonl").read_text().splitlines()
            if line.strip()
        ]
        record = records[-1]
    else:
        record = results[-1]
    result: dict[str, Any] = {
        "status": record.get("status"),
        "instance_id": case_id,
        "task_count": len(subtasks),
        "task_types": [item["task_type"] for item in subtasks],
        "source_code_sha256": source_hash,
        "frozen_plan_sha256": planning["frozen_plan_sha256"],
        "planning_usage": planning.get("usage") or {},
        "cost_usd": record.get("cost_usd", 0),
        "steps": record.get("steps") or [],
    }
    if record.get("status") == "ok" and len(planned) < len(subtasks):
        result["status"] = "partial"
        result["completed_prefix"] = len(planned)
    if record.get("status") == "ok":
        patches, final, step_summaries = _load_step_patches(
            record, subtasks=subtasks, prefix=prefix
        )
        source_for_replay = [
            {"path": str(item["path"]), "code": str(item["code"])} for item in code
        ]
        if apply_patches(source_for_replay, patches) != [
            {"path": prefix + item["path"], "code": item["code"]} for item in final
        ]:
            raise ValueError("full ordered compound patch replay failed")
        result.update(
            patch_scope="full" if result["status"] == "ok" else "accepted_prefix",
            response=patches,
            patch_count=len(patches),
            patch_count_by_task={
                task_type: sum(patch.get("task_type") == task_type for patch in patches)
                for task_type in result["task_types"]
            },
            final_code_sha256=_code_sha256(
                [{"path": prefix + item["path"], "code": item["code"]} for item in final]
            ),
            step_summaries=step_summaries,
        )
        (output / "patches.json").write_text(
            json.dumps({"scope": result["patch_scope"], "completed_steps": len(step_summaries),
                        "total_steps": len(subtasks), "patches": patches}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--case", type=Path, required=True)
    result.add_argument("--fast-gt", action="store_true", help="Native preloaded single-call GT path; no Codex CLI")
    result.add_argument("--unattended", action="store_true", help="Fresh fast GT validation without manual checks or resume")
    result.add_argument("--subtask-timeout", type=int, default=180)
    result.add_argument("--browser-url", help="Already running isolated frontend for the current flow")
    result.add_argument("--browser-check", type=Path, help="Frozen typed browser flow for a single-step smoke")
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--model", default=None)
    result.add_argument("--provider-profile", choices=["experimental-luna", "qwen"])
    result.add_argument("--port", type=int, default=18931)
    result.add_argument("--budget-usd", type=float, default=20.0)
    result.add_argument("--request-timeout", type=int, default=600)
    result.add_argument("--case-timeout", type=float, default=14400)
    result.add_argument("--resume", action="store_true")
    result.add_argument("--acceptance", choices=["standard", "lenient"], default="standard")
    result.add_argument("--debug-max-rounds", type=int, choices=range(3, 9), default=3,
                        help="Bounded local debugging continuation; lightweight production defaults to implementation plus two repairs")
    result.add_argument("--max-steps", type=int, choices=range(1, 13), help="Bound a first sample; incomplete prefixes are not exported as full GT")
    return result


def main() -> int:
    args = parser().parse_args()
    os.environ.setdefault("OPENAI_STREAM_LOG", str(args.output.resolve() / "response_stream.jsonl"))
    if args.fast_gt:
        if args.browser_check and args.max_steps != 1:
            raise SystemExit("--browser-check currently requires --max-steps 1")
        if not 1 <= args.subtask_timeout <= 180:
            raise SystemExit("--subtask-timeout must be between 1 and 180")
        from src.orchestration.fast_edit_gt import execute as execute_fast
        result = asyncio.run(execute_fast(args))
        print(json.dumps({k:v for k,v in result.items() if k not in {'response','reference'}}, ensure_ascii=False))
        return 0 if result['status'] in {'ok', 'partial'} else 2
    if args.provider_profile == "qwen":
        raise SystemExit("qwen profile currently requires --fast-gt")
    result = asyncio.run(asyncio.wait_for(execute(args), timeout=args.case_timeout))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
