from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.agents.sdk_runner import AgentRunStats
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.harness import ResumeError, run_harness


class DummyAppStack:
    def __init__(self, frontend_url: str = "http://127.0.0.1:5173") -> None:
        self.frontend_url = frontend_url
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _stub_sprint(number: int, feature_ids: list[str] | None = None) -> dict:
    """Build a sprint dict that satisfies the SprintPlan schema."""
    return {
        "number": number,
        "title": f"Sprint {number}",
        "goal": f"Ship sprint {number}.",
        "feature_ids": feature_ids if feature_ids is not None else [f"F00{number}"],
        "deliverables": [f"S{number} UI."],
        "exit_criteria": [f"S{number} works."],
    }


def _passing_criteria() -> dict:
    return {
        "design_quality": {"score": 7.0, "passed": True},
        "functionality": {"score": 7.0, "passed": True},
        "originality": {"score": 6.0, "passed": True},
        "craft": {"score": 7.0, "passed": True},
    }


def _failing_functionality_criteria() -> dict:
    return {
        "design_quality": {"score": 7.0, "passed": True},
        "functionality": {"score": 5.0, "passed": False},
        "originality": {"score": 6.0, "passed": True},
        "craft": {"score": 7.0, "passed": True},
    }


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
        {"total_sprints": 1, "sprints": [_stub_sprint(1)]}
    )
    _write_feature_list(file_comm)
    file_comm.write_accepted_sprints({"accepted": [], "current_target": 1, "last_evaluated_round": 1})
    file_comm.write_state({
        "last_completed_phase": "build_r2",
        "round_num": 2,
        "prompt": "saved prompt",
        "costs": {"planner": 1.25},
        "accepted_sprints_payload": {
            "accepted": [],
            "current_target": 1,
            "last_evaluated_round": 1,
        },
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

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

    await run_harness("ignored prompt", tmp_path, HarnessConfig(max_rounds=3), resume=True)

    assert ("planner", None) not in calls
    assert ("generator", 2) not in calls
    assert ("start_app_stack", 2) in calls
    assert ("evaluator", 2) in calls
    assert stack.closed is True


@pytest.mark.anyio
async def test_resume_from_evaluate_checkpoint_starts_next_build_round(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, int | None]] = []
    stack = DummyAppStack()
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_sprint_plan(
        {"total_sprints": 2, "sprints": [_stub_sprint(1), _stub_sprint(2)]}
    )
    _write_feature_list(file_comm, total=2)
    file_comm.write_accepted_sprints({"accepted": [1], "current_target": 2, "last_evaluated_round": 1})
    file_comm.write_state({
        "last_completed_phase": "evaluate_r1",
        "round_num": 1,
        "prompt": "saved prompt",
        "costs": {"planner": 1.25, "generator_r1": 2.0, "evaluator_r1": 0.5},
        "accepted_sprints_payload": {
            "accepted": [1],
            "current_target": 2,
            "last_evaluated_round": 1,
        },
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

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

    await run_harness("ignored prompt", tmp_path, HarnessConfig(max_rounds=3), resume=True)

    assert ("planner", None) not in calls
    assert ("generator", 2) in calls
    assert ("start_app_stack", 2) in calls
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
                    "design_quality": {"score": 7.0, "passed": True},
                    "functionality": {"score": 7.0, "passed": True},
                    "originality": {"score": 6.0, "passed": True},
                    "craft": {"score": 7.0, "passed": True},
                },
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

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

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

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
        "visual_score_r1": 0.0,
    }


@pytest.mark.anyio
async def test_successful_evaluation_survives_cancellation_during_stack_close(
    monkeypatch, tmp_path: Path
):
    """app_stack.close() cancels the parent mid-cleanup; safe_sdk_session
    absorbs the leaked cancellation and the round still completes."""
    parent_task: asyncio.Task | None = None

    class CancellingAppStack(DummyAppStack):
        def __init__(self) -> None:
            super().__init__()
            self.close_finished = False

        async def close(self) -> None:
            self.closed = True
            assert parent_task is not None
            parent_task.cancel()
            await asyncio.sleep(0)
            self.close_finished = True

    stack = CancellingAppStack()

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_sprint_plan({"total_sprints": 1, "sprints": [_stub_sprint(1)]})
        file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        _write_feature_list(file_comm)
        return 0.1

    async def fake_generator(*args, **kwargs):
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        nonlocal parent_task
        parent_task = asyncio.current_task()
        return (
            True,
            {
                "round": 1,
                "sprint": 1,
                "mode_recommendation": "complete",
                "overall_passed": True,
                "criteria": _passing_criteria(),
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

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

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr(
        "src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review
    )
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

    await run_harness("build something", tmp_path, HarnessConfig(max_rounds=1))

    assert stack.closed is True
    # close() may or may not run to completion depending on where the
    # injected cancellation lands; the load-bearing assertion is that
    # the round survives and the checkpoint records success.
    state = FileComm(tmp_path / ".harness").read_state()
    assert state is not None
    assert state["last_completed_phase"] == "evaluate_r1"
    assert state["last_verdict"] == "completed"
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_successful_evaluation_survives_delayed_cancellation_after_stack_close(
    monkeypatch, tmp_path: Path
):
    """Cancellation scheduled via call_soon lands one tick later — the
    safe_sdk_session exit drain catches it."""
    parent_task: asyncio.Task | None = None

    class DelayedCancellingAppStack(DummyAppStack):
        async def close(self) -> None:
            self.closed = True
            assert parent_task is not None
            asyncio.get_running_loop().call_soon(parent_task.cancel)

    stack = DelayedCancellingAppStack()

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_sprint_plan({"total_sprints": 1, "sprints": [_stub_sprint(1)]})
        file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        _write_feature_list(file_comm)
        return 0.1

    async def fake_generator(*args, **kwargs):
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        nonlocal parent_task
        parent_task = asyncio.current_task()
        return (
            True,
            {
                "round": 1,
                "sprint": 1,
                "mode_recommendation": "complete",
                "overall_passed": True,
                "criteria": _passing_criteria(),
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_review(**kwargs):
        await asyncio.sleep(0)
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

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr(
        "src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review
    )
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

    await run_harness("build something", tmp_path, HarnessConfig(max_rounds=1))

    assert stack.closed is True
    state = FileComm(tmp_path / ".harness").read_state()
    assert state is not None
    assert state["last_completed_phase"] == "evaluate_r1"
    assert state["last_verdict"] == "completed"
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_successful_evaluation_survives_cancellation_during_visual_review(
    monkeypatch, tmp_path: Path
):
    """Cancellation surfaced mid-visual-review (inside the second
    safe_sdk_session block) is absorbed."""
    parent_task: asyncio.Task | None = None
    stack = DummyAppStack()

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_sprint_plan({"total_sprints": 1, "sprints": [_stub_sprint(1)]})
        file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        _write_feature_list(file_comm)
        return 0.1

    async def fake_generator(*args, **kwargs):
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        nonlocal parent_task
        parent_task = asyncio.current_task()
        return (
            True,
            {
                "round": 1,
                "sprint": 1,
                "mode_recommendation": "complete",
                "overall_passed": True,
                "criteria": _passing_criteria(),
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_review(**kwargs):
        assert parent_task is not None
        parent_task.cancel()
        await asyncio.sleep(0)
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

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr(
        "src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review
    )
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

    await run_harness("build something", tmp_path, HarnessConfig(max_rounds=1))

    assert stack.closed is True
    state = FileComm(tmp_path / ".harness").read_state()
    assert state is not None
    assert state["last_completed_phase"] == "evaluate_r1"
    assert state["last_verdict"] == "completed"
    await asyncio.sleep(0)


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
                    "design_quality": {"score": 7.0, "passed": True},
                    "functionality": {"score": 5.0, "passed": False},
                    "originality": {"score": 6.0, "passed": True},
                    "craft": {"score": 7.0, "passed": True},
                },
            },
            _stats(0.3, duration_ms=3400, duration_api_ms=2900, token_usage={"input_tokens": 333, "output_tokens": 44}),
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

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

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

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
                        "design_quality": {"score": 7.0, "passed": True},
                        "functionality": {"score": 7.0, "passed": True},
                        "originality": {"score": 6.0, "passed": True},
                        "craft": {"score": 7.0, "passed": True},
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
                    "design_quality": {"score": 7.0, "passed": True},
                    "functionality": {"score": 7.0, "passed": True},
                    "originality": {"score": 6.0, "passed": True},
                    "craft": {"score": 7.0, "passed": True},
                },
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

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
                        "design_quality": {"score": 7.0, "passed": True},
                        "functionality": {"score": 5.0, "passed": False},
                        "originality": {"score": 6.0, "passed": True},
                        "craft": {"score": 7.0, "passed": True},
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
                    "design_quality": {"score": 7.0, "passed": True},
                    "functionality": {"score": 7.0, "passed": True},
                    "originality": {"score": 6.0, "passed": True},
                    "craft": {"score": 7.0, "passed": True},
                },
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

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
                    "design_quality": {"score": 7.0, "passed": True},
                    "functionality": {"score": 5.0, "passed": False},
                    "originality": {"score": 6.0, "passed": True},
                    "craft": {"score": 7.0, "passed": True},
                },
                "ui_checks": [
                    {
                        "check_id": "UI-001",
                        "feature_id": "F001",
                        "critical": True,
                        "task": "Use feature 1.",
                        "expected_result": "Feature 1 works.",
                        "status": "pass",
                        "notes": "Feature 1 worked.",
                    },
                    {
                        "check_id": "UI-002",
                        "feature_id": "F002",
                        "critical": True,
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

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

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
        "accepted_sprints_payload": {
            "accepted": [],
            "current_target": 1,
            "last_evaluated_round": 1,
        },
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
                    "design_quality": {"score": 7.0, "passed": True},
                    "functionality": {"score": 7.0, "passed": True},
                    "originality": {"score": 6.0, "passed": True},
                    "craft": {"score": 7.0, "passed": True},
                },
            },
            0.3,
        )

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

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
        {"total_sprints": 1, "sprints": [_stub_sprint(1)]}
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
        "accepted_sprints_payload": {
            "accepted": [1],
            "current_target": 2,
            "last_evaluated_round": 1,
        },
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

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

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

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)

    await run_harness("build something", tmp_path, HarnessConfig(max_rounds=0), plan_only=True)

    assert (harness_dir / "spec.md").read_text().startswith("# Fresh Spec")
    assert not (traces_dir / "planner.jsonl").exists()


