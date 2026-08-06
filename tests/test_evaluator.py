from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk.types import ResultMessage

from src.agents.evaluator import _determine_passed, _extract_grades_from_response, run_evaluator
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.sprint_state import SprintState


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
        "edit_scope_audit": "pass",
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
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.3,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.evaluator.run_sdk_agent", fake_run_sdk_agent)

    passed, grades, stats = await run_evaluator(
        HarnessConfig(evaluator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=2,
        app_url="http://127.0.0.1:4173",
    )

    assert passed is True
    assert grades["mode_recommendation"] == "generate_next_sprint"
    # claude-sonnet-4-6 at $3 per 1M input tokens → 100_000 * 3 / 1e6 = $0.30.
    # Matches the legacy 0.3 by coincidence but is now derived from the
    # local pricing table.
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
    assert ".claude/skills/webapp-testing/SKILL.md" in captured["prompt"]
    assert "Phase A: Render Gate" in captured["prompt"]
    assert "Phase B: UI Functionality Verification" in captured["prompt"]
    assert "Phase C: Deferred Visual Review Capture" in captured["prompt"]
    assert "Phase E: Score Aggregation And Verdict" in captured["prompt"]
    assert "3. .harness/visual_manifest_round_2.json" in captured["prompt"]
    assert ".harness/visual_round_2_home.png" in captured["prompt"]
    assert "downstream VLM review" in captured["prompt"]


def test_evaluator_prompt_requires_independent_edit_scope_audit(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)
    from src.agents.evaluator import _build_evaluator_prompt

    prompt = _build_evaluator_prompt(
        file_comm=file_comm, workdir=tmp_path, round_num=1, sprint_num=1,
        sprint_run_context=SprintState.load(file_comm).current_run_context(),
        app_url="http://127.0.0.1:4173",
        edit_guard={"passed": True, "allowed_root_keys": ["main"]},
    )
    assert ".harness/edit_dom_baseline.json" in prompt
    assert ".harness/edit_scope_round_1.json" in prompt
    assert "Edit Scope Contract (independent audit required)" in prompt


@pytest.mark.anyio
async def test_evaluator_prompt_includes_design_contract_reads_when_present(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)
    file_comm.write_design_brief(
        {
            "requested_mode": "image-first",
            "visual_strategy": "image_backed_ui",
            "reference_files": {"background_ui": ".harness/design/background_ui.png"},
            "aesthetic_intent": {"design_hypothesis": "Use asymmetry."},
            "responsive_strategy": {"desktop": "Layered", "mobile": "Stacked"},
            "overlay_regions": [{"id": "hero"}],
            "visual_success_criteria": ["Preserve hierarchy."],
            "implementation_rules": ["Keep text in HTML."],
        }
    )
    file_comm.write_layout_contract(
        {
            "viewport_targets": ["1440x900"],
            "regions": [{"id": "hero"}],
            "safe_zones": [],
            "forbidden_overlay_zones": [],
            "asset_fit": {"background_ui": "cover"},
            "responsive_rules": ["Keep controls visible."],
        }
    )
    file_comm.write_asset_manifest(
        {
            "assets": [{"id": "background_ui"}],
            "generation_records": [],
            "implementation_notes": ["Copy production assets."],
        }
    )
    captured: dict[str, str] = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        file_comm.write_grades(1, _passing_grades(1))
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.3,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.3,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.evaluator.run_sdk_agent", fake_run_sdk_agent)

    await run_evaluator(
        HarnessConfig(evaluator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=1,
        app_url="http://127.0.0.1:4173",
    )

    assert "- .harness/design/design_brief.json" in captured["prompt"]
    assert "- .harness/design/layout_contract.json" in captured["prompt"]
    assert "- .harness/design/asset_manifest.json" in captured["prompt"]
    assert "Design Contract Assessment:" in captured["prompt"]


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
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.3,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.evaluator.run_sdk_agent", fake_run_sdk_agent)

    passed, grades, stats = await run_evaluator(
        HarnessConfig(evaluator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=1,
        app_url="http://127.0.0.1:4173",
    )

    assert passed is False
    assert grades["overall_passed"] is False
    assert grades["mode_recommendation"] == "repair"
    assert grades["phase_results"]["ui_functionality"] == "pass"
    # Local pricing: claude-sonnet-4-6 input @ $3 / 1M → 100_000 * 3 / 1e6 = $0.30.
    assert stats.cost_usd == 0.3


@pytest.mark.anyio
async def test_evaluator_exposes_repo_local_claude_skills_to_workdir(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_evaluator_context(file_comm)

    source_skills = tmp_path / "source-skills"
    (source_skills / "webapp-testing").mkdir(parents=True)
    (source_skills / "webapp-testing" / "SKILL.md").write_text("# webapp testing skill\n")
    monkeypatch.setattr("src.agents.evaluator._LOCAL_CLAUDE_SKILLS_DIR", source_skills)

    async def fake_run_sdk_agent(**kwargs):
        file_comm.write_grades(1, _passing_grades(1))
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.3,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.3,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.evaluator.run_sdk_agent", fake_run_sdk_agent)

    await run_evaluator(
        HarnessConfig(evaluator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=1,
        app_url="http://127.0.0.1:4173",
    )

    exposed = tmp_path / ".claude" / "skills"
    assert exposed.exists()
    if exposed.is_symlink():
        assert exposed.resolve() == source_skills.resolve()
    else:
        assert (exposed / "webapp-testing" / "SKILL.md").read_text() == "# webapp testing skill\n"


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


# --- tri-state robustness against agent-written strings ---


@pytest.mark.parametrize("critical_value", [True, "true", "True", "TRUE", "yes", 1])
def test_determine_passed_rejects_truthy_string_critical_with_failing_ui_check(critical_value):
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["ui_checks"][0]["critical"] = critical_value
    grades["ui_checks"][0]["status"] = "fail"

    assert _determine_passed(grades) is False


@pytest.mark.parametrize("passed_value", [False, "false", "False", "no", 0])
def test_determine_passed_rejects_falsey_string_passed_on_critical_exit_criterion(passed_value):
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["target_exit_criteria_results"][0]["critical"] = True
    grades["target_exit_criteria_results"][0]["passed"] = passed_value

    assert _determine_passed(grades) is False


@pytest.mark.parametrize("overall_value", ["false", "False", "no", 0])
def test_determine_passed_treats_falsey_string_overall_as_fail(overall_value):
    grades = _passing_grades(1)
    grades["overall_passed"] = overall_value

    assert _determine_passed(grades) is False


@pytest.mark.parametrize("sprint_value", ["false", "False", "no", 0])
def test_determine_passed_treats_falsey_string_sprint_passed_as_fail(sprint_value):
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["sprint_passed"] = sprint_value

    assert _determine_passed(grades) is False


def test_determine_passed_accepts_status_failed_synonym_on_critical_check():
    grades = _passing_grades(1)
    grades.pop("overall_passed")
    grades["ui_checks"][0]["status"] = "FAILED"

    assert _determine_passed(grades) is False


# --- grade extraction picks the right JSON among multiples ---


def test_extract_grades_from_response_picks_grade_among_explanatory_objects():
    text = (
        "I had to retry once. Here is my reasoning:\n"
        '{"explanation": "first attempt failed", "retry": true}\n'
        "Final grade JSON for grade_round_4.json:\n"
        '{"round": 4, "criteria": {"design_quality": {"score": 7.0, "passed": true},'
        ' "functionality": {"score": 8.0, "passed": true},'
        ' "originality": {"score": 6.0, "passed": true},'
        ' "craft": {"score": 7.0, "passed": true}},'
        ' "phase_results": {"render_gate": "pass"}}'
    )
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    extracted = _extract_grades_from_response(response)

    assert extracted is not None
    assert extracted["round"] == 4
    assert extracted["criteria"]["design_quality"]["score"] == 7.0
    # Must NOT have picked the explanatory object.
    assert "explanation" not in extracted


def test_extract_grades_from_response_returns_none_when_only_noise_objects():
    text = '{"explanation": "no grade yet"}\n{"another": "non-grade"}'
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    assert _extract_grades_from_response(response) is None


def test_extract_grades_from_response_handles_truncated_trailing_json():
    # A common LLM failure: max_tokens cuts the JSON mid-write.
    text = (
        '{"round": 1, "criteria": {"design_quality": {"score": 7.0, "passed": true},'
        ' "functionality": {"score": 8.0, "passed": true},'
        ' "originality": {"score": 6.0, "passed": true},'
        ' "craft": {"score": 7.0, "passed": true}}}\n'
        '{"truncated": "missin'  # <- truncated here, no closing
    )
    response = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])

    extracted = _extract_grades_from_response(response)
    assert extracted is not None
    assert extracted["round"] == 1
