from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk.types import ResultMessage

from src.agents.planner import (
    PlannerValidationError,
    _make_planner_stop_hook,
    _validate_planning_bundle,
    run_planner,
)
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
    file_comm.write_spec(_valid_spec_text())
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
            "visual_experiment": {
                "design_hypothesis": "Use poster-like asymmetry.",
                "reason_for_image_first": "Text-only outputs stay too templated.",
                "desired_break_from_web_templates": ["poster-like asymmetry"],
                "visual_opportunities_beyond_css": ["ink texture"],
                "forbidden_generic_patterns": ["centered card grid"],
            },
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


def test_final_project_mode_instruction_requests_natural_complete_roadmap():
    from src.agents.planner import _build_planner_prompt

    prompt = _build_planner_prompt(
        HarnessConfig(final_project_mode=True), "build everything", Path("/tmp/work")
    )
    assert "natural number of Sprints" in prompt
    assert "complete requested product" in prompt
    assert "exactly one Sprint" not in prompt


@pytest.mark.anyio
async def test_planner_initializes_accepted_sprints_after_successful_run(
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
                usage={"input_tokens": 100_000},
                result=_valid_spec_text(),
            ),
            0.1,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.planner.run_sdk_agent", fake_run_sdk_agent)

    stats = await run_planner(
        HarnessConfig(planner_model="claude-sonnet-4-6"),
        "build a counter app",
        file_comm,
        tmp_path,
    )

    # claude-sonnet-4-6 at $3 per 1M input tokens
    # → 100_000 * 3 / 1e6 = $0.30.
    assert stats.cost_usd == 0.3
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
        file_comm.write_spec(_valid_spec_text())
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

    with pytest.raises(PlannerValidationError, match="design_tokens.json"):
        await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)


@pytest.mark.anyio
async def test_planner_raises_when_planning_bundle_is_malformed(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_spec(_valid_spec_text())
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
                "visual_experiment": {
                    "design_hypothesis": "Use poster-like asymmetry.",
                    "reason_for_image_first": "Text-only outputs stay too templated.",
                    "desired_break_from_web_templates": ["poster-like asymmetry"],
                    "visual_opportunities_beyond_css": ["ink texture"],
                    "forbidden_generic_patterns": ["centered card grid"],
                },
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

    with pytest.raises(PlannerValidationError, match="F001"):
        await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)