# --- evaluator failure must still close the app stack ---


@pytest.mark.anyio
async def test_evaluator_failure_aborts_round_and_closes_stack(
    monkeypatch, tmp_path: Path
):
    """When run_evaluator raises, the round aborts before the manifest is
    read or the vision review runs, and the app stack must still be closed."""
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
        raise RuntimeError("evaluator boom")

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], None

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr(
        "src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review
    )
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

    with pytest.raises(RuntimeError, match="evaluator boom"):
        await run_harness("build something", tmp_path, HarnessConfig(max_rounds=1))

    assert stack.closed is True


# --- resume must trust state when accepted_sprints.json is stale ---


@pytest.mark.anyio
async def test_resume_reconciles_advanced_accepted_sprints_against_build_checkpoint(
    monkeypatch, tmp_path: Path
):
    """Resume must reconcile a stale accepted_sprints.json from the
    checkpoint state.

    With the old (file-first, state-second) write order, a crash between
    the two writes left ``accepted_sprints.json`` advanced to sprint N+1
    while ``harness_state.json`` still said ``build_rN`` was the last
    completed phase. On resume, the harness picked up the stale file and
    fed sprint N+1 to the evaluator — even though sprint N+1 was never
    built. The fix writes state first AND reconciles the file from
    state's view on resume.
    """
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_sprint_plan(
        {
            "total_sprints": 2,
            "sprints": [
                {
                    "number": 1,
                    "title": "Sprint 1",
                    "goal": "Ship sprint 1.",
                    "feature_ids": ["F001"],
                    "deliverables": ["S1 UI."],
                    "exit_criteria": ["S1 works."],
                },
                {
                    "number": 2,
                    "title": "Sprint 2",
                    "goal": "Ship sprint 2.",
                    "feature_ids": ["F002"],
                    "deliverables": ["S2 UI."],
                    "exit_criteria": ["S2 works."],
                },
            ],
        }
    )
    _write_feature_list(file_comm, total=2)

    # accepted_sprints.json is prematurely advanced: claims sprint 1 done.
    file_comm.write_accepted_sprints(
        {"accepted": [1], "current_target": 2, "last_evaluated_round": 1}
    )
    # State only got as far as build_r1 — evaluate never completed.
    file_comm.write_state(
        {
            "last_completed_phase": "build_r1",
            "round_num": 1,
            "prompt": "saved prompt",
            "costs": {"planner": 0.1, "generator_r1": 0.2},
            "phase_metrics": {},
            "current_sprint": 1,
            "generator_mode": "generate",
            "accepted_sprints": [],
            "accepted_sprints_payload": {
                "accepted": [],
                "current_target": 1,
                "last_evaluated_round": 0,
            },
            "last_verdict": "awaiting_review",
        }
    )

    captured: dict = {}

    async def fake_planner(*args, **kwargs):
        captured["planner_called"] = True
        return 0.1

    async def fake_generator(*args, **kwargs):
        captured["generator_called"] = True
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        # accepted_sprints.json must already have been reconciled to
        # state's view (current_target=1) by the time the evaluator
        # is asked to score.
        accepted = FileComm(tmp_path / ".harness").read_accepted_sprints() or {}
        captured["evaluator_target"] = accepted.get("current_target")
        return (
            True,
            {
                "round": kwargs["round_num"],
                "sprint": accepted.get("current_target"),
                "mode_recommendation": "generate_next_sprint",
                "overall_passed": True,
                "criteria": {
                    "design_quality": {"score": 7.0, "passed": True},
                    "functionality": {"score": 7.0, "passed": True},
                    "originality": {"score": 6.0, "passed": True},
                    "craft": {"score": 7.0, "passed": True},
                },
            },
            0.3,
        )

    async def fake_start_app_stack(*args, **kwargs):
        return DummyAppStack()

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr(
        "src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review
    )
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

    await run_harness(
        "ignored", tmp_path, HarnessConfig(max_rounds=1), resume=True
    )

    # Generator must NOT run again (build_r1 was checkpointed).
    assert "generator_called" not in captured
    # Planner must NOT run again (we resumed past plan).
    assert "planner_called" not in captured
    # Evaluator was asked to score sprint 1 (state SOT), not sprint 2.
    assert captured["evaluator_target"] == 1


