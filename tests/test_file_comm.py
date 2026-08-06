import tempfile
from pathlib import Path

from src.orchestration.file_comm import FileComm


def _full_design_tokens(theme: str = "editorial") -> dict:
    return {
        "theme_name": theme,
        "color": {"bg": "#111111"},
        "typography": {"display": "Space Grotesk"},
        "spacing": {"base": 8},
        "radius": {"card": 16},
        "motion": {"duration_fast": 160},
        "style_rules": ["bold hierarchy"],
        "anti_patterns": [],
        "visual_experiment": {
            "design_hypothesis": "Use poster-like asymmetry.",
            "reason_for_image_first": "Text-only outputs stay too templated.",
            "desired_break_from_web_templates": ["poster-like asymmetry"],
            "visual_opportunities_beyond_css": ["ink texture"],
            "forbidden_generic_patterns": ["centered card grid"],
        },
    }


def _full_feature_list() -> dict:
    return {
        "features": [
            {
                "id": "F001",
                "name": "Hero",
                "priority": "high",
                "depends_on": [],
                "description": "Hero section.",
                "acceptance_criteria": ["Hero renders."],
                "status": "planned",
                "sprint": 1,
            }
        ]
    }


def _full_sprint_plan() -> dict:
    return {
        "total_sprints": 1,
        "sprints": [
            {
                "number": 1,
                "title": "Core sprint",
                "goal": "Ship the hero.",
                "feature_ids": ["F001"],
                "deliverables": ["Hero UI."],
                "exit_criteria": ["Hero renders."],
            }
        ],
    }


def _full_verification_plan() -> dict:
    return {
        "sprints": [
            {
                "sprint": 1,
                "checks": [
                    {
                        "id": "UI-001",
                        "feature_id": "F001",
                        "task": "View hero.",
                        "expected_result": "Hero is visible.",
                        "critical": True,
                        "category": "core_interaction",
                    }
                ],
            }
        ]
    }


def _full_accepted_sprints() -> dict:
    return {"accepted": [], "current_target": 1, "last_evaluated_round": 0}


def _full_grades(round_num: int = 1) -> dict:
    return {
        "round": round_num,
        "overall_passed": True,
        "criteria": {
            "design_quality": {"score": 7.0, "passed": True},
            "functionality": {"score": 7.0, "passed": True},
            "originality": {"score": 6.0, "passed": True},
            "craft": {"score": 7.0, "passed": True},
        },
    }


def test_write_and_read_spec():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        comm.write_spec("# My Spec\nHello")
        assert comm.read_spec() == "# My Spec\nHello"


def test_feedback_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        comm.write_feedback(1, "Fix the login button")
        assert comm.read_feedback(1) == "Fix the login button"
        assert comm.read_feedback(2) == ""


def test_grades_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        grades = _full_grades(1)
        comm.write_grades(1, grades)
        result = comm.read_grades(1)
        assert result["round"] == 1
        assert result["overall_passed"] is True


def test_missing_files():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        assert comm.read_spec() == ""
        assert comm.read_design_tokens() is None
        assert comm.read_feature_list() is None
        assert comm.read_sprint_plan() is None
        assert comm.read_ui_verification_plan() is None
        assert comm.read_design_brief() is None
        assert comm.read_layout_contract() is None
        assert comm.read_asset_manifest() is None
        assert comm.read_accepted_sprints() is None
        assert comm.read_progress() == ""
        assert comm.read_grades(99) is None
        assert comm.read_state() is None


def test_state_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        state = {"round_num": 2, "last_completed_phase": "build_r2"}
        comm.write_state(state)
        result = comm.read_state()
        assert result["round_num"] == 2
        assert result["last_completed_phase"] == "build_r2"


def test_planning_artifact_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        design_tokens = _full_design_tokens()
        feature_list = _full_feature_list()
        sprint_plan = _full_sprint_plan()
        verification_plan = _full_verification_plan()
        accepted_sprints = _full_accepted_sprints()

        comm.write_design_tokens(design_tokens)
        comm.write_feature_list(feature_list)
        comm.write_sprint_plan(sprint_plan)
        comm.write_ui_verification_plan(verification_plan)
        comm.write_accepted_sprints(accepted_sprints)

        # Pydantic model_dump may add optional defaults absent from the
        # input dict, so check that every input field round-trips rather
        # than asserting full-dict equality.
        read_tokens = comm.read_design_tokens()
        for key, value in design_tokens.items():
            assert read_tokens[key] == value

        assert comm.read_feature_list() == feature_list
        assert comm.read_sprint_plan() == sprint_plan
        assert comm.read_ui_verification_plan() == verification_plan
        assert comm.read_accepted_sprints() == accepted_sprints


