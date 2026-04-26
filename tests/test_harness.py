from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.sdk_runner import AgentRunStats
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.harness import run_harness


class DummyAppStack:
    def __init__(self, frontend_url: str = "http://127.0.0.1:5173") -> None:
        self.frontend_url = frontend_url
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _visual_manifest(round_num: int) -> dict:
    return {
        "round": round_num,
        "app_url": "http://127.0.0.1:5173",
        "screenshots": [f".harness/visual_round_{round_num}_home.png"],
        "notes": "top view",
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _write_feature_list(file_comm: FileComm, total: int = 1) -> None:
    features = []
    for index in range(1, total + 1):
        features.append(
            {
                "id": f"F00{index}",
                "name": f"Feature {index}",
                "priority": "high",
                "depends_on": [],
                "description": f"Feature {index} description.",
                "acceptance_criteria": [f"Feature {index} works."],
                "status": "planned",
                "sprint": index,
            }
        )
    file_comm.write_feature_list({"features": features})


def _write_features(file_comm: FileComm, specs: list[tuple[str, int]]) -> None:
    features = []
    for index, (feature_id, sprint_num) in enumerate(specs, start=1):
        features.append(
            {
                "id": feature_id,
                "name": f"Feature {index}",
                "priority": "high",
                "depends_on": [],
                "description": f"Feature {index} description.",
                "acceptance_criteria": [f"Feature {index} works."],
                "status": "planned",
                "sprint": sprint_num,
            }
        )
    file_comm.write_feature_list({"features": features})


def _stats(
    cost_usd: float,
    *,
    duration_ms: int = 1000,
    duration_api_ms: int = 800,
    wall_duration_ms: int | None = None,
    token_usage: dict[str, int] | None = None,
) -> AgentRunStats:
    return AgentRunStats(
        cost_usd=cost_usd,
        duration_ms=duration_ms,
        duration_api_ms=duration_api_ms,
        token_usage=token_usage or {"input_tokens": 100, "output_tokens": 20},
        usage=(token_usage or {"input_tokens": 100, "output_tokens": 20}).copy(),
        model_usage={},
        wall_duration_ms=wall_duration_ms,
    )


@pytest.mark.anyio
async def test_resume_from_build_checkpoint_skips_planner_and_build(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, int | None]] = []
    stack = DummyAppStack()
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_sprint_plan(
        {"total_sprints": 1, "sprints": [{"number": 1, "feature_ids": ["F001"]}]}
    )
    _write_feature_list(file_comm)
    file_comm.write_accepted_sprints({"accepted": [], "current_target": 1, "last_evaluated_round": 1})
    file_comm.write_state({
        "last_completed_phase": "build_r2",
        "round_num": 2,
        "prompt": "saved prompt",
        "costs": {"planner": 1.25},
    })

    async def fake_planner(*args, **kwargs):
        calls.append(("planner", None))
        return 0.1

    async def fake_generator(*args, **kwargs):
        calls.append(("generator", kwargs["round_num"]))
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        calls.append(("evaluator", kwargs["round_num"]))
        return True, {}, 0.3

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        calls.append(("start_app_stack", round_num))
        return stack

    async def fake_visual_capture(config, file_comm, workdir, round_num, app_url):
        calls.append(("visual_capture", round_num))
        return _visual_manifest(round_num), _stats(0.05)

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.harness.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.harness.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.harness.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.harness.run_visual_capture", fake_visual_capture)
    monkeypatch.setattr("src.orchestration.harness.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.harness.start_app_stack", fake_start_app_stack)

    await run_harness("ignored prompt", tmp_path, HarnessConfig(max_rounds=3), resume=True)

    assert ("planner", None) not in calls
    assert ("generator", 2) not in calls
    assert ("start_app_stack", 2) in calls
    assert ("visual_capture", 2) in calls
    assert ("evaluator", 2) in calls
    assert stack.closed is True