@pytest.mark.anyio
async def test_evaluate_checkpoint_is_written_before_accepted_sprints_file(
    monkeypatch, tmp_path: Path
):
    """Forward-only contract: ``harness_state.json`` must be on disk before
    ``accepted_sprints.json`` is updated for the new round. If it isn't,
    a crash in the gap reverts to the H6 bug where the file races ahead
    of the checkpoint.
    """
    file_writes: list[str] = []

    original_write_state = FileComm.write_state
    original_write_accepted = FileComm.write_accepted_sprints

    def tracking_write_state(self, *args, **kwargs):
        file_writes.append("state")
        return original_write_state(self, *args, **kwargs)

    def tracking_write_accepted(self, *args, **kwargs):
        file_writes.append("accepted_sprints")
        return original_write_accepted(self, *args, **kwargs)

    monkeypatch.setattr(FileComm, "write_state", tracking_write_state)
    monkeypatch.setattr(FileComm, "write_accepted_sprints", tracking_write_accepted)

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_sprint_plan(
            {
                "total_sprints": 1,
                "sprints": [
                    {
                        "number": 1,
                        "title": "S",
                        "goal": "g",
                        "feature_ids": ["F001"],
                        "deliverables": ["D"],
                        "exit_criteria": ["C"],
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
                    "design_quality": {"score": 7.0, "passed": True},
                    "functionality": {"score": 7.0, "passed": True},
                    "originality": {"score": 6.0, "passed": True},
                    "craft": {"score": 7.0, "passed": True},
                },
            },
            0.3,
        )

    async def fake_start_app_stack(*args, **kwargs):
        return DummyAppStack()

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr(
        "src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review
    )
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

    await run_harness("build something", tmp_path, HarnessConfig(max_rounds=1))

    # Find the LAST occurrence of each post-evaluate write. The state
    # for evaluate_r1 must precede the accepted_sprints update for the
    # post-evaluate transition.
    last_state_index = max(i for i, w in enumerate(file_writes) if w == "state")
    last_accepted_index = max(
        i for i, w in enumerate(file_writes) if w == "accepted_sprints"
    )
    assert last_state_index < last_accepted_index, (
        "post-evaluate state must be checkpointed BEFORE accepted_sprints.json "
        f"is rewritten; saw writes: {file_writes}"
    )


@pytest.mark.anyio
async def test_legacy_state_resume_raises(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_sprint_plan(
        {"total_sprints": 1, "sprints": [_stub_sprint(1)]}
    )
    _write_feature_list(file_comm)

    legacy_state = {
        "last_completed_phase": "build_r1",
        "round_num": 1,
        "prompt": "saved prompt",
        "costs": {"planner": 0.1, "generator_r1": 0.2},
        "phase_metrics": {},
        "current_sprint": 1,
        "generator_mode": "generate",
        "accepted_sprints": [],
        "last_verdict": "awaiting_review",
    }
    (file_comm.dir / "harness_state.json").write_text(json.dumps(legacy_state))

    async def fake_planner(*args, **kwargs):
        raise AssertionError("planner should not run")

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)

    with pytest.raises(ResumeError, match="older version"):
        await run_harness("ignored prompt", tmp_path, HarnessConfig(max_rounds=1), resume=True)


# --- budget gate after evaluate three-phase block ---


@pytest.mark.anyio
async def test_budget_check_after_evaluate_stops_subsequent_rounds(
    monkeypatch, tmp_path: Path
):
    """Without an evaluate-phase budget check, the harness could blow
    far past max_budget_usd in a single round (evaluator + visual_score
    both add cost). The fix re-runs is_over_budget after the evaluate
    block settles, before the next round begins.
    """
    rounds_started: list[int] = []
    stack = DummyAppStack()

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_sprint_plan(
            {
                "total_sprints": 5,
                "sprints": [
                    {
                        "number": n,
                        "title": f"Sprint {n}",
                        "goal": f"g{n}",
                        "feature_ids": [f"F{n:03d}"],
                        "deliverables": ["D"],
                        "exit_criteria": ["C"],
                    }
                    for n in range(1, 6)
                ],
            }
        )
        file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        _write_features(file_comm, [(f"F{n:03d}", n) for n in range(1, 6)])
        return 0.1

    async def fake_generator(*args, **kwargs):
        rounds_started.append(kwargs["round_num"])
        # Build cost is small; the over-budget condition only crosses
        # the threshold once visual_score is added.
        return 0.05

    async def fake_evaluator(*args, **kwargs):
        return (
            True,
            {
                "round": kwargs["round_num"],
                "sprint": kwargs["round_num"],
                "mode_recommendation": "generate_next_sprint",
                "overall_passed": True,
                "criteria": {
                    "design_quality": {"score": 7.0, "passed": True},
                    "functionality": {"score": 7.0, "passed": True},
                    "originality": {"score": 6.0, "passed": True},
                    "craft": {"score": 7.0, "passed": True},
                },
            },
            5.0,  # large evaluator cost crosses budget
        )

    async def fake_start_app_stack(*args, **kwargs):
        return stack

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.1)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr(
        "src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review
    )
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

    # Budget=1.0, planner 0.1 + generator_r1 0.05 = 0.15 < 1.0,
    # then evaluator_r1 5.0 crosses to 5.15 — must stop.
    await run_harness(
        "build something", tmp_path, HarnessConfig(max_budget_usd=1.0, max_rounds=5)
    )

    assert rounds_started == [1], (
        f"Generator must only run for round 1; budget gate after evaluate_r1 "
        f"should stop subsequent rounds. Saw rounds: {rounds_started}"
    )


