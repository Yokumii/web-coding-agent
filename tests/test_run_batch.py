from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from dataclasses import replace

import pytest

from scripts.run_batch import (
    BatchTask,
    EditStep,
    _recover_failed_atomic_plan,
    load_latest_results,
    parse_input_file,
    run_batch,
)


def test_failed_atomic_plan_recovery_requires_matching_paid_trace(tmp_path: Path):
    workdir = tmp_path / "step"
    harness = workdir / ".harness"
    traces = harness / "traces"
    traces.mkdir(parents=True)
    plan = {"schema_version": "atomic-edit-plan-v1", "goal": "Add filter"}
    (harness / "atomic_edit_plan.json").write_text(json.dumps(plan))
    prompt = "Add one filter"
    (traces / "planner.jsonl").write_text(json.dumps({
        "event": "run_start",
        "phase": "atomic_edit_planner",
        "prompt": (
            "Create the atomic Edit plan for this instruction:\n\n"
            + prompt
            + "\n\nRequested target routes: [\"/\"]"
        ),
    }) + "\n")

    assert _recover_failed_atomic_plan(workdir, instruction=prompt) == plan
    assert _recover_failed_atomic_plan(workdir, instruction="Different Edit") is None


def test_failed_atomic_plan_recovery_prefers_raw_traced_response(tmp_path: Path):
    workdir = tmp_path / "step"
    harness = workdir / ".harness"
    traces = harness / "traces"
    traces.mkdir(parents=True)
    normalized = {"schema_version": "atomic-edit-plan-v1", "checks": []}
    raw = {
        "schema_version": "atomic-edit-plan-v1",
        "checks": [{"id": "restore", "route": "/#type=Document", "actions": []}],
    }
    (harness / "atomic_edit_plan.json").write_text(json.dumps(normalized))
    prompt = "Restore a type filter from the URL hash"
    (traces / "planner.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "run_start",
                        "phase": "atomic_edit_planner",
                        "prompt": (
                            "Create the atomic Edit plan for this instruction:\n\n"
                            + prompt
                            + "\n\nRequested target routes: [\"/\"]"
                        ),
                    }
                ),
                json.dumps(
                    {"event": "assistant_response", "content": json.dumps(raw)}
                ),
            ]
        )
        + "\n"
    )

    assert _recover_failed_atomic_plan(workdir, instruction=prompt) == raw


def test_failed_atomic_plan_recovery_uses_trace_when_normalized_file_was_not_written(
    tmp_path: Path,
):
    workdir = tmp_path / "step"
    traces = workdir / ".harness" / "traces"
    traces.mkdir(parents=True)
    prompt = "Add a direct detail link"
    raw = {"schema_version": "atomic-edit-plan-v1", "checks": []}
    (traces / "planner.jsonl").write_text(
        "\n".join([
            json.dumps({
                "event": "run_start",
                "phase": "atomic_edit_planner",
                "prompt": (
                    "Create the atomic Edit plan for this instruction:\n\n"
                    + prompt
                    + "\n\nRequested target routes: [\"/\"]"
                ),
            }),
            json.dumps({"event": "assistant_response", "content": json.dumps(raw)}),
        ]) + "\n"
    )

    assert _recover_failed_atomic_plan(workdir, instruction=prompt) == raw


def test_failed_atomic_plan_recovery_skips_latest_unrecoverable_drag(tmp_path: Path):
    workdir = tmp_path / "step"
    traces = workdir / ".harness" / "traces"
    traces.mkdir(parents=True)
    prompt = "Record every successful artifact reorder action"
    complete = {
        "schema_version": "atomic-edit-plan-v1",
        "checks": [{"actions": [{
            "action": "drag_and_drop",
            "selector": ".artifact-card:nth-child(1)",
            "target_selector": ".artifact-card:nth-child(3)",
        }]}],
    }
    incomplete = {
        "schema_version": "atomic-edit-plan-v1",
        "checks": [{"actions": [{
            "action": "drag_and_drop",
            "source_selector": ".artifact-card",
        }]}],
    }
    run_start = {
        "event": "run_start",
        "phase": "atomic_edit_planner",
        "prompt": (
            "Create the atomic Edit plan for this instruction:\n\n"
            + prompt
            + "\n\nRequested target routes: [\"/\"]"
        ),
    }
    (traces / "planner.jsonl").write_text(
        "\n".join([
            json.dumps(run_start),
            json.dumps({"event": "assistant_response", "content": json.dumps(complete)}),
            json.dumps(run_start),
            json.dumps({"event": "assistant_response", "content": json.dumps(incomplete)}),
        ]) + "\n"
    )

    assert _recover_failed_atomic_plan(workdir, instruction=prompt) == complete


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


