from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk.types import ResultMessage

from src.agents.generator import run_generator
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _write_generator_context(file_comm: FileComm) -> None:
    file_comm.write_sprint_plan(
        {
            "total_sprints": 2,
            "sprints": [
                {
                    "number": 1,
                    "title": "Core counter",
                    "goal": "Ship the primary counter flow.",
                    "feature_ids": ["F001"],
                    "deliverables": ["Visible counter UI."],
                    "exit_criteria": ["Counter increments correctly."],
                },
                {
                    "number": 2,
                    "title": "Refine interactions",
                    "goal": "Repair and polish the interaction flow.",
                    "feature_ids": ["F002"],
                    "deliverables": ["Repair evaluator findings."],
                    "exit_criteria": ["Reset works correctly."],
                },
            ],
        }
    )
    file_comm.write_accepted_sprints(
        {
            "accepted": [],
            "current_target": 1,
            "last_evaluated_round": 0,
        }
    )


@pytest.mark.anyio
async def test_generator_generate_mode_builds_sprint_scoped_prompt(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    captured: dict = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    stats = await run_generator(
        HarnessConfig(generator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=1,
        sprint_num=1,
        mode="generate",
    )

    # claude-sonnet-4-6 at $3 per 1M input tokens
    # → 100_000 * 3 / 1e6 = $0.30.
    assert stats.cost_usd == 0.3
    assert stats.duration_ms == 1
    assert "Mode: generate" in captured["prompt"]
    assert "Sprint: 1" in captured["prompt"]
    assert "Sprint Title: Core counter" in captured["prompt"]
    assert "Target Feature IDs: F001" in captured["prompt"]
    assert "Required Reads:" in captured["prompt"]
    assert "- .harness/sprint_plan.json" in captured["prompt"]
    assert "- .harness/feature_list.json" in captured["prompt"]
    assert "- .harness/design_tokens.json" in captured["prompt"]
    assert "- .harness/accepted_sprints.json" in captured["prompt"]
    assert "Do not implement future sprint functionality or unrelated refactors." in captured["prompt"]
    assert "Read the planning bundle first" not in captured["prompt"]
    assert ".harness/spec.md" not in captured["prompt"]
    assert ".harness/ui_verification_plan.json" not in captured["prompt"]


@pytest.mark.anyio
async def test_generator_repair_mode_builds_feedback_scoped_prompt(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    file_comm.write_feedback(1, "Fix reset interaction.")
    file_comm.write_grades(
        1,
        {
            "round": 1,
            "overall_passed": False,
            "ui_checks": [
                {
                    "check_id": "UI-001",
                    "feature_id": "F001",
                    "critical": True,
                    "status": "fail",
                    "task": "Click increment once.",
                },
                {
                    "check_id": "UI-099",
                    "feature_id": "F999",
                    "critical": False,
                    "status": "fail",
                    "task": "Unrelated future sprint check.",
                },
                {
                    "check_id": "UI-002",
                    "feature_id": "F001",
                    "critical": False,
                    "status": "partial",
                    "task": "Audio output check.",
                },
            ],
            "target_exit_criteria_results": [
                {
                    "criterion_id": "EXIT-01-01",
                    "feature_id": "F001",
                    "critical": True,
                    "passed": False,
                    "criterion": "Counter increments correctly.",
                }
            ],
        },
    )
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    captured: dict = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    stats = await run_generator(
        HarnessConfig(generator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=2,
        sprint_num=1,
        mode="repair",
    )

    # claude-sonnet-4-6 at $3 per 1M input tokens → 100_000 * 3 / 1e6 = $0.30.
    assert stats.cost_usd == 0.3
    assert stats.duration_ms == 1
    assert "Mode: repair" in captured["prompt"]
    assert "Sprint: 1" in captured["prompt"]
    assert "Sprint Title: Core counter" in captured["prompt"]
    assert "Repair Scope: Fix evaluator-reported issues for the current sprint only" in captured["prompt"]
    assert "Affected Feature IDs: F001" in captured["prompt"]
    assert "Failed Exit Criteria:" in captured["prompt"]
    assert "EXIT-01-01 | feature_id=F001 | critical=True | criterion=Counter increments correctly." in captured["prompt"]
    assert "Failed UI Checks:" in captured["prompt"]
    assert "UI-001 | feature_id=F001 | critical=True | status=fail | task=Click increment once." in captured["prompt"]
    assert "UI-099" not in captured["prompt"]
    assert "Audio output check." not in captured["prompt"]
    assert "Required Reads:" in captured["prompt"]
    assert ".harness/feedback_round_1.md" in captured["prompt"]
    assert ".harness/grade_round_1.json" in captured["prompt"]
    assert "- .harness/sprint_plan.json" in captured["prompt"]
    assert "- .harness/design_tokens.json" in captured["prompt"]
    assert "- .harness/accepted_sprints.json" in captured["prompt"]
    assert "Do not implement new features from future sprints." in captured["prompt"]
    assert "Do not start work for the next sprint." in captured["prompt"]
    assert ".harness/spec.md" not in captured["prompt"]
    assert ".harness/feature_list.json" not in captured["prompt"]


@pytest.mark.anyio
async def test_generator_raises_when_expected_dirs_are_missing(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)

    async def fake_run_sdk_agent(**kwargs):
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                result="I created some files elsewhere.",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(RuntimeError, match="frontend"):
        await run_generator(
            HarnessConfig(),
            file_comm,
            tmp_path,
            round_num=1,
            sprint_num=1,
            mode="generate",
        )

    assert "I created some files elsewhere." in file_comm.read_build_log()


# --- empty frontend dir must not be treated as success ---


@pytest.mark.anyio
async def test_generator_raises_when_frontend_dir_is_empty(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    # Frontend dir exists but contains no package.json — agent exited too early.
    (tmp_path / "frontend").mkdir()

    async def fake_run_sdk_agent(**kwargs):
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                result="created folder",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(RuntimeError, match="package.json"):
        await run_generator(
            HarnessConfig(),
            file_comm,
            tmp_path,
            round_num=1,
            sprint_num=1,
            mode="generate",
        )


# --- repair mode must not silently degrade to no-direction generate ---


@pytest.mark.anyio
async def test_generator_repair_raises_when_previous_grade_missing(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    # NB: grade_round_1.json deliberately not written.

    sdk_called = {"value": False}

    async def fake_run_sdk_agent(**kwargs):
        sdk_called["value"] = True
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.0,
                result="should not run",
            ),
            0.0,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(RuntimeError, match="grade_round_1"):
        await run_generator(
            HarnessConfig(),
            file_comm,
            tmp_path,
            round_num=2,
            sprint_num=1,
            mode="repair",
        )

    assert sdk_called["value"] is False, "Repair must not call the SDK when its inputs are missing"