# --- fresh run clears workdir/frontend by default ---


@pytest.mark.anyio
async def test_fresh_run_clears_existing_frontend_dir(monkeypatch, tmp_path: Path):
    """A fresh prompt with no --resume should not have generator round
    1 inherit a frontend/ left over from the previous prompt — that
    causes generate-mode to "repair" code it never saw the spec for.
    """
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / "stale.txt").write_text("from previous prompt")

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_spec(
            "# Fresh\n\n## Overview\nx\n\n## Technical Stack\ny\n\n## Design Direction\nz\n\n## Features\n## AI Integration\n## Technical Architecture"
        )
        return 0.0

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)

    await run_harness(
        "fresh prompt", tmp_path, HarnessConfig(max_rounds=0), plan_only=True
    )

    assert not (tmp_path / "frontend" / "stale.txt").exists()


@pytest.mark.anyio
async def test_keep_frontend_preserves_existing_frontend_dir(monkeypatch, tmp_path: Path):
    """Power-user opt-out: `--keep-frontend` keeps an existing
    workdir/frontend/ across fresh runs."""
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / "kept.txt").write_text("preserved")

    async def fake_planner(config, user_prompt, file_comm, workdir):
        file_comm.write_spec(
            "# Fresh\n\n## Overview\nx\n\n## Technical Stack\ny\n\n## Design Direction\nz\n\n## Features\n## AI Integration\n## Technical Architecture"
        )
        return 0.0

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)

    await run_harness(
        "fresh prompt",
        tmp_path,
        HarnessConfig(max_rounds=0),
        plan_only=True,
        keep_frontend=True,
    )

    assert (tmp_path / "frontend" / "kept.txt").read_text() == "preserved"


