from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.export_trajectory_dataset import (
    _accepted_tape_rounds,
    _accepted_tape_replay_passed,
    _is_real_project_failure,
    _minimal_path_provenance,
    _repair_task_descriptions,
    _resolved_round_commits,
    _strict_mutation_evidence_passed,
    append_jsonl_records,
    apply_patches,
    export_run,
    to_v2_records,
)


def test_hidden_source_edit_risk_failure_is_a_typed_natural_repair_candidate():
    grade = {
        "overall_passed": False,
        "hidden_oracle": {
            "status": "failed",
            "failed_check_ids": ["RISK-01-OVERFLOW"],
        },
        "repair_task_descriptions": [{
            "task_type": "Overflow",
            "description": (
                "Overflow reproduced after the normal Edit: "
                "viewport-horizontal-overflow at html."
            ),
            "evidence_ids": ["RISK-01-OVERFLOW"],
        }],
    }

    assert _is_real_project_failure(grade) is True
    assert _repair_task_descriptions(grade) == grade["repair_task_descriptions"]


def _git(frontend: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=frontend, check=True, capture_output=True)


def _commit(frontend: Path, subject: str, content: str) -> None:
    (frontend / "index.html").write_text(content)
    _git(frontend, "add", "index.html")
    _git(frontend, "commit", "-m", subject)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def _write_strict_acceptance(
    harness: Path,
    accepted: list[tuple[int, int]],
    *,
    mutation_kinds: dict[int, str] | None = None,
    write_certificates: bool = True,
) -> None:
    tape_lines = []
    for round_num, sprint_num in accepted:
        check = {
            "id": f"UI-{round_num:03d}",
            "route": "/",
            "actions": [
                {"action": "assert_visible", "selector": "main"},
                {"action": "assert_count", "selector": "main", "count": 1},
            ],
        }
        _write_json(
            harness / f"browser_evidence_round_{round_num}.json",
            {"checks": [{"check_id": check["id"], "status": "ok"}]},
        )
        tape_lines.append(
            json.dumps(
                {
                    "schema_version": "accepted-tape-v1",
                    "status": "ok",
                    "sprint": sprint_num,
                    "round": round_num,
                    "checks": [check],
                    "evidence_ref": f".harness/browser_evidence_round_{round_num}.json",
                }
            )
        )
    (harness / "accepted_tapes.jsonl").write_text("\n".join(tape_lines) + "\n")

    accepted_checks = [json.loads(line) for line in tape_lines]
    for round_num, sprint_num in accepted:
        prior = [
            check
            for record in accepted_checks
            if int(record["sprint"]) < sprint_num
            for check in record["checks"]
        ]
        if prior:
            _write_json(
                harness / f"accepted_tape_replay_round_{round_num}.json",
                {
                    "checks": [
                        {"check_id": check["id"], "status": "ok"}
                        for check in prior
                    ]
                },
            )

    if not mutation_kinds:
        return
    _write_json(harness / "minimality_policy.json", {"enabled": True})
    for round_num, kind in mutation_kinds.items():
        sprint_num = next(sprint for current_round, sprint in accepted if current_round == round_num)
        baseline_name = f"edit_dom_source_sprint_{sprint_num}.json"
        _write_json(
            harness / baseline_name,
            {"version": 4, "stable": True, "roots": [], "fragments": []},
        )
        _write_json(
            harness / f"minimal_path_plan_round_{round_num}.json",
            {
                "schema_version": "minimal-path-plan-v3",
                "owner": "harness",
                "status": "ready",
                "source_change_cone": {},
                "dom_change_cone": {},
                "route_scope": {},
            },
        )
        _write_json(
            harness / f"edit_scope_round_{round_num}.json",
            {
                "schema_version": "edit-scope-v4",
                "owner": "harness",
                "baseline": f".harness/{baseline_name}",
                "allowed_fragment_keys": [],
                "expected_new_fragments": [],
            },
        )
        _write_json(
            harness / f"recommended_scope_state_round_{round_num}.json",
            {"touched_paths": ["frontend/index.html"], "validation_last_ok": True},
        )
        if write_certificates:
            _write_json(
                harness / f"minimality_round_{round_num}_{kind}.json",
                {"status": "certified"},
            )


def _write_runtime_failure(harness: Path, round_num: int) -> None:
    _write_json(
        harness / f"browser_evidence_round_{round_num}.json",
        {"checks": [{"check_id": f"UI-{round_num:03d}", "status": "action_failed"}]},
    )


def test_round_commit_resolution_prefers_build_provenance_over_commit_position(
    tmp_path: Path,
):
    frontend = tmp_path / "frontend"
    harness = tmp_path / ".harness"
    frontend.mkdir()
    harness.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test")
    _git(frontend, "config", "user.email", "test@example.com")
    _commit(frontend, "feat: sprint one", "<main>first</main>")
    _commit(frontend, "fix: sprint one config", "<main>accepted first</main>")
    sprint_one = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=frontend,
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()
    _commit(frontend, "feat: sprint two", "<main>accepted second</main>")
    sprint_two = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=frontend,
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()
    _write_json(
        harness / "round_build_map.json",
        {
            "2": {
                "round": 2,
                "source_commit": sprint_one,
                "destination_commit": sprint_two,
            }
        },
    )

    resolved = _resolved_round_commits(
        harness=harness, frontend=frontend, grade_rounds={1, 2}
    )

    assert resolved == {1: sprint_one, 2: sprint_two}


def test_transferred_tapes_require_replay_but_do_not_hide_local_acceptance(tmp_path: Path):
    harness = tmp_path / ".harness"
    harness.mkdir()
    _write_strict_acceptance(harness, [(2, 1)])
    path = harness / "accepted_tapes.jsonl"
    current = path.read_text()
    parent = {
        "schema_version": "accepted-tape-v1", "status": "ok",
        "sprint": 1, "round": 0,
        "lineage_source": "sequential_edit_chain",
        "evidence_ref": "chain_parent_accepted_checkpoint",
        "checks": [{"id": "q1__saved", "route": "/", "actions": [
            {"action": "assert_visible", "selector": "#saved"}
        ]}],
    }
    path.write_text(json.dumps(parent) + "\n" + current)
    assert _accepted_tape_rounds(harness) == {2}
    assert not _accepted_tape_replay_passed(harness, round_num=2, sprint_num=1)
    evidence = harness / "accepted_tape_replay_round_2.json"
    _write_json(evidence, {"checks": [{"check_id": "q1__saved", "status": "ok"}]})
    assert _accepted_tape_replay_passed(harness, round_num=2, sprint_num=1)
    _write_json(evidence, {"checks": [{"check_id": "q1__saved", "status": "action_failed"}]})
    assert not _accepted_tape_replay_passed(harness, round_num=2, sprint_num=1)
    parent["round"] = 1
    path.write_text(json.dumps(parent) + "\n" + current)
    assert _accepted_tape_rounds(harness) == set()