def test_parse_batch_accepts_external_edit_sequence(tmp_path: Path):
    (tmp_path / "seed").mkdir()
    (tmp_path / "evaluation.json").write_text("{}")
    (tmp_path / "sequence.json").write_text(json.dumps({
        "edits": [
            {"edit_id": "q1", "instruction": "First Edit"},
            {"edit_id": "q2", "instruction": "Second Edit", "target_routes": ["/"]},
        ]
    }))
    source = tmp_path / "tasks.jsonl"
    source.write_text(json.dumps({
        "id": "chain", "task_mode": "edit",
        "seed_frontend": "seed", "seed_evaluation": "evaluation.json",
        "edit_sequence": "sequence.json",
    }) + "\n")

    task = parse_input_file(source)[0]

    assert [step.id for step in task.edits] == ["q1", "q2"]
    assert [step.prompt for step in task.edits] == ["First Edit", "Second Edit"]


def test_current_chain_metadata_is_lossless_and_dependencies_are_validated(tmp_path: Path):
    sequence = {
        "schema_version": "webcoding-linear-edit-query-sequence-v5",
        "seed_id": "accepted-seed", "future_provenance": {"opaque": [1, 2]},
        "edits": [
            {"edit_id": "q1", "edit_index": 1, "instruction": "Create saved items",
             "source_version": "s0", "target_version": "s1", "depends_on": [],
             "requires": [{"source": "seed", "state": "catalog"}],
             "produces": [{"state": "saved items"}], "acceptance": ["Items can be saved"],
             "preserve": ["Catalog navigation"], "inputs": ["reference.png"]},
            {"edit_id": "q2", "edit_index": 2, "instruction": "Filter saved items",
             "source_version": "s1", "target_version": "s2", "depends_on": ["q1"],
             "requires": [{"source": "q1", "state": "saved items"}]},
        ],
    }
    nested = tmp_path / "sequences"
    nested.mkdir()
    sequence_path = nested / "seed.json"
    sequence_path.write_text(json.dumps(sequence))
    source = tmp_path / "tasks.jsonl"
    source.write_text(json.dumps({"id": "chain", "task_mode": "edit",
        "seed_frontend": "seed", "seed_evaluation": "evaluation.json",
        "edit_sequence": "sequences/seed.json"}))
    task = parse_input_file(source)[0]
    assert task.sequence_metadata == sequence
    assert task.edits[0].chain_metadata == sequence["edits"][0]
    assert task.edits[0].inputs == (nested / "reference.png",)
    from src.orchestration.edit_task_contract import chain_obligations
    public = chain_obligations({"chain_metadata": {
        **sequence["edits"][0], "donor_code": "PRIVATE DONOR CODE",
    }})
    assert public["acceptance"] == ["Items can be saved"]
    assert public["requires"] == [{"source": "seed", "state": "catalog"}]
    assert "PRIVATE DONOR CODE" not in json.dumps(public)
    sequence["edits"][1]["requires"][0]["state"] = "uncreated state"
    sequence_path.write_text(json.dumps(sequence))
    with pytest.raises(ValueError, match="required state"):
        parse_input_file(source)
    sequence["edits"][1]["depends_on"] = ["q3"]
    sequence_path.write_text(json.dumps(sequence))
    with pytest.raises(ValueError, match="preceding edits"):
        parse_input_file(source)


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


