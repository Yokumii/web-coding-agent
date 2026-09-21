from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web

from src.orchestration.atomic_edit_plan import (
    materialize_atomic_edit_compatibility_bundle,
    write_atomic_edit_plan,
)
from src.orchestration.browser_evidence import collect_browser_evidence
from src.orchestration.edit_task_contract import prepare_edit_task_contract
from src.orchestration.edit_risk_tests import materialize_edit_risk_tests
from src.orchestration.file_comm import FileComm
from src.orchestration.hidden_oracle_checks import write_hidden_oracle_checks
from src.orchestration.preimplementation_validation import (
    PreimplementationValidationError,
    freeze_preimplementation_validation,
    frozen_checks_for_sprint,
    verify_preimplementation_validation,
)


INSTRUCTION = "Add a download history panel and show its item count."


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _plan() -> dict:
    return {
        "schema_version": "atomic-edit-plan-v1",
        "title": "Add download history",
        "goal": INSTRUCTION,
        "source_anchors": [],
        "deliverables": ["A download history panel."],
        "exit_criteria": ["The panel shows the new history item count."],
        "requirement_changes": [
            {
                "requirement_id": "REQ-HISTORY",
                "relation": "add",
                "prior_requirement_ids": [],
                "rationale": INSTRUCTION,
            }
        ],
        "impact_tags": ["route:/", "download-history"],
        "unresolved_conflicts": [],
        "visual_evidence": "conditional",
        "visual_evidence_reason": "DOM evidence covers the behavioral Edit.",
        "checks": [
            {
                "id": "UI-HISTORY",
                "task": "Open download history.",
                "expected_result": "The history count is visible.",
                "critical": True,
                "category": "interaction",
                "requirement_id": "REQ-HISTORY",
                "impact_tags": ["route:/", "download-history"],
                "route": "/",
                "fixtures": [],
                "actions": [
                    {
                        "action": "assert_text",
                        "selector": "[data-testid='download-history-count']",
                        "value": "1",
                    }
                ],
            }
        ],
    }


def _prepared_edit(tmp_path: Path) -> FileComm:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<main>Downloads</main>", encoding="utf-8")
    prepare_edit_task_contract(tmp_path, requested_target_routes=["/"])
    file_comm = FileComm(tmp_path / ".harness")
    plan = _plan()
    write_atomic_edit_plan(file_comm.dir, plan, instruction_delta=INSTRUCTION)
    materialize_atomic_edit_compatibility_bundle(
        file_comm=file_comm,
        instruction_delta=INSTRUCTION,
        plan=plan,
    )
    return file_comm


def test_freezes_tests_against_clean_source_before_target_exists(tmp_path: Path):
    file_comm = _prepared_edit(tmp_path)

    frozen = freeze_preimplementation_validation(
        workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
    )

    assert frozen["schema_version"] == "edit-freeze-v1"
    assert not (file_comm.dir / "preimplementation_validation.json").exists()
    assert file_comm.read_state()["edit_freeze"] == frozen
    verified = verify_preimplementation_validation(
        workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
    )
    assert frozen_checks_for_sprint(verified, 1)[0]["id"] == "UI-HISTORY"


def test_rejects_test_drift_after_freeze(tmp_path: Path):
    file_comm = _prepared_edit(tmp_path)
    freeze_preimplementation_validation(
        workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
    )
    plan = file_comm.read_ui_verification_plan()
    plan["sprints"][0]["checks"][0]["actions"][0]["value"] = "999"
    file_comm.write_ui_verification_plan(plan)

    with pytest.raises(
        PreimplementationValidationError,
        match="changed after tests were frozen",
    ):
        verify_preimplementation_validation(
            workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
        )


def test_refuses_to_freeze_after_candidate_mutation(tmp_path: Path):
    file_comm = _prepared_edit(tmp_path)
    (tmp_path / "frontend" / "index.html").write_text(
        "<main>Candidate target</main>", encoding="utf-8"
    )

    with pytest.raises(
        PreimplementationValidationError,
        match="already contains changes",
    ):
        freeze_preimplementation_validation(
            workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
        )


def test_rejects_hidden_oracle_added_after_target_freeze(tmp_path: Path):
    file_comm = _prepared_edit(tmp_path)
    freeze_preimplementation_validation(
        workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
    )
    write_hidden_oracle_checks(
        file_comm.dir,
        [
            {
                "id": "HIDDEN-HISTORY",
                "route": "/",
                "actions": [
                    {
                        "action": "assert_visible",
                        "selector": "[data-testid='download-history-count']",
                    }
                ],
            }
        ],
        target_routes=["/"],
    )

    with pytest.raises(
        PreimplementationValidationError,
        match="hidden_oracle_checks.json changed after tests were frozen",
    ):
        verify_preimplementation_validation(
            workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
        )


def test_rejects_source_edit_risk_profile_drift_after_freeze(tmp_path: Path):
    file_comm = _prepared_edit(tmp_path)
    materialize_edit_risk_tests(
        workdir=tmp_path, instruction_delta=INSTRUCTION
    )
    freeze_preimplementation_validation(
        workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
    )
    path = file_comm.dir / "edit_risk_tests.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["max_generated_checks"] += 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        PreimplementationValidationError,
        match="edit_risk_tests.json changed after tests were frozen",
    ):
        verify_preimplementation_validation(
            workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
        )


@pytest.mark.anyio
async def test_real_browser_runs_the_same_frozen_test_before_and_after_build(
    tmp_path: Path,
):
    file_comm = _prepared_edit(tmp_path)
    freeze_preimplementation_validation(
        workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
    )
    checks = frozen_checks_for_sprint(
        verify_preimplementation_validation(
            workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
        ),
        1,
    )

    async def page(_request):
        return web.Response(
            text=(tmp_path / "frontend" / "index.html").read_text(encoding="utf-8"),
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        before = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=checks,
            output_path=tmp_path / "before.json",
            headless=True,
        )
        (tmp_path / "frontend" / "index.html").write_text(
            "<main>Downloads <span data-testid='download-history-count'>1</span></main>",
            encoding="utf-8",
        )
        verify_preimplementation_validation(
            workdir=tmp_path, file_comm=file_comm, instruction_delta=INSTRUCTION
        )
        after = await collect_browser_evidence(
            app_url=f"http://127.0.0.1:{port}",
            checks=checks,
            output_path=tmp_path / "after.json",
            headless=True,
        )
    finally:
        await runner.cleanup()

    assert before["checks"][0]["status"] == "action_failed"
    assert after["checks"][0]["status"] == "ok"