def test_later_checkpoint_requires_prior_accepted_tape_replay(tmp_path: Path):
    harness = tmp_path / ".harness"
    harness.mkdir()
    _write_strict_acceptance(harness, [(1, 1), (2, 2)])

    assert _accepted_tape_replay_passed(
        harness, round_num=2, sprint_num=2
    ) is True
    (harness / "accepted_tape_replay_round_2.json").unlink()
    assert _accepted_tape_replay_passed(
        harness, round_num=2, sprint_num=2
    ) is False


def test_minimal_path_provenance_preserves_guidance_decisions(tmp_path: Path):
    harness = tmp_path / ".harness"
    harness.mkdir()
    _write_json(
        harness / "minimal_path_plan_round_2.json",
        {
            "schema_version": "minimal-path-plan-v1",
            "owner": "harness",
            "source_change_cone": {
                "initial_paths": ["frontend/App.jsx"],
                "local_paths": ["frontend/App.jsx"],
                "dependency_paths": ["frontend/app.css"],
            },
            "dom_change_cone": {"allowed_root_keys": ["main"]},
            "route_scope": {
                "target_routes": ["/catalog"],
                "protected_routes": ["/settings"],
                "cross_route_shared_paths": ["frontend/src/Shell.jsx"],
                "off_target_paths": ["frontend/src/Settings.jsx"],
            },
        },
    )
    _write_json(
        harness / "recommended_scope_state_round_2.json",
        {
            "touched_paths": ["frontend/App.jsx", "frontend/app.css"],
            "validation_last_ok": True,
        },
    )

    provenance = _minimal_path_provenance(harness, 2)

    assert provenance["status"] == "advisory"
    assert provenance["initial_paths"] == ["frontend/App.jsx"]
    assert provenance["touched_paths"] == ["frontend/App.jsx", "frontend/app.css"]
    assert provenance["plan_artifact"] == ".harness/minimal_path_plan_round_2.json"
    assert provenance["target_routes"] == ["/catalog"]
    assert provenance["protected_routes"] == ["/settings"]
    assert provenance["cross_route_shared_paths"] == ["frontend/src/Shell.jsx"]


def test_evidence_only_checkpoint_reuses_last_matching_mutation_ledger(tmp_path: Path):
    harness = tmp_path / ".harness"
    harness.mkdir()
    _write_json(harness / "minimality_policy.json", {"enabled": True})
    _write_json(
        harness / "minimality_round_9_edit.json",
        {"status": "certified"},
    )
    for round_num in (6, 9):
        baseline_name = f"repair_dom_source_round_{round_num}.json"
        _write_json(
            harness / baseline_name,
            {"version": 4, "stable": True, "roots": [], "fragments": []},
        )
        _write_json(
            harness / f"minimal_path_plan_round_{round_num}.json",
            {
                "schema_version": "minimal-path-plan-v3",
                "owner": "harness",
                "status": "ready",
                "source_change_cone": {"initial_paths": ["frontend/library.js"]},
                "dom_change_cone": {},
                "route_scope": {},
            },
        )
        _write_json(
            harness / f"edit_scope_round_{round_num}.json",
            {
                "schema_version": "edit-scope-v4",
                "owner": "harness",
                "baseline": f".harness/{baseline_name}",
                "allowed_fragment_keys": [],
                "expected_new_fragments": [],
            },
        )
    _write_json(
        harness / "recommended_scope_state_round_6.json",
        {"touched_paths": ["frontend/library.js"], "validation_last_ok": True},
    )
    _write_json(
        harness / "round_build_map.json",
        {
            "6": {
                "round": 6,
                "sprint": 2,
                "source_commit": "broken",
                "destination_commit": "accepted",
            },
            "9": {
                "round": 9,
                "sprint": 2,
                "source_commit": "accepted",
                "destination_commit": "accepted",
            },
        },
    )

    assert _strict_mutation_evidence_passed(harness, 9, "edit") is True
    provenance = _minimal_path_provenance(harness, 9)
    assert provenance["mutation_round"] == 6
    assert provenance["evidence_round"] == 9
    assert provenance["plan_artifact"] == ".harness/minimal_path_plan_round_6.json"
    assert provenance["touched_paths"] == ["frontend/library.js"]


@pytest.mark.parametrize("repair_metadata_source", ["evaluator", "repair_generator"])
def test_export_run_builds_generate_edit_and_real_repair_records(tmp_path: Path, repair_metadata_source):
    run_dir = tmp_path / "natural_case"
    frontend = run_dir / "frontend"
    harness = run_dir / ".harness"
    frontend.mkdir(parents=True)
    harness.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test")
    _git(frontend, "config", "user.email", "test@example.com")
    (frontend / ".gitignore").write_text("dist/\n")
    _git(frontend, "add", ".gitignore")
    _git(frontend, "commit", "-m", "chore: baseline")
    _commit(frontend, "feat: sprint one attempt", "<main>broken</main>")
    _commit(frontend, "fix: sprint one repair", "<main>checkpoint one</main>")
    _commit(frontend, "feat: sprint two", "<main>checkpoint two</main>")

    _write_json(harness / "sprint_plan.json", {"total_sprints": 2, "sprints": [
        {"number": 1, "title": "Foundation", "goal": "Build foundation", "deliverables": ["Home"]},
        {"number": 2, "title": "Search", "goal": "Add search", "deliverables": ["Search box"]},
    ]})
    _write_json(harness / "feature_list.json", {"features": [
        {"id": "F1", "name": "Home", "sprint": 1},
        {"id": "F2", "name": "Search Autocomplete", "sprint": 2},
    ]})
    _write_json(harness / "grade_round_1.json", {
        "round": 1, "sprint": 1, "overall_passed": False,
        "mode_recommendation": "repair", "criteria": {"functionality": {"notes": "Search button is broken"}},
        "ui_checks": [{
            "check_id": "UI-001", "status": "fail",
            "notes": "Search button is broken",
        }],
        "repair_instructions": ["Connect the search button to the filtering state."],
        "repair_task_descriptions": [{
            "task_type": "Loss of Interactivity",
            "description": "The search button does not respond to a normal click.",
            "evidence_ids": ["UI-001"],
        }],
    })
    _write_json(harness / "grade_round_2.json", {
        "round": 2, "sprint": 1, "overall_passed": True, "mode_recommendation": "generate_next_sprint",
    })
    _write_json(harness / "grade_round_3.json", {
        "round": 3, "sprint": 2, "overall_passed": True, "mode_recommendation": "complete",
    })
    (harness / "feedback_round_1.md").write_text("Fix the broken search button.")
    for round_num in (1, 2, 3):
        screenshot = harness / f"visual_round_{round_num}_home.png"
        screenshot.write_bytes(b"png")
        _write_json(harness / f"visual_manifest_round_{round_num}.json", {
            "round": round_num,
            "screenshots": [f".harness/{screenshot.name}"],
        })
    _write_strict_acceptance(
        harness,
        [(2, 1), (3, 2)],
        mutation_kinds={2: "repair", 3: "edit"},
    )
    _write_runtime_failure(harness, 1)

    if repair_metadata_source == "repair_generator":
        grade_path = harness / "grade_round_1.json"
        failed = json.loads(grade_path.read_text())
        labels = failed.pop("repair_task_descriptions")
        _write_json(grade_path, failed)
        _write_json(harness / "harness_state.json", {"supplied_atomic_plan": True})
        _write_json(harness / "repair_metadata_round_2.json", {
            "source": "same_repair_model_response", "round": 2, "repair_task_descriptions": labels})

    records = export_run(run_dir)

    assert [record["task"] for record in records] == [
        "text-editing", "text-repair", "text-generation", "text-generation", "text-generation"
    ]
    generation = next(
        record for record in records
        if record["quality"].get("trajectory_role") == "complete_generate"
    )
    assert generation["instance_id"] == "natural_case__complete_generate"
    assert generation["reference"]["dst_code"][0]["code"] == "<main>checkpoint two</main>"
    assert generation["quality"]["trajectory_role"] == "complete_generate"
    assert generation["quality"]["accepted_sprints"] == [1, 2]
    edit = next(record for record in records if record["task"] == "text-editing")
    assert edit["instruction"]["src_code"][0]["code"] == "<main>checkpoint one</main>"
    assert edit["reference"]["dst_code"][0]["code"] == "<main>checkpoint two</main>"
    assert edit["label_modified_files"][0]["task_type"] == "Search Autocomplete"
    assert edit["quality"]["task_descriptions"] == []
    repair = next(record for record in records if record["task"] == "text-repair")
    if repair_metadata_source == "repair_generator":
        assert "The search button does not respond to a normal click." in repair["description"]
    else:
        assert "Search button is broken" in repair["description"]
    assert repair["instruction"]["src_code"][0]["code"] == "<main>broken</main>"
    assert len(repair["images"]["src_screenshot"]) == 1
    assert repair["quality"]["same_sprint_recovery"] is True
    assert repair["task_type"] == ["Loss of Interactivity"]
    assert repair["quality"]["defect_injection"] is False