@pytest.mark.anyio
async def test_resume_does_not_clear_frontend_dir(monkeypatch, tmp_path: Path):
    """--resume must never touch frontend/ regardless of keep_frontend."""
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / "in_progress.txt").write_text("don't delete")

    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_state(
        {
            "last_completed_phase": "plan",
            "round_num": 0,
            "prompt": "saved",
            "costs": {"planner": 0.1},
            "current_sprint": 1,
            "accepted_sprints": [],
            "accepted_sprints_payload": {
                "accepted": [],
                "current_target": 1,
                "last_evaluated_round": 0,
            },
            "last_verdict": "planned",
        }
    )

    async def fake_planner(*args, **kwargs):
        raise AssertionError("planner should not run on resume")

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)

    await run_harness(
        "ignored",
        tmp_path,
        HarnessConfig(max_rounds=0),
        resume=True,
    )

    assert (tmp_path / "frontend" / "in_progress.txt").read_text() == "don't delete"


@pytest.mark.anyio
async def test_harness_does_not_commit_on_behalf_of_generator(monkeypatch, tmp_path: Path):
    stack = DummyAppStack()
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_sprint_plan(
        {"total_sprints": 1, "sprints": [_stub_sprint(1)]}
    )
    _write_feature_list(file_comm)
    file_comm.write_accepted_sprints({"accepted": [], "current_target": 1, "last_evaluated_round": 0})

    async def fake_planner(*args, **kwargs):
        return 0.1

    async def fake_generator(*args, **kwargs):
        return 0.2

    async def fake_evaluator(*args, **kwargs):
        grades = {
            "round": kwargs.get("round_num", 1),
            "criteria": {
                "design_quality": {"score": 7, "passed": True},
                "functionality":  {"score": 7, "passed": True},
                "originality":    {"score": 6, "passed": True},
                "craft":          {"score": 7, "passed": True},
            },
            "overall_passed": True,
            "ui_checks": [],
            "target_exit_criteria_results": [],
        }
        return True, grades, 0.3

    async def fake_start_app_stack(workdir, harness_dir, config, round_num):
        return stack

    async def fake_visual_review(**kwargs):
        return kwargs["grades"], _stats(0.0)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_planner)
    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_generator)
    monkeypatch.setattr("src.orchestration.phases.run_evaluator", fake_evaluator)
    monkeypatch.setattr("src.orchestration.phases.apply_dedicated_visual_review", fake_visual_review)
    monkeypatch.setattr("src.orchestration.phases.start_app_stack", fake_start_app_stack)

    await run_harness("test prompt", tmp_path, HarnessConfig(max_rounds=1))

    build_log = file_comm.read_build_log() or ""
    assert "git commit" not in build_log