@pytest.mark.anyio
async def test_planner_rejects_missing_visual_experiment(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_spec(_valid_spec_text())
        (file_comm.dir / "design_tokens.json").write_text(
            json.dumps(
                {
                    "theme_name": "editorial counter",
                    "color": {"bg": "#111111"},
                    "typography": {"display": "Space Grotesk"},
                    "spacing": {"base": 8},
                    "radius": {"card": 16},
                    "motion": {"duration_fast": 160},
                    "style_rules": ["bold hierarchy"],
                    "anti_patterns": ["generic cards"],
                    "visual_experiment": {},
                }
            ),
            encoding="utf-8",
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

    with pytest.raises(PlannerValidationError, match="schema validation"):
        await run_planner(HarnessConfig(), "build a counter app", file_comm, tmp_path)


@pytest.mark.anyio
async def test_planner_prompt_explicitly_forbids_bash_and_uses_precreated_artifacts(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    captured: dict[str, str] = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        captured["system_prompt"] = kwargs["system_prompt"]
        captured["stop_hooks"] = kwargs["stop_hooks"]
        assert file_comm.read_spec().startswith("# Draft Product - Working Title")
        assert file_comm.read_progress() == "# Progress Log\n"
        assert (file_comm.dir / "design_tokens.json").exists() is True
        assert (file_comm.dir / "feature_list.json").exists() is True
        assert (file_comm.dir / "sprint_plan.json").exists() is True
        assert (file_comm.dir / "ui_verification_plan.json").exists() is True
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
    assert "The Harness has already prepared the workdir, the .harness directory, and the required artifact files." in captured["prompt"]
    assert "Replace the scaffold content in those files" in captured["prompt"]
    assert "`Bash` is unavailable for this task." in captured["system_prompt"]
    assert "The Harness prepares the workdir, the `.harness/` directory, and the required artifact" in captured["system_prompt"]
    assert "Use `total_sprints` exactly as written, never `total_sprint`." in captured["system_prompt"]
    assert "Every sprint entry must include at least one item in `feature_ids`" in captured["system_prompt"]
    assert "Before finishing, reread every required file under `.harness`" in captured["system_prompt"]
    assert len(captured["stop_hooks"]) == 1


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
        assert (harness_dir / "spec.md").exists() is True
        assert (harness_dir / "design_tokens.json").exists() is True
        assert (harness_dir / "feature_list.json").exists() is True
        assert (harness_dir / "sprint_plan.json").exists() is True
        assert (harness_dir / "ui_verification_plan.json").exists() is True
        assert (harness_dir / "progress.md").exists() is True
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


@pytest.mark.anyio
async def test_planner_stop_hook_blocks_invalid_bundle(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.initialize_planning_artifacts()
    hook = _make_planner_stop_hook(file_comm, HarnessConfig())

    result = await hook({}, None, None)

    assert result["decision"] == "block"
    assert "Planning artifact validation failed" in result["reason"]
    assert ".harness/spec.md" in result["reason"]


@pytest.mark.anyio
async def test_planner_stop_hook_allows_valid_bundle(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_valid_planning_bundle(file_comm)
    hook = _make_planner_stop_hook(file_comm, HarnessConfig())

    result = await hook({}, None, None)

    assert result == {"continue_": True}


# --- cross-ref consistency between the three plan files ---


def _seed_valid_bundle(file_comm: FileComm) -> None:
    """Write a self-consistent planning bundle with two features and two sprints."""
    file_comm.write_spec(_valid_spec_text())
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
            "visual_experiment": {
                "design_hypothesis": "Use poster-like asymmetry.",
                "reason_for_image_first": "Text-only outputs stay too templated.",
                "desired_break_from_web_templates": ["poster-like asymmetry"],
                "visual_opportunities_beyond_css": ["ink texture"],
                "forbidden_generic_patterns": ["centered card grid"],
            },
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
                },
                {
                    "id": "F002",
                    "name": "Polish",
                    "priority": "medium",
                    "depends_on": ["F001"],
                    "description": "Animate.",
                    "acceptance_criteria": ["Animation runs."],
                    "status": "planned",
                    "sprint": 2,
                },
            ]
        }
    )
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
                    "title": "Polish",
                    "goal": "Add motion.",
                    "feature_ids": ["F002"],
                    "deliverables": ["Animated counter."],
                    "exit_criteria": ["Animation runs."],
                },
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
                },
                {
                    "sprint": 2,
                    "checks": [
                        {
                            "id": "UI-002",
                            "feature_id": "F002",
                            "task": "Observe animation.",
                            "expected_result": "Counter animates.",
                            "critical": False,
                            "category": "appearance",
                        }
                    ],
                },
            ]
        }
    )
    file_comm.write_progress("# Progress Log\n\n## planning\n- status: complete")


def test_validate_planning_bundle_passes_for_consistent_bundle(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)
    # No exception → bundle is internally consistent.
    _validate_planning_bundle(file_comm)
    assert file_comm.read_sprint_plan()["total_sprints"] == 2


def test_validate_planning_bundle_rejects_dangling_sprint_feature_id(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["feature_ids"] = ["F999"]  # not in feature_list
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match="F999"):
        _validate_planning_bundle(file_comm)