def test_export_run_does_not_publish_partial_generate_mainline(tmp_path: Path):
    run_dir = tmp_path / "partial_generate"
    frontend = run_dir / "frontend"
    harness = run_dir / ".harness"
    frontend.mkdir(parents=True)
    harness.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test")
    _git(frontend, "config", "user.email", "test@example.com")
    _commit(frontend, "feat: sprint one", "<main>only checkpoint one</main>")
    _write_json(harness / "sprint_plan.json", {"total_sprints": 2, "sprints": [
        {"number": 1, "title": "Foundation", "goal": "Build foundation", "deliverables": []},
        {"number": 2, "title": "Search", "goal": "Add search", "deliverables": []},
    ]})
    _write_json(harness / "feature_list.json", {"features": [
        {"id": "F1", "name": "Home", "sprint": 1},
        {"id": "F2", "name": "Search", "sprint": 2},
    ]})
    _write_json(harness / "grade_round_1.json", {
        "round": 1, "sprint": 1, "overall_passed": True,
        "mode_recommendation": "generate_next_sprint",
    })
    _write_strict_acceptance(harness, [(1, 1)], mutation_kinds={1: "edit"})

    records = export_run(run_dir)
    assert [record["quality"]["trajectory_role"] for record in records] == [
        "checkpoint_generate"
    ]
    assert records[0]["quality"]["checkpoint_index"] == 1


def test_export_run_treats_accepted_seed_baseline_as_first_forward_edit(tmp_path: Path):
    run_dir = tmp_path / "forward_case"
    frontend = run_dir / "frontend"
    harness = run_dir / ".harness"
    frontend.mkdir(parents=True); harness.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test"); _git(frontend, "config", "user.email", "test@example.com")
    _commit(frontend, "chore: accepted forward-edit baseline", "<main>before</main>")
    baseline = subprocess.run(["git", "rev-parse", "HEAD"], cwd=frontend, text=True, check=True, capture_output=True).stdout.strip()
    _commit(frontend, "feat: add reading aid", "<main>after</main>")
    _write_json(run_dir / "seed_manifest.json", {"baseline_commit": baseline})
    _write_json(harness / "sprint_plan.json", {"sprints": [{"number": 1, "title": "Aid", "goal": "Aid", "deliverables": []}]})
    _write_json(harness / "feature_list.json", {"features": [{"id": "F1", "name": "Reading aid", "description": "Add an aid.", "sprint": 1}]})
    _write_json(harness / "grade_round_1.json", {
        "round": 1, "sprint": 1, "overall_passed": True,
        "target_exit_criteria_results": [{
            "critical": True, "passed": True, "notes": "Clicked the reading-aid control and observed its panel."
        }],
        "ui_checks": [{
            "critical": True, "status": "pass", "notes": "Clicked the reading-aid control and observed its panel."
        }],
    })
    _write_strict_acceptance(harness, [(1, 1)], mutation_kinds={1: "edit"})

    records = export_run(run_dir)

    assert [record["task"] for record in records] == ["text-editing"]
    assert records[0]["trajectory"]["source_commit"] == baseline
    assert records[0]["quality"]["edit_kind"] == "atomic_edit"
    assert records[0]["quality"]["task_count"] == 1


def test_user_image_input_flows_into_image_edit_v2(tmp_path: Path):
    from src.orchestration.task_inputs import stage_task_inputs

    run_dir = tmp_path / "image_forward_case"
    frontend = run_dir / "frontend"
    harness = run_dir / ".harness"
    frontend.mkdir(parents=True); harness.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test"); _git(frontend, "config", "user.email", "test@example.com")
    _commit(frontend, "chore: accepted forward-edit baseline", "<main>before</main>")
    baseline = subprocess.run(["git", "rev-parse", "HEAD"], cwd=frontend, text=True, check=True, capture_output=True).stdout.strip()
    _commit(frontend, "feat: match reference", "<main>after</main>")
    reference = tmp_path / "reference.png"
    reference.write_bytes(b"\x89PNG\r\n\x1a\nreference")
    stage_task_inputs(run_dir, [reference])
    _write_json(run_dir / "seed_manifest.json", {"baseline_commit": baseline})
    _write_json(harness / "sprint_plan.json", {"sprints": [{"number": 1, "title": "Reference", "goal": "Match reference", "deliverables": []}]})
    _write_json(harness / "feature_list.json", {"features": [{"id": "F1", "name": "Reference layout", "description": "Match the supplied image.", "sprint": 1}]})
    _write_json(harness / "grade_round_1.json", {
        "round": 1, "sprint": 1, "overall_passed": True,
        "target_exit_criteria_results": [{"critical": True, "passed": True, "notes": "Observed reference layout."}],
        "ui_checks": [{"critical": True, "status": "pass", "notes": "Observed reference layout."}],
    })
    _write_strict_acceptance(
        harness,
        [(1, 1)],
        mutation_kinds={1: "edit"},
    )

    records = export_run(run_dir)
    converted = to_v2_records(records)

    assert len(converted["image-edit.v2"]) == 1
    assert converted["image-edit.v2"][0]["edit_kind"] == "atomic_edit"
    staged = converted["image-edit.v2"][0]["input_images"]
    assert len(staged) == 1
    assert Path(staged[0]).is_file()


