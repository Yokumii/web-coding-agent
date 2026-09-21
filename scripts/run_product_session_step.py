"""Execute one externally planned capability using the existing Harness unchanged."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.config import HarnessConfig
from src.orchestration.harness import run_harness
from src.orchestration.ui_action_contracts import validate_ui_action, validate_ui_action_sequence
from prepare_forward_edit_seed import prepare_seed
from run_batch import _accepted_checks, _latest_grade_path, _read_state


def atomic_plan_from_edit(edit):
    """Translate the upstream Edit and its sole check without another LLM plan."""
    requirement_id = f"REQ-{str(edit['edit_id']).upper()}"
    classification = edit.get("classification") or {}
    primary = classification.get("primary") or {}
    impact_tags = [
        "product-session-edit",
        f"{primary.get('taxonomy', 'extension')}:{primary.get('type', 'feature')}",
    ]
    supplied = edit["browser_check"]
    for action in supplied["actions"]:
        validate_ui_action(action)
    validate_ui_action_sequence(supplied["actions"])
    check = {
        "id": supplied["id"],
        "task": f"Verify {edit['capability']}",
        "expected_result": "The requested functional state transition completes.",
        "critical": True,
        "category": "functional",
        "requirement_id": requirement_id,
        "impact_tags": impact_tags,
        "route": supplied["route"],
        "fixtures": [],
        "actions": supplied["actions"],
    }
    return {
        "schema_version": "atomic-edit-plan-v1",
        "title": edit["capability"],
        "goal": edit["instruction"],
        "source_anchors": [],
        "deliverables": [edit["capability"]],
        "exit_criteria": ["\n".join(edit.get("completion_criteria") or edit.get("acceptance") or [edit["capability"]])],
        "requirement_changes": [{
            "requirement_id": requirement_id,
            "relation": "add",
            "prior_requirement_ids": [],
            "rationale": edit["instruction"],
        }],
        "impact_tags": impact_tags,
        "unresolved_conflicts": [],
        "visual_evidence": "not_required",
        "visual_evidence_reason": "The upstream functional browser check is the sole acceptance check.",
        "checks": [check],
    }


async def execute(payload):
    workdir = Path(payload["workdir"])
    edit = payload["edit"]
    if workdir.exists() and not (
        (workdir / "seed_manifest.json").is_file()
        or (workdir / ".harness" / "edit_task_contract.json").is_file()
    ):
        shutil.rmtree(workdir)
    if not workdir.exists():
        prepare_seed(Path(payload["source_project"]), workdir, Path(payload["source_evaluation"]))
    if "inspiration_code" in payload:
        reference_path = workdir / ".harness" / "inspiration_code.json"
        if reference_path.exists() and json.loads(reference_path.read_text()) != payload["inspiration_code"]:
            raise ValueError("cannot resume with changed inspiration code")
        reference_path.parent.mkdir(parents=True, exist_ok=True)
        reference_path.write_text(json.dumps(payload["inspiration_code"], ensure_ascii=False, indent=2) + "\n")
    state = _read_state(workdir)
    contract = workdir / ".harness" / "edit_task_contract.json"
    if contract.is_file():
        saved = json.loads(contract.read_text()).get("chain_metadata")
        if saved != edit:
            raise ValueError("cannot resume with changed Edit metadata")
    resume_existing = bool(state)
    if contract.is_file() and not state:
        baseline = json.loads(contract.read_text())["baseline_commit"]
        frontend = workdir / "frontend"
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=frontend, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=frontend, text=True).strip()
        if head != baseline or dirty:
            raise ValueError("incomplete initialization has source changes; preserve it for diagnosis")
        # Re-materialize the same upstream Edit after interrupted initialization.
        # No model plan or code generation has run at this clean baseline.
    if state.get("last_verdict") != "completed":
        budget = float("inf") if payload.get("no_api_budget") else payload["budget_usd"]
        config = HarnessConfig(agent_runtime="openai", openai_api_key=os.environ["TOKENWAVE_OPENAI_API_KEY"],
            openai_base_url=os.environ.get("TOKENWAVE_OPENAI_BASE_URL", "https://api.tokenwave.us/v1"),
            planner_model="gpt-5.5", generator_model="gpt-5.5", evaluator_model="gpt-5.5",
            evaluator_vision_model="gpt-5.5", evaluator_mode="full", playwright_headless=True,
            evaluator_evidence_route="typed",
            evaluator_vision_endpoint_type="openai", evaluator_vision_max_retries=0,
            evaluator_vision_api_key=os.environ["TOKENWAVE_OPENAI_API_KEY"],
            evaluator_vision_base_url=os.environ.get("TOKENWAVE_OPENAI_BASE_URL", "https://api.tokenwave.us/v1"),
            frontend_port=payload.get("port", 18931), max_budget_usd=budget,
            allow_paid_resume_call=bool(payload.get("no_api_budget")),
            minimality_guard_enabled=False,
            edit_originality_required=False,
            edit_collect_visual_failures_before_repair=False,
            edit_webcompass_defect_checks=True,
            edit_max_rounds=2,
            planner_budget_usd=budget if payload.get("no_api_budget") else min(2.0,budget),
            generator_budget_usd=budget,
            evaluator_budget_usd=budget if payload.get("no_api_budget") else min(3.0,budget),
            agent_request_timeout_seconds=180)
        await run_harness(edit["instruction"], workdir, config, task_mode="edit", keep_frontend=True,
            resume=resume_existing, target_routes=edit["target_routes"],
            atomic_plan=None if resume_existing else atomic_plan_from_edit(edit),
            chain_metadata=None if resume_existing else edit,
            prior_accepted_checks=None)
    state = _read_state(workdir)
    completed = state.get("last_verdict") == "completed"
    result = {"status": "completed" if completed else "incomplete", "workdir": str(workdir),
              "cost_usd": sum((state.get("costs") or {}).values()),
              "last_verdict": state.get("last_verdict")}
    if completed:
        result.update(project_path=str(workdir / "frontend"), evaluation=str(_latest_grade_path(workdir)),
                      accepted_checks=_accepted_checks(workdir, namespace=payload["session_id"]+"__"+edit["edit_id"]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(execute(json.loads(args.input.read_text())))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