@pytest.mark.anyio
async def test_resume_from_evaluate_checkpoint_starts_next_build_round(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, int | None]] = []
    stack = DummyAppStack()
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_sprint_plan(
        {"total_sprints": 2, "sprints": [{"number": 1, "feature_ids": ["F001"]}, {"number": 2, "feature_ids": ["F002"]}]}
    )
    _write_feature_list(file_comm, total=2)
    file_comm.write_accepted_sprints({"accepted": [1], "current_target": 2, "last_evaluated_round": 1})
    file_comm.write_state({
        "last_completed_phase": "evaluate_r1",
        "round_num": 1,
        "prompt": "saved prompt",
        "costs": {"planner": 1.25, "generator_r1": 2.0, "evaluator_r1": 0.5},
    })

    async def fake_planner(*args, **kwargs):
        calls.append(("planner", None))
        return 0.1

    async def fake_generator(*args, **kwargs):
        calls.append(("generator", kwargs["round_num"]))
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        calls.append(("evaluator", kwargs["round_num"]))
        return True, {}, 0.3

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        calls.append(("start_app_stack", round_num))
        return stack

    async def fake_visual_capture(config, file_comm, workdir, round_num, app_url):
        calls.append(("visual_capture", round_num))
        return _visual_manifest(round_num), _stats(0.05)

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.harness.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.harness.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.harness.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.harness.run_visual_capture", fake_visual_capture)
    monkeypatch.setattr("src.orchestration.harness.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.harness.start_app_stack", fake_start_app_stack)

    await run_harness("ignored prompt", tmp_path, HarnessConfig(max_rounds=3), resume=True)

    assert ("planner", None) not in calls
    assert ("generator", 2) in calls
    assert ("start_app_stack", 2) in calls
    assert ("visual_capture", 2) in calls
    assert ("evaluator", 2) in calls
    assert stack.closed is True