@pytest.mark.parametrize("plan_version", ["minimal-path-plan-v3", "minimal-path-plan-v6"])
def test_new_policy_excludes_forward_edit_without_certified_minimality(tmp_path: Path, plan_version):
    run_dir = tmp_path / "forward_guarded"
    frontend = run_dir / "frontend"
    harness = run_dir / ".harness"
    frontend.mkdir(parents=True); harness.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test"); _git(frontend, "config", "user.email", "test@example.com")
    _commit(frontend, "chore: accepted forward-edit baseline", "<main>before</main>")
    baseline = subprocess.run(["git", "rev-parse", "HEAD"], cwd=frontend, text=True, check=True, capture_output=True).stdout.strip()
    _commit(frontend, "feat: add control", "<main>after</main>")
    _write_json(run_dir / "seed_manifest.json", {"baseline_commit": baseline})
    _write_json(harness / "minimality_policy.json", {"enabled": True})
    _write_json(harness / "sprint_plan.json", {"sprints": [{"number": 1, "title": "Aid", "goal": "Aid", "deliverables": []}]})
    _write_json(harness / "feature_list.json", {"features": [{"id": "F1", "name": "Aid", "description": "Add aid.", "sprint": 1}]})
    _write_json(harness / "grade_round_1.json", {
        "round": 1, "sprint": 1, "overall_passed": True,
        "ui_checks": [{"critical": True, "status": "pass", "notes": "Observed aid."}],
        "target_exit_criteria_results": [{"critical": True, "passed": True, "notes": "Observed aid."}],
    })
    _write_strict_acceptance(
        harness,
        [(1, 1)],
        mutation_kinds={1: "edit"},
        write_certificates=False,
    )

    plan_path = harness / "minimal_path_plan_round_1.json"
    plan = json.loads(plan_path.read_text())
    _write_json(plan_path, {**plan, "schema_version": plan_version})
    assert export_run(run_dir) == []
    exempt = export_run(run_dir, require_minimality=False)
    assert [record["task"] for record in exempt] == ["text-editing"]
    assert exempt[0]["quality"]["counterfactual_minimality"]["status"] == "skipped_by_user_policy"
    assert apply_patches(exempt[0]["instruction"]["src_code"], exempt[0]["label_modified_files"]) == exempt[0]["reference"]["dst_code"]
    grade_path = harness / "grade_round_1.json"
    grade = json.loads(grade_path.read_text())
    _write_json(grade_path, {**grade, "overall_passed": False})
    assert export_run(run_dir, require_minimality=False) == []
    _write_json(grade_path, grade)

    _write_json(harness / "minimality_round_1_edit.json", {"status": "certified"})
    records = export_run(run_dir)
    assert [record["task"] for record in records] == ["text-editing"]
    assert records[0]["quality"]["counterfactual_minimality"][0]["status"] == "certified"


def test_export_run_aggregates_four_forward_sprints_as_compound_edit(tmp_path: Path):
    run_dir = tmp_path / "forward_aggregate"
    frontend = run_dir / "frontend"
    harness = run_dir / ".harness"
    frontend.mkdir(parents=True); harness.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test"); _git(frontend, "config", "user.email", "test@example.com")
    _commit(frontend, "chore: accepted forward-edit baseline", "<main>before</main>")
    baseline = subprocess.run(["git", "rev-parse", "HEAD"], cwd=frontend, text=True, check=True, capture_output=True).stdout.strip()
    _commit(frontend, "feat: add controls", "<main>controls</main>")
    _commit(frontend, "feat: add mobile layout", "<main>controls mobile</main>")
    _commit(frontend, "feat: add search", "<main>controls mobile search</main>")
    _commit(frontend, "feat: add cart", "<main>controls mobile search cart</main>")
    _write_json(run_dir / "seed_manifest.json", {"baseline_commit": baseline})
    _write_json(harness / "sprint_plan.json", {"sprints": [
        {"number": 1, "title": "Controls", "goal": "Controls", "deliverables": []},
        {"number": 2, "title": "Mobile", "goal": "Mobile", "deliverables": []},
        {"number": 3, "title": "Search", "goal": "Search", "deliverables": []},
        {"number": 4, "title": "Cart", "goal": "Cart", "deliverables": []},
    ]})
    _write_json(harness / "feature_list.json", {"features": [
        {"id": "F1", "name": "View controls", "description": "Add controls.", "sprint": 1},
        {"id": "F2", "name": "Responsive layout", "description": "Add mobile layout.", "sprint": 2},
        {"id": "F3", "name": "Search", "description": "Add search.", "sprint": 3},
        {"id": "F4", "name": "Shopping cart", "description": "Add a cart.", "sprint": 4},
    ]})
    for round_num, sprint_num in ((1, 1), (2, 2), (3, 3), (4, 4)):
        _write_json(harness / f"grade_round_{round_num}.json", {
            "round": round_num, "sprint": sprint_num, "overall_passed": True,
            "target_exit_criteria_results": [{"critical": True, "passed": True, "notes": "Observed control behavior."}],
            "ui_checks": [{"critical": True, "status": "pass", "notes": "Observed control behavior."}],
        })
    _write_strict_acceptance(
        harness,
        [(1, 1), (2, 2), (3, 3), (4, 4)],
        mutation_kinds={1: "edit", 2: "edit", 3: "edit", 4: "edit"},
    )

    records = export_run(run_dir)

    assert [record["task"] for record in records] == ["text-editing"]
    edit = records[0]
    assert edit["task_type"] == [
        "View controls", "Responsive layout", "Search", "Shopping cart"
    ]
    assert edit["quality"]["edit_kind"] == "compound_edit"
    assert edit["quality"]["task_count"] == 4
    assert edit["quality"]["accepted_sprints"] == [1, 2, 3, 4]
    assert edit["reference"]["dst_code"][0]["code"] == "<main>controls mobile search cart</main>"
    assert apply_patches(edit["instruction"]["src_code"], edit["label_modified_files"]) == edit["reference"]["dst_code"]


