from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.export_trajectory_dataset import export_run


def _git(frontend: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=frontend, check=True, capture_output=True)


def _commit(frontend: Path, subject: str, content: str) -> None:
    (frontend / "index.html").write_text(content)
    _git(frontend, "add", "index.html")
    _git(frontend, "commit", "-m", subject)


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload))


def test_export_run_builds_generate_edit_and_real_repair_records(tmp_path: Path):
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
        "ui_checks": [{"status": "fail", "notes": "Search button is broken"}],
        "repair_instructions": ["Connect the search button to the filtering state."],
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

    records = export_run(run_dir)

    assert [record["task"] for record in records] == [
        "text-generation", "text-generation", "text-editing", "text-repair"
    ]
    edit = next(record for record in records if record["task"] == "text-editing")
    assert edit["instruction"]["src_code"][0]["code"] == "<main>checkpoint one</main>"
    assert edit["reference"]["dst_code"][0]["code"] == "<main>checkpoint two</main>"
    assert edit["label_modified_files"][0]["task_type"] == "Search Autocomplete"
    assert edit["quality"]["task_descriptions"] == []
    repair = next(record for record in records if record["task"] == "text-repair")
    assert "Search button is broken" in repair["description"]
    assert repair["instruction"]["src_code"][0]["code"] == "<main>broken</main>"
    assert len(repair["images"]["src_screenshot"]) == 1
    assert repair["quality"]["same_sprint_recovery"] is True


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
    _write_json(harness / "grade_round_1.json", {"round": 1, "sprint": 1, "overall_passed": True})

    records = export_run(run_dir)

    assert [record["task"] for record in records] == ["text-editing"]
    assert records[0]["trajectory"]["source_commit"] == baseline


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

    records = export_run(run_dir)

    assert [record["task"] for record in records] == ["text-generation"]


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

    records = export_run(run_dir)

    assert [record["task"] for record in records] == ["text-generation"]


def test_v2_repair_contract_hides_diagnosis_and_requires_paired_images():
    from scripts.export_trajectory_dataset import to_v2_records

    record = {
        "instance_id": "natural__repair", "task": "text-repair",
        "task_type": ["Interaction"],
        "description": "Repair the exact button bug described by the evaluator.",
        "instruction": {"src_code": [{"path": "app.js", "code": "broken()"}]},
        "label_modified_files": [{"path": "app.js", "search": "broken()", "replace": "fixed()", "task_type": "Interaction"}],
        "images": {"src_screenshot": [], "dst_screenshot": []},
        "trajectory": {"source_commit": "abc", "destination_commit": "def"},
        "quality": {"confirmed_failure_evidence": ["button is broken"]},
    }

    converted = to_v2_records([record])

    text = converted["text-repair.v2"][0]
    assert text["instruction"] == [{"path": "app.js", "code": "broken()"}]
    assert "description" not in text["instruction"]
    assert converted["image-repair.v2"] == []


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
    assert converted["text-edit.v2"][0]["metadata"]["input_contract"]["all_files_included"] is True
