from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk.types import ResultMessage

from src.agents.planner import _normalize_spec_candidate, run_planner
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _valid_spec_text() -> str:
    return (
        "# Counter App - Track Every Tap\n\n"
        "## Product Overview\nA simple spec.\n\n"
        "## Target Users\nPeople who count.\n\n"
        "## Feature Descriptions\n"
        "### 1. Counter\n"
        "**Description:** Count.\n"
        "**User Stories:**\n"
        "- As a user, I want to count.\n"
        "**(Priority: High)**\n\n"
        "## Technical Architecture\nClient-only architecture.\n\n"
        "## Visual Design Direction\nMinimal but distinctive.\n"
    )


def _write_valid_planning_bundle(file_comm: FileComm) -> None:
    file_comm.write_design_tokens(
        {
            "theme_name": "editorial counter",
            "color": {"bg": "#111111"},
            "typography": {"display": "Space Grotesk"},
            "spacing": {"base": 8},
            "radius": {"card": 16},
            "motion": {"duration_fast": 160},
            "style_rules": ["bold hierarchy"],
            "anti_patterns": ["generic cards"],
        }
    )
    file_comm.write_feature_list(
        {
            "features": [
                {
                    "id": "F001",
                    "name": "Counter",
                    "priority": "high",
                    "depends_on": [],
                    "description": "Count values.",
                    "acceptance_criteria": ["Counter increments correctly."],
                    "status": "planned",
                    "sprint": 1,
                }
            ]
        }
    )
    file_comm.write_sprint_plan(
        {
            "total_sprints": 1,
            "sprints": [
                {
                    "number": 1,
                    "title": "Core counter",
                    "goal": "Ship the primary counter flow.",
                    "feature_ids": ["F001"],
                    "deliverables": ["Visible counter UI."],
                    "exit_criteria": ["Counter increments correctly."],
                }
            ],
        }
    )
    file_comm.write_ui_verification_plan(
        {
            "sprints": [
                {
                    "sprint": 1,
                    "checks": [
                        {
                            "id": "UI-001",
                            "feature_id": "F001",
                            "task": "Click increment once.",
                            "expected_result": "Counter changes by one step.",
                            "critical": True,
                            "category": "core_interaction",
                        }
                    ],
                }
            ]
        }
    )
    file_comm.write_progress("# Progress Log\n\n## planning\n- status: complete")


@pytest.mark.anyio
async def test_planner_uses_result_text_as_spec_fallback_and_initializes_accepted_sprints(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        _write_valid_planning_bundle(file_comm)
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    stats = await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)

    assert stats.cost_usd == 0.1
    assert stats.duration_ms == 1
    assert file_comm.read_spec().startswith("# Counter App - Track Every Tap")
    assert file_comm.read_accepted_sprints() == {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 0,
    }


@pytest.mark.anyio
async def test_planner_raises_when_planning_bundle_is_missing_required_artifact(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_progress("# Progress")
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(RuntimeError, match="design_tokens.json"):
        await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)


@pytest.mark.anyio
async def test_planner_raises_when_planning_bundle_is_malformed(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_design_tokens(
            {
                "theme_name": "editorial counter",
                "color": {"bg": "#111111"},
                "typography": {"display": "Space Grotesk"},
                "spacing": {"base": 8},
                "radius": {"card": 16},
                "motion": {"duration_fast": 160},
                "style_rules": ["bold hierarchy"],
                "anti_patterns": ["generic cards"],
            }
        )
        file_comm.write_feature_list({"features": []})
        file_comm.write_sprint_plan(
            {
                "total_sprints": 1,
                "sprints": [
                    {
                        "number": 1,
                        "title": "Core counter",
                        "goal": "Ship the primary counter flow.",
                        "feature_ids": ["F001"],
                        "deliverables": ["Visible counter UI."],
                        "exit_criteria": ["Counter increments correctly."],
                    }
                ],
            }
        )
        file_comm.write_ui_verification_plan(
            {
                "sprints": [
                    {
                        "sprint": 1,
                        "checks": [
                            {
                                "id": "UI-001",
                                "feature_id": "F001",
                                "task": "Click increment once.",
                                "expected_result": "Counter changes by one step.",
                                "critical": True,
                                "category": "core_interaction",
                            }
                        ],
                    }
                ]
            }
        )
        file_comm.write_progress("# Progress")
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(RuntimeError, match="feature_list.json.features"):
        await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)


@pytest.mark.anyio
async def test_planner_prompt_explicitly_forbids_bash_and_directory_creation(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    captured: dict[str, str] = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        captured["system_prompt"] = kwargs["system_prompt"]
        _write_valid_planning_bundle(file_comm)
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)

    assert "Bash is unavailable for this task." in captured["prompt"]
    assert "The Harness has already prepared the workdir and .harness directory for this task" in captured["prompt"]
    assert "`Bash` is unavailable for this task." in captured["system_prompt"]
    assert "The Harness prepares the workdir and `.harness/` directory before this task starts." in captured["system_prompt"]


@pytest.mark.anyio
async def test_planner_prepares_missing_workdir_and_harness_dir(monkeypatch, tmp_path: Path):
    workdir = tmp_path / "missing-workdir"
    file_comm = FileComm(workdir / ".harness")
    harness_dir = workdir / ".harness"
    if harness_dir.exists():
        harness_dir.rmdir()
    if workdir.exists():
        workdir.rmdir()

    async def fake_run_sdk_agent(**kwargs):
        assert workdir.exists() is True
        assert harness_dir.exists() is True
        _write_valid_planning_bundle(file_comm)
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.1,
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    await run_planner(HarnessConfig(), "build a counter app", file_comm, workdir)

    assert workdir.exists() is True
    assert harness_dir.exists() is True


def test_normalize_spec_candidate_rejects_chatty_fallback():
    assert _normalize_spec_candidate(
        "I'm encountering technical issues with the file writing.\n\nWould you like me to try again?"
    ) == ""