def test_export_run_excludes_accepted_edit_with_unverified_critical_interaction(tmp_path: Path):
    run_dir = tmp_path / "forward_unverified_case"
    frontend = run_dir / "frontend"
    harness = run_dir / ".harness"
    frontend.mkdir(parents=True); harness.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test"); _git(frontend, "config", "user.email", "test@example.com")
    _commit(frontend, "chore: accepted forward-edit baseline", "<main>before</main>")
    baseline = subprocess.run(["git", "rev-parse", "HEAD"], cwd=frontend, text=True, check=True, capture_output=True).stdout.strip()
    _commit(frontend, "feat: add reading aid", "<main>after</main>")
    _write_json(run_dir / "seed_manifest.json", {"baseline_commit": baseline})
    _write_json(harness / "sprint_plan.json", {"sprints": [{"number": 1, "title": "Aid", "goal": "Aid", "deliverables": []}]})
    _write_json(harness / "feature_list.json", {"features": [{"id": "F1", "name": "Reading aid", "description": "Add an aid.", "sprint": 1}]})
    _write_json(harness / "grade_round_1.json", {
        "round": 1, "sprint": 1, "overall_passed": True,
        "ui_checks": [{"critical": True, "status": "partial", "notes": "Not verified within evaluation budget."}],
        "target_exit_criteria_results": [{"critical": True, "passed": True, "notes": "Could not verify the scroll interaction."}],
    })

    assert export_run(run_dir) == []


def test_export_trace_gate_rejects_success_claim_with_failed_browser_click(tmp_path: Path):
    from scripts.export_trajectory_dataset import _trace_has_no_failed_browser_click

    harness = tmp_path / ".harness"
    traces = harness / "traces"
    traces.mkdir(parents=True)
    (traces / "evaluator_round_1.jsonl").write_text(
        '{"event":"tool","name":"browser_click","ok":false,"output":"timeout"}\n'
    )

    assert _trace_has_no_failed_browser_click(harness, 1) is False


def test_export_trace_gate_rejects_force_only_click(tmp_path: Path):
    from scripts.export_trajectory_dataset import _trace_has_no_failed_browser_click

    harness = tmp_path / ".harness"
    traces = harness / "traces"
    traces.mkdir(parents=True)
    (traces / "evaluator_round_1.jsonl").write_text(
        '{"event":"assistant","message":{"tool_calls":[{"id":"click-1",'
        '"function":{"name":"browser_click","arguments":"{\\"selector\\":\\"#save\\",\\"force\\":true}"}}]}}\n'
        '{"event":"tool","name":"browser_click","ok":true,"output":"clicked"}\n',
        encoding="utf-8",
    )

    assert _trace_has_no_failed_browser_click(harness, 1) is False


def test_make_patches_uses_local_context_instead_of_whole_file():
    from scripts.export_trajectory_dataset import apply_patches, make_patches

    before = "header\nkeep one\nold value\nkeep two\nfooter\n"
    after = "header\nkeep one\nnew value\nkeep two\nfooter\n"
    src = [{"path": "app.js", "code": before}]
    dst = [{"path": "app.js", "code": after}]

    patches = make_patches(src, dst, "Interaction")

    assert patches[0]["search"] != before
    assert "old value" in patches[0]["search"]
    assert apply_patches(src, patches) == dst


def test_make_patches_expands_context_instead_of_using_whole_file():
    from scripts.export_trajectory_dataset import apply_patches, make_patches

    before = (
        "first section\n"
        "shared label\n"
        "old value\n"
        "shared footer\n"
        "second section\n"
        "shared label\n"
        "old value\n"
        "shared footer\n"
        "end\n"
    )
    after = before.replace("second section\nshared label\nold value", "second section\nshared label\nnew value")
    src = [{"path": "app.js", "code": before}]
    dst = [{"path": "app.js", "code": after}]

    patches = make_patches(src, dst, "Interaction")

    assert len(patches) == 1
    assert patches[0]["search"] != before
    assert before.count(patches[0]["search"]) == 1
    assert apply_patches(src, patches) == dst


def test_make_patches_rejects_edit_that_only_has_a_whole_file_anchor():
    from scripts.export_trajectory_dataset import make_patches

    src = [{"path": "app.js", "code": "aaaa"}]
    dst = [{"path": "app.js", "code": "bbbb"}]

    with pytest.raises(ValueError, match="no unique bounded local Search/Replace"):
        make_patches(src, dst, "Interaction")


def test_apply_patches_rejects_non_unique_search():
    from scripts.export_trajectory_dataset import apply_patches

    src = [{"path": "app.js", "code": "same\nsame\n"}]
    patches = [
        {
            "path": "app.js",
            "search": "same\n",
            "replace": "changed\n",
            "task_type": "Interaction",
        }
    ]

    with pytest.raises(ValueError, match="patch search is not unique"):
        apply_patches(src, patches)


def test_make_patches_uses_explicit_create_file_operation():
    from scripts.export_trajectory_dataset import (
        _quality_tier,
        apply_patches,
        make_patches,
        to_v2_records,
    )

    src = [{"path": "app.js", "code": "keep\n"}]
    dst = [
        {"path": "app.js", "code": "keep\n"},
        {"path": "catalog.js", "code": "export const catalog = [];\n"},
    ]

    patches = make_patches(src, dst, "Catalog")

    assert patches == [
        {
            "path": "catalog.js",
            "operation": "create_file",
            "content": "export const catalog = [];\n",
            "task_type": "Catalog",
        }
    ]
    assert apply_patches(src, patches) == dst
    tier, reasons = _quality_tier("text-editing", patches, ["Catalog"])
    assert tier == "natural_trajectory"
    assert reasons == ["explicit_file_creation_requires_native_schema"]
    record = {
        "instance_id": "native__new_file",
        "task": "text-editing",
        "task_type": ["Catalog"],
        "description": "Add a catalog module.",
        "instruction": {"src_code": src},
        "label_modified_files": patches,
        "images": {"src_screenshot": [], "dst_screenshot": []},
        "trajectory": {"source_commit": "abc", "destination_commit": "def"},
        "quality": {},
    }
    assert to_v2_records([record])["text-edit.v2"] == []


def test_export_output_is_append_only_and_resume_idempotent(tmp_path: Path):
    path = tmp_path / "records.jsonl"
    first = {"instance_id": "a", "status": "ok"}
    second = {"instance_id": "b", "status": "ok"}

    assert append_jsonl_records(path, [first]) == 1
    assert append_jsonl_records(path, [first, second]) == 1

    assert [json.loads(line)["instance_id"] for line in path.read_text().splitlines()] == [
        "a",
        "b",
    ]