@pytest.mark.anyio
async def test_successful_evaluation_preserves_completed_checkpoint_and_closes_stack(monkeypatch, tmp_path: Path):
    stack = DummyAppStack()

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_sprint_plan(
            {
                "total_sprints": 1,
                "sprints": [
                    {
                        "number": 1,
                        "title": "Core flow",
                        "goal": "Ship the first sprint.",
                        "feature_ids": ["F001"],
                        "deliverables": ["Primary UI."],
                        "exit_criteria": ["Primary flow works."],
                    }
                ],
            }
        )
        file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        _write_feature_list(file_comm)
        return 0.1

    async def fake_generator(*args, **kwargs):
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        return (
            True,
            {
                "round": 1,
                "sprint": 1,
                "mode_recommendation": "complete",
                "overall_passed": True,
                "criteria": {
                    "design_quality": {"score": 7.0},
                    "functionality": {"score": 7.0},
                    "originality": {"score": 6.0},
                    "craft": {"score": 7.0},
                },
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_capture(config, file_comm, workdir, round_num, app_url):
        return _visual_manifest(round_num), _stats(0.05)

    async def fake_visual_review(**kwargs):
        grades = kwargs["grades"]
        grades["appearance_review"] = {
            "screenshots": [".harness/visual_round_1_home.png"],
            "render_stability": 4,
            "content_relevance": 4,
            "layout_harmony": 4,
            "modernness_memorability": 4,
            "token_adherence": 4,
            "notes": "Captured separately.",
        }
        return grades, _stats(0.0)

    monkeypatch.setattr("src.orchestration.harness.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.harness.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.harness.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.harness.run_visual_capture", fake_visual_capture)
    monkeypatch.setattr("src.orchestration.harness.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.harness.start_app_stack", fake_start_app_stack)

    await run_harness("build something", tmp_path, HarnessConfig(max_rounds=1))

    assert stack.closed is True
    state = FileComm(tmp_path / ".harness").read_state()
    assert state is not None
    assert state["last_completed_phase"] == "evaluate_r1"
    assert state["last_verdict"] == "completed"
    assert state["costs"] == {
        "planner": 0.1,
        "generator_r1": 0.2,
        "evaluator_r1": 0.3,
        "visual_capture_r1": 0.05,
        "visual_score_r1": 0.0,
    }


@pytest.mark.anyio
async def test_checkpoint_records_phase_metrics_with_tokens_and_durations(monkeypatch, tmp_path: Path):
    stack = DummyAppStack()

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_sprint_plan(
            {
                "total_sprints": 1,
                "sprints": [
                    {
                        "number": 1,
                        "title": "Core flow",
                        "goal": "Ship the first sprint.",
                        "feature_ids": ["F001"],
                        "deliverables": ["Primary UI."],
                        "exit_criteria": ["Primary flow works."],
                    }
                ],
            }
        )
        file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        _write_feature_list(file_comm)
        return _stats(0.1, duration_ms=1200, duration_api_ms=950, token_usage={"input_tokens": 111, "output_tokens": 22})

    async def fake_generator(*args, **kwargs):
        return _stats(0.2, duration_ms=2300, duration_api_ms=1800, token_usage={"input_tokens": 222, "output_tokens": 33})

    async def fake_evaluator(*args, **kwargs):
        return (
            False,
            {
                "round": 1,
                "sprint": 1,
                "mode_recommendation": "repair",
                "overall_passed": False,
                "criteria": {
                    "design_quality": {"score": 7.0},
                    "functionality": {"score": 5.0},
                    "originality": {"score": 6.0},
                    "craft": {"score": 7.0},
                },
            },
            _stats(0.3, duration_ms=3400, duration_api_ms=2900, token_usage={"input_tokens": 333, "output_tokens": 44}),
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_capture(config, file_comm, workdir, round_num, app_url):
        return _visual_manifest(round_num), _stats(0.05, duration_ms=900, duration_api_ms=700, token_usage={"input_tokens": 55, "output_tokens": 11})

    async def fake_visual_review(**kwargs):
        grades = kwargs["grades"]
        grades["appearance_review"] = {
            "screenshots": [".harness/visual_round_1_home.png"],
            "render_stability": 4,
            "content_relevance": 4,
            "layout_harmony": 4,
            "modernness_memorability": 4,
            "token_adherence": 4,
            "notes": "Captured separately.",
        }
        return grades, _stats(0.0, duration_ms=650, duration_api_ms=650, token_usage={"input_tokens": 44, "output_tokens": 10})

    monkeypatch.setattr("src.orchestration.harness.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.harness.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.harness.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.harness.run_visual_capture", fake_visual_capture)
    monkeypatch.setattr("src.orchestration.harness.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.harness.start_app_stack", fake_start_app_stack)

    await run_harness("build something", tmp_path, HarnessConfig(max_rounds=1))

    state = FileComm(tmp_path / ".harness").read_state()
    assert state is not None
    assert state["last_completed_phase"] == "evaluate_r1"
    assert state["phase_metrics"]["planner"]["cost_usd"] == 0.1
    assert state["phase_metrics"]["planner"]["duration_ms"] == 1200
    assert state["phase_metrics"]["planner"]["token_usage"] == {
        "input_tokens": 111,
        "output_tokens": 22,
    }
    assert state["phase_metrics"]["planner"]["wall_duration_ms"] is not None
    assert state["phase_metrics"]["generator_r1"]["cost_usd"] == 0.2
    assert state["phase_metrics"]["generator_r1"]["duration_api_ms"] == 1800
    assert state["phase_metrics"]["generator_r1"]["token_usage"] == {
        "input_tokens": 222,
        "output_tokens": 33,
    }
    assert state["phase_metrics"]["generator_r1"]["wall_duration_ms"] is not None
    assert state["phase_metrics"]["evaluator_r1"]["cost_usd"] == 0.3
    assert state["phase_metrics"]["evaluator_r1"]["duration_ms"] == 3400
    assert state["phase_metrics"]["visual_capture_r1"]["cost_usd"] == 0.05
    assert state["phase_metrics"]["visual_capture_r1"]["duration_api_ms"] == 700
    assert state["phase_metrics"]["visual_capture_r1"]["token_usage"] == {
        "input_tokens": 55,
        "output_tokens": 11,
    }
    assert state["phase_metrics"]["visual_score_r1"]["duration_ms"] == 650
    assert state["phase_metrics"]["visual_score_r1"]["token_usage"] == {
        "input_tokens": 44,
        "output_tokens": 10,
    }
    assert state["phase_metrics"]["evaluator_r1"]["token_usage"] == {
        "input_tokens": 333,
        "output_tokens": 44,
    }
    assert state["phase_metrics"]["evaluator_r1"]["wall_duration_ms"] is not None


@pytest.mark.anyio
async def test_passed_sprint_advances_to_next_sprint_in_generate_mode(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, int, int | str]] = []
    stack = DummyAppStack()

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_sprint_plan(
            {
                "total_sprints": 2,
                "sprints": [
                    {
                        "number": 1,
                        "title": "Core flow",
                        "goal": "Ship the first sprint.",
                        "feature_ids": ["F001"],
                        "deliverables": ["Primary UI."],
                        "exit_criteria": ["Primary flow works."],
                    },
                    {
                        "number": 2,
                        "title": "Polish",
                        "goal": "Ship the second sprint.",
                        "feature_ids": ["F002"],
                        "deliverables": ["Polished UI."],
                        "exit_criteria": ["Polish is visible."],
                    },
                ],
            }
        )
        file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        _write_feature_list(file_comm, total=2)
        return 0.1

    async def fake_generator(*args, **kwargs):
        calls.append(("generator", kwargs["round_num"], kwargs["sprint_num"]))
        calls.append(("mode", kwargs["round_num"], kwargs["mode"]))
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        round_num = kwargs["round_num"]
        if round_num == 1:
            return (
                True,
                {
                    "round": 1,
                    "sprint": 1,
                    "mode_recommendation": "generate_next_sprint",
                    "overall_passed": True,
                    "criteria": {
                        "design_quality": {"score": 7.0},
                        "functionality": {"score": 7.0},
                        "originality": {"score": 6.0},
                        "craft": {"score": 7.0},
                    },
                },
                0.3,
            )
        return (
            True,
            {
                "round": 2,
                "sprint": 2,
                "mode_recommendation": "complete",
                "overall_passed": True,
                "criteria": {
                    "design_quality": {"score": 7.0},
                    "functionality": {"score": 7.0},
                    "originality": {"score": 6.0},
                    "craft": {"score": 7.0},
                },
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_capture(config, file_comm, workdir, round_num, app_url):
        return _visual_manifest(round_num), _stats(0.05)

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.harness.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.harness.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.harness.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.harness.run_visual_capture", fake_visual_capture)
    monkeypatch.setattr("src.orchestration.harness.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.harness.start_app_stack", fake_start_app_stack)

    await run_harness("build something", tmp_path, HarnessConfig(max_rounds=3))

    assert ("generator", 1, 1) in calls
    assert ("mode", 1, "generate") in calls
    assert ("generator", 2, 2) in calls
    assert ("mode", 2, "generate") in calls
    file_comm = FileComm(tmp_path / ".harness")
    assert file_comm.read_accepted_sprints() == {
        "accepted": [1, 2],
        "current_target": 3,
        "last_evaluated_round": 2,
    }
    assert file_comm.read_feature_list() == {
        "features": [
            {
                "id": "F001",
                "name": "Feature 1",
                "priority": "high",
                "depends_on": [],
                "description": "Feature 1 description.",
                "acceptance_criteria": ["Feature 1 works."],
                "status": "accepted",
                "sprint": 1,
            },
            {
                "id": "F002",
                "name": "Feature 2",
                "priority": "high",
                "depends_on": [],
                "description": "Feature 2 description.",
                "acceptance_criteria": ["Feature 2 works."],
                "status": "accepted",
                "sprint": 2,
            },
        ]
    }
    assert stack.closed is True
    state = file_comm.read_state()
    assert state is not None
    assert state["last_completed_phase"] == "evaluate_r2"
    assert state["last_verdict"] == "completed"


@pytest.mark.anyio
async def test_failed_sprint_keeps_same_target_and_next_round_repairs(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, int, int | str]] = []
    stack = DummyAppStack()

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_sprint_plan(
            {
                "total_sprints": 1,
                "sprints": [
                    {
                        "number": 1,
                        "title": "Core flow",
                        "goal": "Ship the first sprint.",
                        "feature_ids": ["F001"],
                        "deliverables": ["Primary UI."],
                        "exit_criteria": ["Primary flow works."],
                    }
                ],
            }
        )
        file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        _write_feature_list(file_comm)
        return 0.1

    async def fake_generator(*args, **kwargs):
        calls.append(("generator", kwargs["round_num"], kwargs["sprint_num"]))
        calls.append(("mode", kwargs["round_num"], kwargs["mode"]))
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        round_num = kwargs["round_num"]
        if round_num == 1:
            return (
                False,
                {
                    "round": 1,
                    "sprint": 1,
                    "mode_recommendation": "repair",
                    "overall_passed": False,
                    "criteria": {
                        "design_quality": {"score": 7.0},
                        "functionality": {"score": 5.0},
                        "originality": {"score": 6.0},
                        "craft": {"score": 7.0},
                    },
                },
                0.3,
            )
        return (
            True,
            {
                "round": 2,
                "sprint": 1,
                "mode_recommendation": "complete",
                "overall_passed": True,
                "criteria": {
                    "design_quality": {"score": 7.0},
                    "functionality": {"score": 7.0},
                    "originality": {"score": 6.0},
                    "craft": {"score": 7.0},
                },
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_capture(config, file_comm, workdir, round_num, app_url):
        return _visual_manifest(round_num), _stats(0.05)

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.harness.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.harness.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.harness.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.harness.run_visual_capture", fake_visual_capture)
    monkeypatch.setattr("src.orchestration.harness.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.harness.start_app_stack", fake_start_app_stack)

    await run_harness("build something", tmp_path, HarnessConfig(max_rounds=3))

    assert ("generator", 1, 1) in calls
    assert ("mode", 1, "generate") in calls
    assert ("generator", 2, 1) in calls
    assert ("mode", 2, "repair") in calls
    file_comm = FileComm(tmp_path / ".harness")
    assert file_comm.read_accepted_sprints() == {
        "accepted": [1],
        "current_target": 2,
        "last_evaluated_round": 2,
    }
    assert file_comm.read_feature_list() == {
        "features": [
            {
                "id": "F001",
                "name": "Feature 1",
                "priority": "high",
                "depends_on": [],
                "description": "Feature 1 description.",
                "acceptance_criteria": ["Feature 1 works."],
                "status": "accepted",
                "sprint": 1,
            }
        ]
    }
    assert stack.closed is True
    state = file_comm.read_state()
    assert state is not None
    assert state["last_completed_phase"] == "evaluate_r2"
    assert state["last_verdict"] == "completed"


@pytest.mark.anyio
async def test_failed_ui_checks_mark_only_affected_features_for_repair(monkeypatch, tmp_path: Path):
    stack = DummyAppStack()

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_sprint_plan(
            {
                "total_sprints": 1,
                "sprints": [
                    {
                        "number": 1,
                        "title": "Dual feature sprint",
                        "goal": "Ship two related features.",
                        "feature_ids": ["F001", "F002"],
                        "deliverables": ["Two visible features."],
                        "exit_criteria": ["Both features work."],
                    }
                ],
            }
        )
        file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        _write_features(file_comm, [("F001", 1), ("F002", 1)])
        return 0.1

    async def fake_generator(*args, **kwargs):
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        return (
            False,
            {
                "round": 1,
                "sprint": 1,
                "mode_recommendation": "repair",
                "overall_passed": False,
                "criteria": {
                    "design_quality": {"score": 7.0},
                    "functionality": {"score": 5.0},
                    "originality": {"score": 6.0},
                    "craft": {"score": 7.0},
                },
                "ui_checks": [
                    {
                        "feature_id": "F001",
                        "task": "Use feature 1.",
                        "expected_result": "Feature 1 works.",
                        "status": "pass",
                        "notes": "Feature 1 worked.",
                    },
                    {
                        "feature_id": "F002",
                        "task": "Use feature 2.",
                        "expected_result": "Feature 2 works.",
                        "status": "fail",
                        "notes": "Feature 2 broke.",
                    },
                ],
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_capture(config, file_comm, workdir, round_num, app_url):
        return _visual_manifest(round_num), _stats(0.05)

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.harness.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.harness.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.harness.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.harness.run_visual_capture", fake_visual_capture)
    monkeypatch.setattr("src.orchestration.harness.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.harness.start_app_stack", fake_start_app_stack)

    await run_harness("build something", tmp_path, HarnessConfig(max_rounds=1))

    file_comm = FileComm(tmp_path / ".harness")
    assert file_comm.read_feature_list() == {
        "features": [
            {
                "id": "F001",
                "name": "Feature 1",
                "priority": "high",
                "depends_on": [],
                "description": "Feature 1 description.",
                "acceptance_criteria": ["Feature 1 works."],
                "status": "implemented",
                "sprint": 1,
            },
            {
                "id": "F002",
                "name": "Feature 2",
                "priority": "high",
                "depends_on": [],
                "description": "Feature 2 description.",
                "acceptance_criteria": ["Feature 2 works."],
                "status": "repair_required",
                "sprint": 1,
            },
        ]
    }
    assert file_comm.read_accepted_sprints() == {
        "accepted": [],
        "current_target": 1,
        "last_evaluated_round": 1,
    }
    assert stack.closed is True


@pytest.mark.anyio
async def test_failed_evaluation_resume_uses_repair_mode_from_checkpoint(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, int, int | str]] = []
    stack = DummyAppStack()
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_sprint_plan(
        {
            "total_sprints": 1,
            "sprints": [
                {
                    "number": 1,
                    "title": "Core flow",
                    "goal": "Ship the first sprint.",
                    "feature_ids": ["F001"],
                    "deliverables": ["Primary UI."],
                    "exit_criteria": ["Primary flow works."],
                }
            ],
        }
    )
    _write_feature_list(file_comm)
    file_comm.write_state({
        "last_completed_phase": "evaluate_r1",
        "round_num": 1,
        "prompt": "saved prompt",
        "costs": {"planner": 1.25, "generator_r1": 2.0, "evaluator_r1": 0.5},
        "current_sprint": 1,
        "generator_mode": "repair",
        "accepted_sprints": [],
        "last_verdict": "failed_review",
    })

    async def fake_planner(*args, **kwargs):
        calls.append(("planner", 0, "unexpected"))
        return 0.1

    async def fake_generator(*args, **kwargs):
        calls.append(("generator", kwargs["round_num"], kwargs["sprint_num"]))
        calls.append(("mode", kwargs["round_num"], kwargs["mode"]))
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        calls.append(("evaluator", kwargs["round_num"], 1))
        return (
            True,
            {
                "round": 2,
                "sprint": 1,
                "mode_recommendation": "complete",
                "overall_passed": True,
                "criteria": {
                    "design_quality": {"score": 7.0},
                    "functionality": {"score": 7.0},
                    "originality": {"score": 6.0},
                    "craft": {"score": 7.0},
                },
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_capture(config, file_comm, workdir, round_num, app_url):
        return _visual_manifest(round_num), _stats(0.05)

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.harness.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.harness.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.harness.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.harness.run_visual_capture", fake_visual_capture)
    monkeypatch.setattr("src.orchestration.harness.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.harness.start_app_stack", fake_start_app_stack)

    await run_harness("ignored prompt", tmp_path, HarnessConfig(max_rounds=3), resume=True)

    assert ("planner", 0, "unexpected") not in calls
    assert ("generator", 2, 1) in calls
    assert ("mode", 2, "repair") in calls
    assert ("evaluator", 2, 1) in calls
    assert stack.closed is True


@pytest.mark.anyio
async def test_completed_resume_exits_without_new_round(monkeypatch, tmp_path: Path):
    calls: list[str] = []
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_sprint_plan(
        {"total_sprints": 1, "sprints": [{"number": 1, "feature_ids": ["F001"]}]}
    )
    _write_feature_list(file_comm)
    file_comm.write_accepted_sprints({"accepted": [1], "current_target": 2, "last_evaluated_round": 1})
    file_comm.write_state({
        "last_completed_phase": "evaluate_r1",
        "round_num": 1,
        "prompt": "saved prompt",
        "costs": {"planner": 1.25, "generator_r1": 2.0, "evaluator_r1": 0.5},
        "current_sprint": 1,
        "generator_mode": "generate",
        "accepted_sprints": [1],
        "last_verdict": "completed",
    })

    async def fake_planner(*args, **kwargs):
        calls.append("planner")
        return 0.1

    async def fake_generator(*args, **kwargs):
        calls.append("generator")
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        calls.append("evaluator")
        return True, {}, 0.3

    async def fake_start_app_stack(*args, **kwargs):
        raise AssertionError("runtime should not start")

    monkeypatch.setattr("src.orchestration.harness.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.harness.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.harness.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.harness.start_app_stack", fake_start_app_stack)

    await run_harness("ignored prompt", tmp_path, HarnessConfig(max_rounds=3), resume=True)

    assert calls == []
    state = file_comm.read_state()
    assert state is not None
    assert state["last_verdict"] == "completed"


@pytest.mark.anyio
async def test_fresh_run_clears_stale_harness_artifacts(monkeypatch, tmp_path: Path):
    harness_dir = tmp_path / ".harness"
    harness_dir.mkdir(parents=True, exist_ok=True)
    (harness_dir / "spec.md").write_text("stale spec")
    traces_dir = harness_dir / "traces"
    traces_dir.mkdir()
    (traces_dir / "planner.jsonl").write_text("stale trace")

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_spec("# Fresh Spec\n\n## Overview\nx\n\n## Technical Stack\ny\n\n## Design Direction\nz\n\n## Features\n## AI Integration\n## Technical Architecture")
        return 0.1

    monkeypatch.setattr("src.orchestration.harness.run_planner", fake_planner)

    await run_harness("build something", tmp_path, HarnessConfig(max_rounds=0), plan_only=True)

    assert (harness_dir / "spec.md").read_text().startswith("# Fresh Spec")
    assert not (traces_dir / "planner.jsonl").exists()