def test_design_stage_artifact_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        design_brief = {
            "requested_mode": "image-first",
            "visual_strategy": "image_backed_ui",
            "reference_files": {"background_ui": ".harness/design/background_ui.png"},
            "aesthetic_intent": {"design_hypothesis": "Use asymmetry."},
            "responsive_strategy": {"desktop": "Layered", "mobile": "Stacked"},
            "overlay_regions": [{"id": "hero"}],
            "visual_success_criteria": ["Preserve hierarchy."],
            "implementation_rules": ["Keep text in HTML."],
        }
        layout_contract = {
            "viewport_targets": ["1440x900"],
            "regions": [{"id": "hero"}],
            "safe_zones": [],
            "forbidden_overlay_zones": [],
            "asset_fit": {"background_ui": "cover"},
            "responsive_rules": ["Keep controls visible."],
        }
        asset_manifest = {
            "assets": [{"id": "background_ui"}],
            "generation_records": [],
            "implementation_notes": ["Copy production assets."],
        }

        comm.write_design_brief(design_brief)
        comm.write_layout_contract(layout_contract)
        comm.write_asset_manifest(asset_manifest)

        assert comm.read_design_brief() == {**design_brief, "fallback_reason": None}
        assert comm.read_layout_contract() == layout_contract
        assert comm.read_asset_manifest() == asset_manifest


def test_utf8_artifact_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        spec = "# 规格\n设计一个带有“增量”按钮的计数器。"
        feedback = "修复按钮文案：增加"
        feature_list = _full_feature_list()
        feature_list["features"][0]["name"] = "计数器"
        feature_list["features"][0]["description"] = "支持“+1”"

        comm.write_spec(spec)
        comm.write_feedback(1, feedback)
        comm.write_feature_list(feature_list)

        assert comm.read_spec() == spec
        assert comm.read_feedback(1) == feedback
        assert comm.read_feature_list() == feature_list


def test_progress_round_trip_and_append():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        comm.write_progress("# Progress Log")
        comm.append_progress_entry("## 2026-04-27T10:30:00Z\n- Phase: planning")
        comm.append_progress_entry("## 2026-04-27T10:45:00Z\n- Phase: build")

        progress = comm.read_progress()
        assert "# Progress Log" in progress
        assert "Phase: planning" in progress
        assert "Phase: build" in progress
        assert progress.count("## 2026-04-27T") == 2


def test_initialize_planning_artifacts_creates_scaffolds():
    with tempfile.TemporaryDirectory() as tmp:
        harness_dir = Path(tmp) / ".harness"
        comm = FileComm(harness_dir)

        comm.initialize_planning_artifacts()

        assert comm.read_spec().startswith("# Draft Product - Working Title")
        assert comm.read_progress() == "# Progress Log\n"
        assert (harness_dir / "design_tokens.json").read_text(encoding="utf-8") == "{}\n"
        assert (harness_dir / "feature_list.json").read_text(encoding="utf-8") == '{\n  "features": []\n}\n'
        assert (harness_dir / "sprint_plan.json").read_text(encoding="utf-8") == (
            '{\n  "total_sprints": 0,\n  "sprints": []\n}\n'
        )
        assert (harness_dir / "ui_verification_plan.json").read_text(encoding="utf-8") == (
            '{\n  "sprints": []\n}\n'
        )


def test_reset_run_artifacts_clears_new_planning_files():
    with tempfile.TemporaryDirectory() as tmp:
        harness_dir = Path(tmp) / ".harness"
        comm = FileComm(harness_dir)
        comm.write_spec("# Spec")
        comm.write_design_tokens(_full_design_tokens())
        comm.write_feature_list(_full_feature_list())
        comm.write_sprint_plan(_full_sprint_plan())
        comm.write_ui_verification_plan(_full_verification_plan())
        comm.write_accepted_sprints(_full_accepted_sprints())
        comm.write_progress("# Progress")
        comm.write_build_log("build")
        comm.write_state({"round_num": 1})
        comm.write_feedback(1, "feedback")
        comm.write_grades(1, _full_grades(1))
        comm.write_visual_manifest(1, {"round": 1, "app_url": "http://x", "screenshots": []})
        logs_dir = harness_dir / "logs"
        traces_dir = harness_dir / "traces"
        logs_dir.mkdir()
        traces_dir.mkdir()
        (logs_dir / "frontend.log").write_text("log")
        (traces_dir / "planner.jsonl").write_text("trace")

        comm.reset_run_artifacts()

        assert comm.read_spec() == ""
        assert comm.read_design_tokens() is None
        assert comm.read_feature_list() is None
        assert comm.read_sprint_plan() is None
        assert comm.read_ui_verification_plan() is None
        assert comm.read_design_brief() is None
        assert comm.read_layout_contract() is None
        assert comm.read_asset_manifest() is None
        assert comm.read_accepted_sprints() is None
        assert comm.read_progress() == ""
        assert comm.read_build_log() == ""
        assert comm.read_state() is None
        assert comm.read_feedback(1) == ""
        assert comm.read_grades(1) is None
        assert comm.read_visual_manifest(1) is None
        assert not logs_dir.exists()
        assert not traces_dir.exists()
        assert not (harness_dir / "design").exists()


def test_reset_keeps_accepted_edit_baseline_but_removes_stale_edit_scope():
    with tempfile.TemporaryDirectory() as tmp:
        comm = FileComm(Path(tmp) / ".harness")
        baseline = comm.dir / "edit_dom_baseline.json"
        stale_scope = comm.dir / "edit_scope_round_1.json"
        baseline.write_text('{"roots": []}')
        stale_scope.write_text('{"allowed_root_keys": ["main"]}')

        comm.reset_run_artifacts()

        assert baseline.exists()
        assert not stale_scope.exists()