def test_export_output_deduplicates_same_batch(tmp_path: Path):
    path = tmp_path / "records.jsonl"
    duplicate = {"instance_id": "same", "status": "ok"}

    assert append_jsonl_records(path, [duplicate, duplicate]) == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_export_run_excludes_unverified_evaluator_failure(tmp_path: Path):
    run_dir = tmp_path / "uncertain_case"
    frontend = run_dir / "frontend"
    harness = run_dir / ".harness"
    frontend.mkdir(parents=True)
    harness.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test")
    _git(frontend, "config", "user.email", "test@example.com")
    _commit(frontend, "feat: first", "<main>one</main>")
    _commit(frontend, "fix: retry", "<main>two</main>")
    _write_json(harness / "sprint_plan.json", {"total_sprints": 1, "sprints": [
        {"number": 1, "title": "One", "goal": "One", "deliverables": []},
    ]})
    _write_json(harness / "feature_list.json", {"features": []})
    _write_json(harness / "grade_round_1.json", {
        "round": 1, "sprint": 1, "overall_passed": False,
        "target_exit_criteria_results": [{
            "passed": False, "notes": "Could not verify the modal within evaluation budget."
        }],
    })
    _write_json(harness / "grade_round_2.json", {
        "round": 2, "sprint": 1, "overall_passed": True,
    })
    _write_strict_acceptance(harness, [(2, 1)])

    records = export_run(run_dir)

    assert [record["quality"]["trajectory_role"] for record in records] == [
        "checkpoint_generate", "complete_generate"
    ]


def test_visual_failure_with_concrete_review_is_real_evidence():
    from scripts.export_trajectory_dataset import _confirmed_failure_evidence

    grade = {
        "phase_results": {"render_gate": "pass", "appearance": "fail"},
        "criteria": {
            "design_quality": {
                "passed": False,
                "notes": "Cards have uniform height and the required hierarchy is absent.",
            },
            "originality": {"passed": True, "notes": "ok"},
            "craft": {"passed": True, "notes": "ok"},
        },
    }
    evidence = _confirmed_failure_evidence(grade)
    assert evidence == [
        "design_quality: Cards have uniform height and the required hierarchy is absent."
    ]


def test_export_run_excludes_infrastructure_failure(tmp_path: Path):
    run_dir = tmp_path / "infra_case"
    frontend = run_dir / "frontend"
    harness = run_dir / ".harness"
    frontend.mkdir(parents=True)
    harness.mkdir()
    _git(frontend, "init", "-b", "main")
    _git(frontend, "config", "user.name", "test")
    _git(frontend, "config", "user.email", "test@example.com")
    _commit(frontend, "feat: first", "<main>one</main>")
    _commit(frontend, "fix: retry", "<main>two</main>")
    _write_json(harness / "sprint_plan.json", {"total_sprints": 1, "sprints": [
        {"number": 1, "title": "One", "goal": "One", "deliverables": []},
    ]})
    _write_json(harness / "feature_list.json", {"features": []})
    _write_json(harness / "grade_round_1.json", {
        "round": 1, "sprint": 1, "overall_passed": False,
        "criteria": {"design_quality": {"notes": "vision scorer unavailable: request timed out"}},
    })
    _write_json(harness / "grade_round_2.json", {
        "round": 2, "sprint": 1, "overall_passed": True,
    })
    _write_strict_acceptance(harness, [(2, 1)])

    records = export_run(run_dir)

    assert [record["quality"]["trajectory_role"] for record in records] == [
        "checkpoint_generate", "complete_generate"
    ]


def test_v2_repair_contract_hides_diagnosis_and_requires_paired_images():
    from scripts.export_trajectory_dataset import to_v2_records

    record = {
        "instance_id": "natural__repair", "task": "text-repair",
        "task_type": ["Overflow", "Color Contrast", "Loss of Interactivity", "Missing Attributes"],
        "description": "Repair the exact button bug described by the evaluator.",
        "instruction": {"src_code": [{"path": "app.js", "code": "broken()"}]},
        "label_modified_files": [{"path": "app.js", "search": "broken()", "replace": "fixed()", "task_type": "Loss of Interactivity"}],
        "images": {"src_screenshot": [], "dst_screenshot": []},
        "trajectory": {"source_commit": "abc", "destination_commit": "def"},
        "quality": {
            "confirmed_failure_evidence": ["button is broken"],
            "repair_task_descriptions": [
                {"task_type": task_type, "description": f"Observed defect {index}", "evidence_ids": ["UI-001"]}
                for index, task_type in enumerate([
                    "Overflow", "Color Contrast", "Loss of Interactivity", "Missing Attributes"
                ])
            ],
        },
    }

    converted = to_v2_records([record])

    text = converted["text-repair.v2"][0]
    assert text["instruction"] == [{"path": "app.js", "code": "broken()"}]
    assert "description" not in text["instruction"]
    assert converted["image-repair.v2"] == []


def test_v2_repair_rejects_unclassified_or_too_small_natural_failure():
    from scripts.export_trajectory_dataset import to_v2_records

    record = {
        "instance_id": "natural__single_repair", "task": "text-repair",
        "task_type": ["Loss of Interactivity"],
        "instruction": {"src_code": [{"path": "app.js", "code": "broken()"}]},
        "label_modified_files": [{
            "path": "app.js", "search": "broken()", "replace": "fixed()",
            "task_type": "Loss of Interactivity",
        }],
        "images": {"src_screenshot": [], "dst_screenshot": []},
        "trajectory": {"source_commit": "abc", "destination_commit": "def"},
        "quality": {"repair_task_descriptions": [{
            "task_type": "Loss of Interactivity",
            "description": "One naturally observed issue.",
            "evidence_ids": ["UI-001"],
        }]},
    }

    assert to_v2_records([record])["text-repair.v2"] == []


def test_scope_guard_failure_is_not_a_project_repair_candidate():
    from scripts.export_trajectory_dataset import _is_real_project_failure

    assert _is_real_project_failure({
        "overall_passed": False,
        "edit_scope_audit": "fail",
        "ui_checks": [{"status": "fail", "notes": "A real-looking UI failure."}],
    }) is False


def test_scope_failure_does_not_hide_a_reproduced_ui_repair_candidate():
    from scripts.export_trajectory_dataset import _is_real_project_failure

    assert _is_real_project_failure({
        "overall_passed": False,
        "edit_scope_audit": "fail",
        "ui_checks": [{
            "critical": True,
            "status": "fail",
            "notes": "Navigator is visibly positioned on the left instead of the required right side.",
        }],
    }) is True


def test_export_quality_uses_atomic_and_compound_task_contract():
    from scripts.export_trajectory_dataset import _edit_kind, _quality_tier

    patch = [{"path": "app.js", "search": "old", "replace": "new"}]
    assert _quality_tier("text-editing", patch, ["Navigation"])[0] == "benchmark_aligned"
    assert _edit_kind(["Navigation"]) == "atomic_edit"
    for task_count in (4, 12):
        task_types = [str(index) for index in range(task_count)]
        assert _edit_kind(task_types) == "compound_edit"
        assert _quality_tier("text-editing", patch, task_types)[0] == "benchmark_aligned"
    for task_count in (2, 3, 13):
        task_types = [str(index) for index in range(task_count)]
        assert _edit_kind(task_types) is None
        assert "edit_task_count_not_1_or_4_to_12" in _quality_tier(
            "text-editing", patch, task_types
        )[1]


