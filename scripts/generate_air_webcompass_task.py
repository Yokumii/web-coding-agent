#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from src.task_generation.air_webcompass import (
    OpenAICompatibleJSONClient,
    WEBCOMPASS_EDIT_TYPES,
    append_jsonl_record,
    initial_generation_prompt,
    load_seed_code,
    refinement_prompt,
    validate_initial_candidate,
    validate_refinement,
)


def existing_successes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") == "ok" and row.get("case_id"):
            completed.add(str(row["case_id"]))
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(description="AIR-style WebCompass task generator")
    parser.add_argument("--mode", choices=("initial", "refine"), default="initial")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--seed-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-type", action="append", default=[])
    parser.add_argument("--original-query-file", type=Path)
    parser.add_argument("--evidence-file", type=Path)
    args = parser.parse_args()

    if args.case_id in existing_successes(args.output):
        print(json.dumps({"status": "skipped", "case_id": args.case_id}))
        return

    started = time.monotonic()
    record: dict[str, object] = {"case_id": args.case_id, "mode": args.mode}
    try:
        files = load_seed_code(args.seed_dir)
        client = OpenAICompatibleJSONClient.from_env()
        if args.mode == "initial":
            task_types = tuple(args.task_type)
            if not task_types or any(item not in WEBCOMPASS_EDIT_TYPES for item in task_types):
                raise ValueError("--task-type must use the closed WebCompass edit taxonomy")
            system, user = initial_generation_prompt(files, task_types)
            payload, usage = client.complete(system, user)
            candidate = validate_initial_candidate(payload, task_types)
            record.update({"status": "ok", "task_types": task_types, "candidate": candidate, "usage": usage})
        else:
            if not args.original_query_file or not args.evidence_file:
                raise ValueError("refine mode requires --original-query-file and --evidence-file")
            original_query = args.original_query_file.read_text(encoding="utf-8").strip()
            evidence = json.loads(args.evidence_file.read_text(encoding="utf-8"))
            if not isinstance(evidence, list) or not evidence:
                raise ValueError("evidence file must contain a non-empty JSON list")
            allowed = {str(item.get("id", "")) for item in evidence if isinstance(item, dict)}
            system, user = refinement_prompt(original_query=original_query, evidence=evidence, files=files)
            payload, usage = client.complete(system, user)
            refinement = validate_refinement(
                payload, original_query=original_query, allowed_evidence_ids=allowed
            )
            record.update({"status": "ok", "refinement": refinement, "usage": usage})
    except Exception as exc:
        record.update({"status": "error", "error_type": type(exc).__name__, "error": str(exc)[:1200]})
    record["duration_seconds"] = round(time.monotonic() - started, 3)
    append_jsonl_record(args.output, record)
    print(json.dumps({"status": record["status"], "case_id": args.case_id, "output": str(args.output)}))
    if record["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

