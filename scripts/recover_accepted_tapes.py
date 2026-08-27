"""Recover missing accepted tapes and replay prior behavior on the final project.

This utility is for runs produced by the historical multi-assert recorder bug.
It appends only to a missing accepted_tapes.jsonl and creates a new replay
artifact; it never edits grades, browser evidence, commits, or exported data.
"""
from __future__ import annotations

import argparse
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.orchestration.accepted_tapes import (  # noqa: E402
    AcceptedTapeError,
    accepted_replay_checks,
    recover_missing_accepted_tapes,
)
from src.orchestration.browser_evidence import collect_browser_evidence  # noqa: E402


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: Any) -> None:
        del args


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def terminal_accepted_checkpoint(run_dir: Path) -> tuple[int, int]:
    harness = run_dir / ".harness"
    state = _read_json(harness / "accepted_sprints.json")
    accepted = state.get("accepted") if isinstance(state, dict) else None
    if not isinstance(accepted, list) or not accepted:
        raise AcceptedTapeError("run has no accepted sprint to replay")
    sprint_num = max(int(item) for item in accepted)
    candidate_rounds: list[int] = []
    for path in harness.glob("grade_round_*.json"):
        grade = _read_json(path)
        if (
            isinstance(grade, dict)
            and grade.get("sprint") == sprint_num
            and grade.get("overall_passed") is True
            and grade.get("sprint_passed") is True
            and grade.get("regression_passed") is True
        ):
            candidate_rounds.append(int(path.stem.rsplit("_", 1)[-1]))
    if not candidate_rounds:
        raise AcceptedTapeError(
            f"accepted sprint {sprint_num} has no fully passing terminal grade"
        )
    return sprint_num, max(candidate_rounds)


def replay_evidence_is_complete(
    evidence: dict[str, Any], checks: list[dict[str, Any]]
) -> bool:
    observed = evidence.get("checks") if isinstance(evidence, dict) else None
    if not isinstance(observed, list) or len(observed) != len(checks):
        return False
    return [str(item.get("check_id")) for item in observed if isinstance(item, dict)] == [
        str(check.get("id")) for check in checks
    ] and all(
        isinstance(item, dict) and item.get("status") == "ok" for item in observed
    )


async def recover_and_replay(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    frontend = run_dir / "frontend"
    if not frontend.is_dir():
        raise AcceptedTapeError(f"frontend directory does not exist: {frontend}")
    recovery = recover_missing_accepted_tapes(run_dir)
    sprint_num, round_num = terminal_accepted_checkpoint(run_dir)
    checks = accepted_replay_checks(
        run_dir / ".harness", before_sprint=sprint_num
    )
    if not checks:
        return {
            "status": "ok",
            "tapes": recovery,
            "replay": "not_required",
            "round": round_num,
            "check_count": 0,
        }

    output_path = run_dir / ".harness" / f"accepted_tape_replay_round_{round_num}.json"
    if output_path.exists():
        evidence = _read_json(output_path)
        if not replay_evidence_is_complete(evidence, checks):
            raise AcceptedTapeError(
                f"existing replay evidence is incomplete; refusing to overwrite {output_path}"
            )
        replay_status = "reused"
    else:
        handler = partial(_QuietHandler, directory=str(frontend))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = int(server.server_address[1])
        try:
            evidence = await collect_browser_evidence(
                app_url=f"http://127.0.0.1:{port}",
                checks=checks,
                output_path=output_path,
                headless=True,
                fail_fast=True,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        if not replay_evidence_is_complete(evidence, checks):
            raise AcceptedTapeError(
                f"final checkpoint failed accepted-tape replay; inspect {output_path}"
            )
        replay_status = "recorded"

    return {
        "status": "ok",
        "tapes": recovery,
        "replay": replay_status,
        "artifact": str(output_path),
        "round": round_num,
        "check_count": len(checks),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(recover_and_replay(args.run_dir))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