def test_unverified_failure_wording_is_not_a_project_repair_candidate():
    from scripts.export_trajectory_dataset import _is_real_project_failure

    assert _is_real_project_failure({
        "overall_passed": False,
        "ui_checks": [{
            "status": "partial",
            "notes": "Filtering behavior could not be fully verified within budget.",
        }],
    }) is False


def test_v2_conversion_rejects_file_creation_patch_for_reverse_compatibility():
    from scripts.export_trajectory_dataset import to_v2_records

    record = {
        "instance_id": "natural__new_file", "task": "text-repair", "task_type": ["Interaction"],
        "description": "ignored", "instruction": {"src_code": [{"path": "app.js", "code": "x"}]},
        "label_modified_files": [{"path": "server.js", "search": "", "replace": "new", "task_type": "Interaction"}],
        "images": {"src_screenshot": [], "dst_screenshot": []},
        "trajectory": {"source_commit": "abc", "destination_commit": "def"}, "quality": {},
    }
    assert to_v2_records([record])["text-repair.v2"] == []


def test_v2_edit_uses_planner_feature_descriptions_not_sprint_summary():
    from scripts.export_trajectory_dataset import to_v2_records

    record = {
        "instance_id": "natural__edit", "source_project": "/source", "task": "text-editing",
        "task_type": ["Reading aids"], "description": "Sprint title is not the model request.",
        "instruction": {"src_code": [{"path": "index.html", "code": "<main>before</main>"}]},
        "label_modified_files": [{"path": "index.html", "search": "before", "replace": "after", "task_type": "Reading aids"}],
        "images": {"src_screenshot": [], "dst_screenshot": []},
        "trajectory": {"source_commit": "abc", "destination_commit": "def"},
        "quality": {"task_descriptions": [{"task_type": "Reading aids", "description": "Add a concise summary."}]},
    }

    converted = to_v2_records([record])

    assert converted["text-edit.v2"][0]["instruction"]["description"] == [
        {"task_type": "Reading aids", "description": "Add a concise summary."}
    ]
    assert converted["text-edit.v2"][0]["edit_kind"] == "atomic_edit"
    assert converted["text-edit.v2"][0]["metadata"]["edit_kind"] == "atomic_edit"
    assert converted["text-edit.v2"][0]["metadata"]["input_contract"]["all_files_included"] is True


def test_v2_edit_rejects_two_task_bundle():
    from scripts.export_trajectory_dataset import to_v2_records

    record = {
        "instance_id": "invalid__two_tasks",
        "source_project": "/source",
        "task": "text-editing",
        "task_type": ["Search", "Navigation"],
        "description": "Two tasks are neither atomic nor compound.",
        "instruction": {"src_code": [{"path": "index.html", "code": "before"}]},
        "label_modified_files": [
            {"path": "index.html", "search": "before", "replace": "after", "task_type": "Search"}
        ],
        "images": {"src_screenshot": [], "dst_screenshot": []},
        "trajectory": {"source_commit": "abc", "destination_commit": "def"},
        "quality": {
            "task_descriptions": [
                {"task_type": "Search", "description": "Add search."},
                {"task_type": "Navigation", "description": "Add navigation."},
            ]
        },
    }

    assert to_v2_records([record])["text-edit.v2"] == []


def test_session_exports_actual_classification_and_bound_final_query(tmp_path, monkeypatch):
    import copy
    import hashlib
    from scripts import export_trajectory_dataset as module
    files = [{"path": "index.html", "code": "<main>Accepted product</main>"}]
    sha = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    grade_path = tmp_path / 'grade.json'
    _write_json(grade_path, {"overall_passed": True})
    edits = [{"edit_id": f"q{i}", "instruction": f"Add feature {i}.", "source_version": f"s{i-1}",
              "target_version": f"s{i}", "classification": {"primary": {"taxonomy": "extension", "type": "Planning"}},
              "execution": {"status": "completed", "workdir": str(tmp_path / f'q{i}'), "evaluation": str(grade_path)}} for i in range(1,5)]
    session = {"schema_version": "product_edit_session_v1", "session_id": "session1", "edits": edits,
               "selection": {"edit_count": 4}, "current_state": {"state_id": "s4", "sha256": sha}}
    path = tmp_path / 'session.json'
    _write_json(path, session)
    def export(run_dir, *, require_minimality):
        assert require_minimality is False
        return [module._base_record(run_dir=run_dir, instance_id=run_dir.name, task="text-editing", task_types=['old'],
            description='old', src_code=files, dst_code=files, patches=[], src_images=[], dst_images=[],
            source_commit='a', destination_commit='b')]
    monkeypatch.setattr(module, 'export_run', export)
    records, report = module.export_product_session(path)
    assert report['counts']['text-editing'] == 4
    assert report['generation_status'] == 'waiting_for_final_query'
    assert records[0]['description'] == edits[0]['instruction']
    assert records[0]['task_type'] == ['Planning']
    assert len({row['instance_id'] for row in records}) == 4
    (tmp_path/'dataset').mkdir()
    query = {'schema_version':'product-final-query-v1','source_sha256':sha,'state_id':'s4',
             'edit_ids':[e['edit_id'] for e in edits], 'instruction':'Create the complete product.', 'model':'gpt-5.5'}
    _write_json(tmp_path/'dataset/final_query.json', query)
    records, report = module.export_product_session(path)
    assert report['counts']['text-generation'] == 1
    assert records[-1]['instruction']['src_code'] == []
    assert records[-1]['reference']['dst_code'] == files
    assert append_jsonl_records(tmp_path/'records.jsonl',records) == 5
    assert append_jsonl_records(tmp_path/'records.jsonl',records) == 0
    _write_json(tmp_path/'dataset/final_query.json',{**query,'source_sha256':'changed'})
    with pytest.raises(ValueError, match='final Generate query'):
        module.export_product_session(path)
    broken = copy.deepcopy(session)
    broken['edits'][0]['execution']['status'] = 'pending'
    _write_json(path,broken)
    with pytest.raises(ValueError,match='continuous prefix'):
        module.export_product_session(path)


