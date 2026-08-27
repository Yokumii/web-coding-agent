from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_batch import BatchTask, load_latest_results, parse_input_file, run_batch


def test_parse_batch_jsonl_supports_edit_routes_and_multimodal_inputs(tmp_path: Path):
    image = tmp_path / "reference.png"
    image.write_bytes(b"png")
    source = tmp_path / "tasks.jsonl"
    source.write_text(
        json.dumps({
            "id": "catalog-filter",
            "prompt": "Add a catalog filter",
            "task_mode": "edit",
            "inputs": ["reference.png"],
            "target_routes": ["/catalog"],
        }) + "\n",
        encoding="utf-8",
    )

    tasks = parse_input_file(source)

    assert tasks == [BatchTask(
        id="catalog-filter",
        prompt="Add a catalog filter",
        workdir="",
        task_mode="edit",
        inputs=(image.resolve(),),
        target_routes=("/catalog",),
        seed_frontend=None,
        seed_evaluation=None,
    )]


def test_parse_batch_jsonl_resolves_verified_seed_pair(tmp_path: Path):
    seed = tmp_path / "seed"
    seed.mkdir()
    evidence = tmp_path / "evaluation.json"
    evidence.write_text("{}")
    source = tmp_path / "tasks.jsonl"
    source.write_text(json.dumps({
        "id": "edit", "prompt": "Edit it", "task_mode": "edit",
        "seed_frontend": "seed", "seed_evaluation": "evaluation.json",
    }) + "\n")

    task = parse_input_file(source)[0]

    assert task.seed_frontend == seed.resolve()
    assert task.seed_evaluation == evidence.resolve()


@pytest.mark.anyio
async def test_batch_appends_each_result_and_resume_skips_success(monkeypatch, tmp_path: Path):
    calls: list[str] = []

    async def fake_harness(prompt, workdir, config, **kwargs):
        calls.append(prompt)
        harness = workdir / ".harness"
        harness.mkdir(parents=True, exist_ok=True)
        (harness / "harness_state.json").write_text(json.dumps({
            "last_verdict": "completed", "last_completed_phase": "evaluate_r1",
            "costs": {"planner": 0.1},
        }))

    monkeypatch.setattr("scripts.run_batch.run_harness", fake_harness)
    tasks = [
        BatchTask(id="a", prompt="first"),
        BatchTask(id="b", prompt="second"),
    ]
    results_path = tmp_path / "results.jsonl"

    first = await run_batch(
        tasks, output_dir=tmp_path / "runs", results_path=results_path,
        workers=2, base_port=6200, timeout_seconds=5,
    )
    second = await run_batch(
        tasks, output_dir=tmp_path / "runs", results_path=results_path,
        workers=2, base_port=6200, timeout_seconds=5,
    )

    assert sorted(item["status"] for item in first) == ["ok", "ok"]
    assert second == []
    assert sorted(calls) == ["first", "second"]
    assert load_latest_results(results_path).keys() == {"a", "b"}
    assert len(results_path.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.anyio
async def test_batch_records_timeout_without_losing_other_cases(monkeypatch, tmp_path: Path):
    async def fake_harness(prompt, workdir, config, **kwargs):
        if prompt == "slow":
            import asyncio
            await asyncio.sleep(1)
        harness = workdir / ".harness"
        harness.mkdir(parents=True, exist_ok=True)
        (harness / "harness_state.json").write_text(json.dumps({
            "last_verdict": "completed", "last_completed_phase": "evaluate_r1",
            "costs": {},
        }))

    monkeypatch.setattr("scripts.run_batch.run_harness", fake_harness)
    tasks = [BatchTask(id="slow", prompt="slow"), BatchTask(id="fast", prompt="fast")]

    results = await run_batch(
        tasks, output_dir=tmp_path / "runs", results_path=tmp_path / "results.jsonl",
        workers=2, base_port=6300, timeout_seconds=0.01,
    )

    assert {item["id"]: item["status"] for item in results} == {
        "slow": "timeout", "fast": "ok",
    }


@pytest.mark.anyio
async def test_batch_does_not_label_unfinished_harness_as_ok(monkeypatch, tmp_path: Path):
    async def fake_harness(prompt, workdir, config, **kwargs):
        harness = workdir / ".harness"
        harness.mkdir(parents=True)
        (harness / "harness_state.json").write_text(json.dumps({
            "last_verdict": "failed_review", "last_completed_phase": "evaluate_r1",
            "costs": {"generator_r1": 0.2},
        }))

    monkeypatch.setattr("scripts.run_batch.run_harness", fake_harness)

    result = await run_batch(
        [BatchTask(id="partial", prompt="partial")],
        output_dir=tmp_path / "runs", results_path=tmp_path / "results.jsonl",
        workers=1, base_port=6400, timeout_seconds=5,
    )

    assert result[0]["status"] == "incomplete"
    assert result[0]["last_verdict"] == "failed_review"
    assert "--resume-harness" in result[0]["error"]
