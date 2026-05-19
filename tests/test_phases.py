import asyncio
import inspect
from pathlib import Path

import pytest

from src.agents.sdk_runner import AgentRunStats
from src.config import HarnessConfig
from src.orchestration import phases
from src.orchestration.cost_tracker import CostTracker
from src.orchestration.file_comm import FileComm
from src.orchestration.git_journal import CommitResult
from src.orchestration.phases import (
    HarnessContext,
    Verdict,
    run_build_phase,
    run_evaluate_phase,
    run_planner_phase,
)
from src.orchestration.sprint_state import SprintState


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _stats(cost_usd: float) -> AgentRunStats:
    return AgentRunStats(
        cost_usd=cost_usd,
        duration_ms=1000,
        duration_api_ms=800,
        token_usage={"input_tokens": 100, "output_tokens": 20},
        usage={"input_tokens": 100, "output_tokens": 20},
        model_usage={},
    )


def _stub_sprint(number: int) -> dict:
    return {
        "number": number,
        "title": f"Sprint {number}",
        "goal": f"Ship sprint {number}.",
        "feature_ids": [f"F00{number}"],
        "deliverables": [f"S{number} UI."],
        "exit_criteria": [f"S{number} works."],
    }


def _write_feature_list(file_comm: FileComm, total: int = 1) -> None:
    file_comm.write_feature_list(
        {
            "features": [
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
                for index in range(1, total + 1)
            ]
        }
    )


def _make_ctx(tmp_path: Path) -> HarnessContext:
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_sprint_plan({"total_sprints": 1, "sprints": [_stub_sprint(1)]})
    file_comm.write_accepted_sprints(
        {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
    )
    _write_feature_list(file_comm, total=1)
    return HarnessContext(
        workdir=tmp_path,
        config=HarnessConfig(max_rounds=1),
        file_comm=file_comm,
        cost_tracker=CostTracker(10),
        sprint_state=SprintState.load(file_comm),
        user_prompt="demo",
    )


def test_module_imports():
    assert phases.__name__ == "src.orchestration.phases"


def test_verdict_values():
    assert Verdict.completed == "completed"
    assert Verdict.accepted_review == "accepted_review"
    assert Verdict.failed_review == "failed_review"


def test_planner_phase_is_async():
    assert inspect.iscoroutinefunction(run_planner_phase)


def test_build_phase_is_async_and_takes_round_num():
    assert inspect.iscoroutinefunction(run_build_phase)
    sig = inspect.signature(run_build_phase)
    assert "round_num" in sig.parameters
    assert "resume_state" in sig.parameters


def test_evaluate_phase_is_async_and_takes_round_num():
    assert inspect.iscoroutinefunction(run_evaluate_phase)
    sig = inspect.signature(run_evaluate_phase)
    assert "round_num" in sig.parameters


def test_harness_context_dataclass_fields():
    fields = {f.name for f in HarnessContext.__dataclass_fields__.values()}
    assert {
        "workdir", "config", "file_comm", "cost_tracker", "sprint_state",
        "phase_metrics", "user_prompt",
    } <= fields


@pytest.mark.anyio
async def test_run_planner_phase_survives_delayed_cancellation(monkeypatch, tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    parent_task: asyncio.Task | None = None

    async def fake_run_planner(config, user_prompt, file_comm, workdir):
        nonlocal parent_task
        del config, user_prompt, workdir
        parent_task = asyncio.current_task()
        file_comm.write_sprint_plan({"total_sprints": 1, "sprints": [_stub_sprint(1)]})
        file_comm.write_accepted_sprints(
            {"accepted": [], "current_target": 1, "last_evaluated_round": 0}
        )
        asyncio.get_running_loop().call_soon(parent_task.cancel)
        return _stats(0.1)

    monkeypatch.setattr("src.orchestration.phases.run_planner", fake_run_planner)

    await run_planner_phase(ctx)

    state = ctx.file_comm.read_state()
    assert state is not None
    assert state["last_completed_phase"] == "plan"
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_run_build_phase_survives_delayed_cancellation(monkeypatch, tmp_path: Path):
    ctx = _make_ctx(tmp_path)
    parent_task: asyncio.Task | None = None
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()

    async def fake_run_generator(*args, **kwargs):
        nonlocal parent_task
        del args, kwargs
        parent_task = asyncio.current_task()
        return _stats(0.2)

    async def fake_commit_round(*args, **kwargs):
        del args, kwargs
        assert parent_task is not None
        asyncio.get_running_loop().call_soon(parent_task.cancel)
        return CommitResult(
            success=True,
            commit_hash="abc1234",
            message="test commit",
            was_empty=False,
        )

    monkeypatch.setattr("src.orchestration.phases.run_generator", fake_run_generator)
    monkeypatch.setattr("src.orchestration.phases.commit_round", fake_commit_round)

    await run_build_phase(ctx, 1)

    state = ctx.file_comm.read_state()
    assert state is not None
    assert state["last_completed_phase"] == "build_r1"
    await asyncio.sleep(0)