def test_session_exports_one_generate_per_bound_accepted_state(tmp_path, monkeypatch):
    import hashlib
    from scripts import export_trajectory_dataset as module
    files = [[{'path':'index.html', 'code':f'<main>State {i}</main>'}] for i in range(3)]
    grade = tmp_path/'grade.json'
    _write_json(grade, {'overall_passed':True})
    edits = [{'edit_id':f'q{i}', 'instruction':f'Add capability {i}.', 'source_version':f's{i-1}',
              'target_version':f's{i}', 'classification':{'primary':{'taxonomy':'extension','type':'Planning'}},
              'execution':{'status':'completed','workdir':str(tmp_path/f'q{i}'),'evaluation':str(grade)}}
             for i in (1,2)]
    session = {'schema_version':'product_edit_session_v1','session_id':'stateful',
               'generation_policy':'each_accepted_state','edits':edits,'selection':{'edit_count':4}}
    path = tmp_path/'session.json'
    _write_json(path, session)
    def export(run_dir, **_):
        i = int(run_dir.name[1:])
        return [module._base_record(run_dir=run_dir,instance_id=run_dir.name,task='text-editing',
            task_types=['Planning'],description='edit',src_code=files[i-1],dst_code=files[i],patches=[],
            src_images=[],dst_images=[],source_commit=f'c{i-1}',destination_commit=f'c{i}')]
    monkeypatch.setattr(module, 'export_run', export)
    def query(i):
        return {'schema_version':'product-state-query-v1','instruction':f'Create state {i}.',
                'source_sha256':hashlib.sha256(json.dumps(files[i],sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
                'state_id':f's{i}','edit_ids':[f'q{j}' for j in range(1,i+1)],'model':'gpt-5.5'}
    folder = tmp_path/'dataset/generate_queries'
    folder.mkdir(parents=True)
    _write_json(folder/'q1.json', query(1))
    rows, report = module.export_product_session(path)
    assert report['counts'] == {'text-editing':2,'text-generation':1,'text-repair':0}
    assert report['generation_status'] == 'waiting_for_state_queries'
    _write_json(folder/'q2.json', query(2))
    rows, report = module.export_product_session(path)
    generated = [r for r in rows if r['task']=='text-generation']
    assert [r['reference']['dst_code'] for r in generated] == files[1:]
    assert all(r['instruction']['src_code']==[] for r in generated)
    assert report['counts']['text-generation'] == 2
    assert append_jsonl_records(tmp_path/'records.jsonl', rows) == 4
    assert append_jsonl_records(tmp_path/'records.jsonl', rows) == 0
    # Final-state compatibility artifact must not produce an extra Generate.
    session['selection']['edit_count'] = 2
    _write_json(path, session)
    _write_json(tmp_path/'dataset/final_query.json', query(2))
    assert module.export_product_session(path)[1]['counts']['text-generation'] == 2
    for changed in ({'source_sha256':'wrong'}, {'state_id':'s1'}, {'edit_ids':['q1','q2','q3']}):
        _write_json(folder/'q2.json', {**query(2), **changed})
        with pytest.raises(ValueError, match='state Generate query'):
            module.export_product_session(path)


def test_historical_risk_export_rejects_unexecuted_audit_and_table_box_false_positive():
    grade={'hidden_oracle':{'status':'failed','failed_check_ids':['risk']},
           'repair_task_descriptions':[{'task_type':'Crowding','description':'Old risk failure','evidence_ids':['risk']}]}
    assert _repair_task_descriptions(grade,hidden_evidence={'checks':[{'check_id':'risk','steps':[
        {'action':'click','ok':False,'output':'not clickable'}]}]})==[]
    actual={'defect_type':'Crowding','passed':False,'issues':[{'kind':'less-than-2px-gap','element':'cell',
        'element_path':'#report > table:nth-of-type(1) > tbody:nth-of-type(1) > tr:nth-of-type(1)'}]}
    evidence={'checks':[{'check_id':'risk','steps':[{'action':'assert_webcompass_risk','ok':False,'output':{'actual':actual}}]}]}
    assert _repair_task_descriptions(grade,hidden_evidence=evidence)==[]
    actual['issues']=[{'kind':'less-than-2px-gap','element':'button','element_path':'#report > button:nth-of-type(1)'}]
    assert _repair_task_descriptions(grade,hidden_evidence=evidence)[0]['task_type']=='Crowding'


def test_trace_commit_provenance_covers_rechecks_without_build(tmp_path):
    from scripts.export_trajectory_dataset import _git as git_output
    frontend=tmp_path/'frontend';harness=tmp_path/'.harness';frontend.mkdir();(harness/'traces').mkdir(parents=True)
    _git(frontend,'init','-b','main');_git(frontend,'config','user.name','test');_git(frontend,'config','user.email','test@example.com')
    _commit(frontend,'chore: seed','<main>seed</main>')
    _commit(frontend,'feat: report','<main>report</main>'); first=git_output(frontend,'rev-parse','HEAD').strip()
    _commit(frontend,'fix: action','<main>report action</main>'); second=git_output(frontend,'rev-parse','HEAD').strip()
    _commit(frontend,'fix: sort','<main>sorted</main>'); third=git_output(frontend,'rev-parse','HEAD').strip()
    for n,items in [(1,[(first,'feat: report'),(second,'fix: action')]),(3,[(third,'fix: sort')])]:
        (harness/'traces'/f'generator_round_{n}.jsonl').write_text(''.join(json.dumps({
            'event':'tool','name':'run_command','ok':True,'output':f'[main {commit[:7]}] {subject}\n'})+'\n' for commit,subject in items))
    assert _resolved_round_commits(harness=harness,frontend=frontend,grade_rounds={1,2,3})=={1:second,2:second,3:third}


def test_session_reconciliation_keeps_backup_and_other_sessions(tmp_path):
    from scripts.export_trajectory_dataset import reconcile_session_records
    path=tmp_path/'records.jsonl'
    other={'instance_id':'other','trajectory':{'session_id':'other'}}
    old={'instance_id':'stale','trajectory':{'session_id':'target'}}
    before=''.join(json.dumps(record)+'\n' for record in [other,old]);path.write_text(before)
    current={'instance_id':'current','quality':{'session_id':'target'}}
    assert reconcile_session_records(path,[current],'target')==1
    assert [json.loads(line) for line in path.read_text().splitlines()]==[other,current]
    assert next(tmp_path.glob('records.before-*.jsonl')).read_text()==before
    assert reconcile_session_records(path,[current],'target')==0
    assert len(list(tmp_path.glob('records.before-*.jsonl')))==1


def test_complete_check_with_more_than_four_assertions_is_exportable(tmp_path):
    harness = tmp_path / ".harness"
    harness.mkdir()
    _write_strict_acceptance(harness, [(1, 1)])
    path = harness / "accepted_tapes.jsonl"
    tape = json.loads(path.read_text())
    tape["checks"][0]["actions"] += [
        {"action": "assert_visible", "selector": f"#item-{i}"} for i in range(5)]
    path.write_text(json.dumps(tape) + "\n")
    assert _accepted_tape_rounds(harness) == {1}


def test_product_session_export_uses_build_provenance_without_minimality_ledger(tmp_path):
    harness = tmp_path / ".harness"
    harness.mkdir()
    _write_json(harness / "harness_state.json", {"supplied_atomic_plan": True})
    _write_json(harness / "round_build_map.json", {
        "2": {"source_commit": "before", "destination_commit": "after"}})
    assert _strict_mutation_evidence_passed(harness, 2, "edit", require_minimality=False)
    assert not _strict_mutation_evidence_passed(harness, 1, "edit", require_minimality=False)
    assert not _strict_mutation_evidence_passed(harness, 2, "edit", require_minimality=True)