@pytest.mark.anyio
async def test_image_first_mode_writes_design_checkpoint_before_rounds(monkeypatch, tmp_path: Path):
    async def fake_planner_phase(ctx):
        ctx.file_comm.write_sprint_plan(
            {
                "total_sprints": 1,
                "sprints": [_stub_sprint(1)],
            }
        )
        ctx.file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        _write_feature_list(ctx.file_comm)
        ctx.file_comm.write_state({"last_completed_phase": "plan"})

    monkeypatch.setattr("src.orchestration.harness.run_planner_phase", fake_planner_phase)

    await run_harness(
        "build something",
        tmp_path,
        HarnessConfig(max_rounds=0, design_mode="image-first", design_image_api_key=""),
    )

    file_comm = FileComm(tmp_path / ".harness")
    state = file_comm.read_state()
    assert state is not None
    assert state["last_completed_phase"] == "design"
    assert state["requested_design_mode"] == "image-first"
    assert state["design_mode"] == "text_only_fallback"
    assert state["design_status"] == "fallback_text_only"
    assert file_comm.read_design_brief()["visual_strategy"] == "text_only_fallback"


@pytest.mark.anyio
async def test_resume_from_design_checkpoint_skips_planner_and_design(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_sprint_plan({"total_sprints": 1, "sprints": [_stub_sprint(1)]})
    file_comm.write_accepted_sprints(
        {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
    )
    _write_feature_list(file_comm)
    file_comm.write_state(
        {
            "last_completed_phase": "design",
            "round_num": 0,
            "prompt": "saved prompt",
            "costs": {"planner": 0.1},
            "accepted_sprints_payload": {
                "accepted": [],
                "current_target": 1,
                "last_evaluated_round": 0,
            },
            "requested_design_mode": "image-first",
            "design_mode": "text_only_fallback",
            "design_status": "fallback_text_only",
            "approved_concept_path": None,
            "background_ui_path": None,
        }
    )
    calls: list[str] = []

    async def fake_planner_phase(*args, **kwargs):
        calls.append("planner")
        raise AssertionError("planner phase should not rerun from design checkpoint")

    async def fake_design_phase(*args, **kwargs):
        calls.append("design")
        raise AssertionError("design phase should not rerun from design checkpoint")

    monkeypatch.setattr("src.orchestration.harness.run_planner_phase", fake_planner_phase)
    monkeypatch.setattr("src.orchestration.harness.run_design_phase", fake_design_phase)

    await run_harness(
        "ignored",
        tmp_path,
        HarnessConfig(max_rounds=0, design_mode="text-only"),
        resume=True,
    )

    assert calls == []