@pytest.mark.anyio
@pytest.mark.parametrize("resume_prefix", [False, True])
async def test_edit_chain_uses_each_accepted_target_as_next_source_and_exports_diff(
    monkeypatch, tmp_path: Path, resume_prefix,
):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "app.js").write_text("const seed = true;\n")
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text("{}")
    prior_counts: list[int] = []

    def fake_prepare(source_frontend, target_workdir, source_evaluation):
        del source_evaluation
        frontend = target_workdir / "frontend"
        shutil.copytree(source_frontend, frontend, ignore=shutil.ignore_patterns(".git"))
        subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
        subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
             "commit", "-m", "seed"], cwd=frontend, check=True, capture_output=True,
        )
        baseline = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=frontend, text=True,
            check=True, capture_output=True,
        ).stdout.strip()
        target_workdir.mkdir(parents=True, exist_ok=True)
        (target_workdir / "seed_manifest.json").write_text(json.dumps({
            "baseline_commit": baseline,
        }))

    async def fake_harness(prompt, workdir, config, **kwargs):
        del config
        prior_counts.append(len(kwargs.get("prior_accepted_checks") or []))
        frontend = workdir / "frontend"
        with (frontend / "app.js").open("a") as handle:
            handle.write(f"// {prompt}\n")
        subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
             "commit", "-m", "feat: edit"], cwd=frontend, check=True, capture_output=True,
        )
        harness = workdir / ".harness"
        harness.mkdir(parents=True, exist_ok=True)
        (harness / "harness_state.json").write_text(json.dumps({
            "last_verdict": "completed", "last_completed_phase": "evaluate_r1",
            "costs": {"generator_r1": 0.01},
        }))
        (harness / "grade_round_1.json").write_text('{"overall_passed":true}')
        (harness / "ui_verification_plan.json").write_text(json.dumps({
            "sprints": [{"sprint": 1, "checks": [{
                "id": "UI-1", "feature_id": "EDIT-001", "critical": True,
                "task": prompt, "expected_result": "Done", "category": "behavior",
                "requirement_id": "REQ-1", "impact_tags": ["edit"], "route": "/",
                "actions": [{"action": "assert_text", "selector": "body", "value": "Done"}],
            }]}],
        }))

    monkeypatch.setattr("scripts.run_batch.prepare_seed", fake_prepare)
    monkeypatch.setattr("scripts.run_batch.run_harness", fake_harness)
    task = BatchTask(
        id="chain", prompt="", task_mode="edit",
        seed_frontend=seed, seed_evaluation=evaluation,
        edits=(EditStep(id="q1", prompt="first"), EditStep(id="q2", prompt="second")),
    )

    if resume_prefix:
        prefix = await run_batch(
            [replace(task, edits=task.edits[:1])], output_dir=tmp_path / "runs",
            results_path=tmp_path / "results.jsonl", workers=1, base_port=6500,
            timeout_seconds=10,
        )
        assert prefix[0]["status"] == "ok"
        assert prior_counts == [0]

    result = await run_batch(
        [task], output_dir=tmp_path / "runs", results_path=tmp_path / "results.jsonl",
        workers=1, base_port=6500, timeout_seconds=10,
    )

    assert result[0]["status"] == "ok"
    assert prior_counts == [0, 1]
    assert all(Path(item["ground_truth"]["patch"]).is_file() for item in result[0]["steps"])
    q2 = Path(result[0]["steps"][1]["workdir"]) / "frontend"
    q2_source = subprocess.run(
        ["git", "show", "HEAD^:app.js"], cwd=q2, text=True,
        check=True, capture_output=True,
    ).stdout
    assert "// first" in q2_source


@pytest.mark.anyio
async def test_single_edit_exports_source_to_target_ground_truth(
    monkeypatch, tmp_path: Path
):
    source = tmp_path / "seed"
    source.mkdir()
    (source / "index.html").write_text("<main>Before</main>\n")
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"checks": []}\n')

    def fake_prepare(source_frontend, target_workdir, source_evaluation):
        del source_evaluation
        frontend = target_workdir / "frontend"
        shutil.copytree(source_frontend, frontend)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=frontend,
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "add", "--all"], cwd=frontend, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Harness", "-c", "user.email=harness@example.test",
             "commit", "-m", "seed"],
            cwd=frontend,
            check=True,
            capture_output=True,
        )
        baseline = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=frontend, text=True,
            check=True, capture_output=True,
        ).stdout.strip()
        target_workdir.mkdir(parents=True, exist_ok=True)
        (target_workdir / "seed_manifest.json").write_text(json.dumps({
            "baseline_commit": baseline,
        }))

    async def fake_run_harness(_prompt, workdir, _config, **_kwargs):
        frontend = workdir / "frontend"
        (frontend / "index.html").write_text("<main>After</main>\n")
        subprocess.run(["git", "add", "index.html"], cwd=frontend, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Harness", "-c", "user.email=harness@example.test",
             "commit", "-m", "edit"],
            cwd=frontend,
            check=True,
            capture_output=True,
        )
        harness = workdir / ".harness"
        harness.mkdir(parents=True, exist_ok=True)
        (harness / "harness_state.json").write_text(json.dumps({
            "last_verdict": "completed",
            "last_completed_phase": "evaluate_r1",
            "costs": {"generator_r1": 0.01},
        }))

    monkeypatch.setattr("scripts.run_batch.prepare_seed", fake_prepare)
    monkeypatch.setattr("scripts.run_batch.run_harness", fake_run_harness)
    task = BatchTask(
        id="single-edit",
        prompt="Change the label.",
        task_mode="edit",
        seed_frontend=source,
        seed_evaluation=evidence,
    )
    results = await run_batch(
        [task],
        output_dir=tmp_path / "out",
        results_path=tmp_path / "results.jsonl",
        workers=1,
        base_port=7600,
        timeout_seconds=30,
    )

    assert results[0]["status"] == "ok"
    assert results[0]["ground_truth"]["edit_id"] == "single-edit"
    assert results[0]["ground_truth"]["changed_files"] == ["index.html"]
    assert Path(results[0]["ground_truth"]["patch"]).read_text().strip()
