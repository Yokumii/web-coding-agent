from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import ResultMessage

from src.agents.evaluator import _determine_passed, _extract_grades_from_response, run_evaluator
from src.agents.sdk_runner import AgentRunStats
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _write_evaluator_context(file_comm: FileComm) -> None:
    file_comm.write_spec("# Product Spec\n\n## Product Overview\nBrowser app.")
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
                    "status": "in_progress",
                    "sprint": 1,
                }
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
                    "title": "Polish counter",
                    "goal": "Improve presentation quality.",
                    "feature_ids": ["F002"],
                    "deliverables": ["Improve layout and polish."],
                    "exit_criteria": ["Layout is stable across breakpoints."],
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
                }
            ]
        }
    )
    file_comm.write_accepted_sprints(
        {
            "accepted": [],
            "current_target": 1,
            "last_evaluated_round": 0,
        }
    )


def _passing_grades(round_num: int = 1) -> dict:
    return {
        "round": round_num,
        "sprint": 1,
        "mode_recommendation": "generate_next_sprint",
        "phase_results": {
            "render_gate": "pass",
            "ui_functionality": "pass",
            "appearance": "pass",
            "source_inspection": "skipped",
        },
        "sprint_passed": True,
        "regression_passed": True,
        "overall_passed": True,
        "criteria": {
            "design_quality": {"score": 7.0, "passed": True, "notes": "Distinct visual identity."},
            "functionality": {"score": 8.0, "passed": True, "notes": "Core task works."},
            "originality": {"score": 6.0, "passed": True, "notes": "Intentional choices."},
            "craft": {"score": 7.0, "passed": True, "notes": "Polished layout."},
        },
        "target_exit_criteria_results": [
            {
                "criterion_id": "EXIT-01-01",
                "feature_id": "F001",
                "critical": True,
                "criterion": "Counter increments correctly.",
                "passed": True,
                "notes": "Verified in browser.",
            }
        ],
        "ui_checks": [
            {
                "check_id": "UI-001",
                "feature_id": "F001",
                "critical": True,
                "task": "Click increment once.",
                "expected_result": "Counter changes by one step.",
                "status": "pass",
                "notes": "Worked in browser.",
            }
        ],
        "appearance_review": {
            "screenshots": [f"round_{round_num}_home.png"],
            "render_stability": 4,
            "content_relevance": 4,
            "layout_harmony": 4,
            "modernness_memorability": 4,
            "token_adherence": 4,
            "notes": "Solid visual consistency.",
        },
        "bugs_found": [],
        "regressions_found": [],
        "missing_features": [],
        "repair_instructions": [],
    }


@pytest.mark.anyio
async def test_evaluator_builds_staged_prompt_with_sprint_context(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)
    file_comm.write_feedback(1, "# Round 1 Feedback")
    file_comm.write_grades(1, _passing_grades(1))
    captured: dict = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        file_comm.write_grades(2, _passing_grades(2))
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.3,
                result="done",
            ),
            0.3,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.evaluator.run_sdk_agent", fake_run_sdk_agent)

    passed, grades, stats = await run_evaluator(
        HarnessConfig(),
        file_comm,
        tmp_path,
        round_num=2,
        app_url="http://127.0.0.1:4173",
    )

    assert passed is True
    assert grades["mode_recommendation"] == "generate_next_sprint"
    assert stats.cost_usd == 0.3
    assert stats.duration_ms == 1
    assert "Application URL: http://127.0.0.1:4173" in captured["prompt"]
    assert "Round: 2" in captured["prompt"]
    assert "Sprint: 1" in captured["prompt"]
    assert "Sprint Title: Core counter" in captured["prompt"]
    assert "Sprint Goal: Ship the primary counter flow." in captured["prompt"]
    assert "Target Feature IDs: F001" in captured["prompt"]
    assert "Exit Criterion Feature Mapping:" in captured["prompt"]
    assert "criterion_id=EXIT-01-01 | feature_id=F001 | critical=True | criterion=Counter increments correctly." in captured["prompt"]
    assert "Current Sprint UI Verification Checks:" in captured["prompt"]
    assert "check_id=UI-001 | feature_id=F001 | critical=True" in captured["prompt"]
    assert "task=Click increment once." in captured["prompt"]
    assert "expected=Counter changes by one step." in captured["prompt"]
    assert "Required Reads:" in captured["prompt"]
    assert "- .harness/spec.md" in captured["prompt"]
    assert "- .harness/design_tokens.json" in captured["prompt"]
    assert "- .harness/feature_list.json" in captured["prompt"]
    assert "- .harness/sprint_plan.json" in captured["prompt"]
    assert "- .harness/ui_verification_plan.json" in captured["prompt"]
    assert "- .harness/accepted_sprints.json" in captured["prompt"]
    assert "- .harness/feedback_round_1.md" in captured["prompt"]
    assert "- .harness/grade_round_1.json" in captured["prompt"]
    assert "Phase A: Render Gate" in captured["prompt"]
    assert "Phase B: UI Functionality Verification" in captured["prompt"]
    assert "Phase C: External Appearance Review Placeholder" in captured["prompt"]
    assert "Phase E: Score Aggregation And Verdict" in captured["prompt"]


@pytest.mark.anyio
async def test_evaluator_reads_written_grade_file_and_uses_overall_verdict(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)

    failing_grades = _passing_grades(1)
    failing_grades["overall_passed"] = False
    failing_grades["mode_recommendation"] = "repair"
    failing_grades["criteria"]["functionality"] = {
        "score": 5.0,
        "passed": False,
        "notes": "Critical interaction is broken.",
    }

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_grades(1, failing_grades)
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.3,
                result="done",
            ),
            0.3,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.evaluator.run_sdk_agent", fake_run_sdk_agent)

    passed, grades, stats = await run_evaluator(
        HarnessConfig(),
        file_comm,
        tmp_path,
        round_num=1,
        app_url="http://127.0.0.1:4173",
    )

    assert passed is False
    assert grades["overall_passed"] is False
    assert grades["mode_recommendation"] == "repair"
    assert grades["phase_results"]["ui_functionality"] == "pass"
    assert stats.cost_usd == 0.3


def test_extract_grades_from_response_parses_embedded_json():
    grades = _passing_grades(3)
    response = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="text",
                text=(
                    "Wrote .harness/grade_round_3.json\n"
                    f"{grades}"
                ).replace("'", '"').replace("True", "true").replace("False", "false")
            )
        ],
    )

    extracted = _extract_grades_from_response(response)

    assert extracted is not None
    assert extracted["round"] == 3
    assert extracted["criteria"]["functionality"]["score"] == 8.0
    assert extracted["target_exit_criteria_results"][0]["feature_id"] == "F001"
    assert extracted["target_exit_criteria_results"][0]["criterion_id"] == "EXIT-01-01"
    assert extracted["ui_checks"][0]["check_id"] == "UI-001"


def test_determine_passed_rejects_failed_critical_ui_check_without_overall_flag():
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["ui_checks"][0]["status"] = "fail"

    assert _determine_passed(grades) is False


def test_determine_passed_rejects_failed_critical_exit_criterion_without_overall_flag():
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["target_exit_criteria_results"][0]["passed"] = False

    assert _determine_passed(grades) is False