def test_validate_planning_bundle_rejects_dangling_ui_check_feature_id(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    plan = file_comm.read_ui_verification_plan()
    plan["sprints"][0]["checks"][0]["feature_id"] = "F404"
    file_comm.write_ui_verification_plan(plan)

    with pytest.raises(PlannerValidationError, match="F404"):
        _validate_planning_bundle(file_comm)


def test_validate_planning_bundle_rejects_feature_assigned_to_unknown_sprint(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    feature_list = file_comm.read_feature_list()
    feature_list["features"][0]["sprint"] = 99  # outside total_sprints=2
    file_comm.write_feature_list(feature_list)

    with pytest.raises(PlannerValidationError, match="sprint"):
        _validate_planning_bundle(file_comm)


def test_validate_planning_bundle_rejects_sprint_plan_missing_a_sprint_number(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    # total_sprints=2 but sprint_plan only declares sprint 1.
    sprint_plan["sprints"] = [sprint_plan["sprints"][0]]
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match="sprint"):
        _validate_planning_bundle(file_comm)


# --- sprint sizing caps ---


def test_validate_sprint_plan_rejects_too_many_deliverables(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["deliverables"] = [f"Deliverable {i}" for i in range(6)]
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match=r"deliverables.*max allowed is 5"):
        _validate_planning_bundle(file_comm)


def test_validate_sprint_plan_rejects_too_many_exit_criteria(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["exit_criteria"] = [f"Criterion {i}" for i in range(6)]
    file_comm.write_sprint_plan(sprint_plan)

    with pytest.raises(PlannerValidationError, match=r"exit_criteria.*max allowed is 5"):
        _validate_planning_bundle(file_comm)


def test_validate_sprint_plan_accepts_at_cap(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["deliverables"] = [f"D{i}" for i in range(5)]
    sprint_plan["sprints"][0]["exit_criteria"] = [f"C{i}" for i in range(5)]
    file_comm.write_sprint_plan(sprint_plan)

    _validate_planning_bundle(file_comm)


def test_validate_sprint_plan_respects_config_override_for_caps(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)

    sprint_plan = file_comm.read_sprint_plan()
    sprint_plan["sprints"][0]["deliverables"] = [f"D{i}" for i in range(8)]
    file_comm.write_sprint_plan(sprint_plan)

    relaxed = HarnessConfig(max_deliverables_per_sprint=8)
    _validate_planning_bundle(file_comm, relaxed)


def test_expansive_data_enforces_three_item_sprint_cap(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _seed_valid_bundle(file_comm)
    sprint_plan = file_comm.read_sprint_plan()
    feature_list = file_comm.read_feature_list()
    feature_id = feature_list["features"][0]["id"]
    sprint_plan["total_sprints"] = 6
    template = sprint_plan["sprints"][0]
    sprint_plan["sprints"] = []
    for number in range(1, 7):
        item = dict(template)
        item["number"] = number
        item["feature_ids"] = [feature_id]
        item["deliverables"] = [f"D{i}" for i in range(5)]
        sprint_plan["sprints"].append(item)
    file_comm.write_sprint_plan(sprint_plan)
    feature_list["features"][0]["sprint"] = 1
    file_comm.write_feature_list(feature_list)
    verification = file_comm.read_ui_verification_plan()
    verification["sprints"] = [verification["sprints"][0]]
    file_comm.write_ui_verification_plan(verification)

    config = HarnessConfig(planner_scope_mode="expansive-data")
    with pytest.raises(PlannerValidationError, match=r"deliverables.*max allowed is 3"):
        _validate_planning_bundle(file_comm, config)


def test_planner_prompt_documents_sprint_size_caps():
    from src.prompts.planner import PLANNER_SYSTEM_PROMPT

    # Hard cap on per-sprint scope must be visible in the system prompt so
    # the planner doesn't ship 8-deliverable mega-sprints (see test-chunk-1-c2).
    assert "5 deliverables" in PLANNER_SYSTEM_PROMPT
    assert "5 exit_criteria" in PLANNER_SYSTEM_PROMPT
    assert "vertical slice" in PLANNER_SYSTEM_PROMPT.lower()


def test_expansive_data_scope_uses_shallow_natural_sprint_expansion():
    from src.prompts.planner import planner_system_prompt

    prompt = planner_system_prompt("expansive-data")
    assert "6-9 dependency-ordered Sprints" in prompt
    assert "2-3 closely related user-visible deliverables" in prompt
    assert 'standalone\n   "polish/refactor/cleanup" Sprint' in prompt
    assert "generate/edit" in prompt


def test_query_aligned_scope_does_not_enable_legacy_expansion():
    from src.prompts.planner import planner_system_prompt

    prompt = planner_system_prompt("query-aligned")
    assert "Scope Profile: Expansive Data Construction" not in prompt
