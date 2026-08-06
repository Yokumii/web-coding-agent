"""Tests for src.orchestration.schemas.

These tests cover three things:

1. Each artifact model exposes a ``filename(**params)`` classmethod that
   returns the on-disk filename (round-numbered for per-round artifacts).
2. Each artifact model rejects unknown top-level keys
   (``ConfigDict(extra="forbid")``).
3. Every JSON artifact captured in ``examples/e2e-test-*/.harness/``
   round-trips through the corresponding model. This is the
   "matches the wire format" smoke test.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.orchestration.schemas import (
    ALL_ARTIFACT_MODELS,
    AcceptedSprints,
    AssetManifest,
    DesignBrief,
    DesignTokens,
    FeatureList,
    Grades,
    HarnessState,
    LayoutContract,
    SprintPlan,
    UIVerificationPlan,
    VisualManifest,
)


# ---- filename() contract ---------------------------------------------------


def test_design_tokens_filename():
    assert DesignTokens.filename() == "design_tokens.json"


def test_feature_list_filename():
    assert FeatureList.filename() == "feature_list.json"


def test_sprint_plan_filename():
    assert SprintPlan.filename() == "sprint_plan.json"


def test_ui_verification_plan_filename():
    assert UIVerificationPlan.filename() == "ui_verification_plan.json"


def test_design_brief_filename():
    assert DesignBrief.filename() == "design/design_brief.json"


def test_layout_contract_filename():
    assert LayoutContract.filename() == "design/layout_contract.json"


def test_asset_manifest_filename():
    assert AssetManifest.filename() == "design/asset_manifest.json"


def test_accepted_sprints_filename():
    assert AcceptedSprints.filename() == "accepted_sprints.json"


def test_harness_state_filename():
    assert HarnessState.filename() == "harness_state.json"


def test_grades_filename_includes_round():
    assert Grades.filename(round_num=3) == "grade_round_3.json"


def test_visual_manifest_filename_includes_round():
    assert VisualManifest.filename(round_num=7) == "visual_manifest_round_7.json"


def test_all_artifact_models_registry_complete():
    # The registry must contain every advertised artifact model and each
    # entry must expose a callable filename classmethod.
    assert DesignTokens in ALL_ARTIFACT_MODELS
    assert FeatureList in ALL_ARTIFACT_MODELS
    assert SprintPlan in ALL_ARTIFACT_MODELS
    assert UIVerificationPlan in ALL_ARTIFACT_MODELS
    assert DesignBrief in ALL_ARTIFACT_MODELS
    assert LayoutContract in ALL_ARTIFACT_MODELS
    assert AssetManifest in ALL_ARTIFACT_MODELS
    assert AcceptedSprints in ALL_ARTIFACT_MODELS
    assert Grades in ALL_ARTIFACT_MODELS
    assert VisualManifest in ALL_ARTIFACT_MODELS
    assert HarnessState in ALL_ARTIFACT_MODELS
    for model in ALL_ARTIFACT_MODELS:
        assert hasattr(model, "filename")
        assert callable(model.filename)


# ---- strict validation (extra="forbid") ------------------------------------


def _minimal_design_tokens() -> dict:
    return {
        "theme_name": "Demo",
        "color": {"bg": "#000"},
        "typography": {"font_ui": "Inter"},
        "spacing": {"md": "16px"},
        "radius": {"md": "8px"},
        "motion": {"duration_fast": "150ms"},
        "style_rules": ["follow the tokens"],
        "anti_patterns": [],
        "visual_experiment": {
            "design_hypothesis": "Use poster-like asymmetry.",
            "reason_for_image_first": "Text-only outputs stay too templated.",
            "desired_break_from_web_templates": ["poster-like asymmetry"],
            "visual_opportunities_beyond_css": ["ink texture"],
            "forbidden_generic_patterns": ["centered card grid"],
        },
    }


def _minimal_feature_list() -> dict:
    return {
        "features": [
            {
                "id": "F001",
                "name": "Demo",
                "priority": "P0",
                "depends_on": [],
                "description": "demo feature",
                "acceptance_criteria": ["it works"],
                "status": "planned",
                "sprint": 1,
            }
        ]
    }


def _minimal_sprint_plan() -> dict:
    return {
        "total_sprints": 1,
        "sprints": [
            {
                "number": 1,
                "title": "Sprint 1",
                "goal": "Ship something",
                "feature_ids": ["F001"],
                "deliverables": ["A thing"],
                "exit_criteria": ["It exists"],
            }
        ],
    }


def _minimal_grades() -> dict:
    return {
        "round": 1,
        "criteria": {
            "design_quality": {"score": 7, "passed": True, "notes": "ok"},
        },
        "overall_passed": False,
    }


def test_design_tokens_rejects_unknown_field():
    payload = {**_minimal_design_tokens(), "bogus": "nope"}
    with pytest.raises(ValidationError):
        DesignTokens.model_validate(payload)


def test_design_tokens_minimum_accepts():
    DesignTokens.model_validate(_minimal_design_tokens())


def test_design_tokens_rejects_invalid_visual_experiment_shape():
    payload = _minimal_design_tokens()
    payload["visual_experiment"] = {}
    with pytest.raises(ValidationError):
        DesignTokens.model_validate(payload)


def test_feature_list_rejects_unknown_top_level_field():
    with pytest.raises(ValidationError):
        FeatureList.model_validate({"features": [], "bogus": 1})


def test_feature_rejects_unknown_nested_field():
    payload = _minimal_feature_list()
    payload["features"][0]["bogus"] = "x"
    with pytest.raises(ValidationError):
        FeatureList.model_validate(payload)


def test_sprint_plan_rejects_unknown_nested_field():
    payload = _minimal_sprint_plan()
    payload["sprints"][0]["bogus"] = "x"
    with pytest.raises(ValidationError):
        SprintPlan.model_validate(payload)


def test_grades_rejects_unknown_top_level_field():
    payload = {**_minimal_grades(), "bogus": "x"}
    with pytest.raises(ValidationError):
        Grades.model_validate(payload)


def test_grades_accepts_legacy_minimal_shape():
    # Early runs (e2e-test-1/2) only emitted these 5 keys + bugs/missing.
    Grades.model_validate({**_minimal_grades(), "bugs_found": [], "missing_features": []})


def test_grades_accepts_evaluation_infrastructure_failure():
    payload = {
        **_minimal_grades(),
        "evaluation_infrastructure_failure": {
            "phase": "visual_review",
            "reason": "screenshot missing",
        },
    }
    assert Grades.model_validate(payload).evaluation_infrastructure_failure is not None


def test_visual_manifest_rejects_unknown_field():
    with pytest.raises(ValidationError):
        VisualManifest.model_validate(
            {
                "round": 1,
                "app_url": "http://127.0.0.1:5173",
                "screenshots": [],
                "notes": "",
                "bogus": "x",
            }
        )


def test_harness_state_accepts_empty_dict():
    # Real artifacts have ``{}`` as the initial state — must round-trip.
    HarnessState.model_validate({})


def test_accepted_sprints_round_trip():
    payload = {"accepted": [1, 2], "current_target": 3, "last_evaluated_round": 5}
    parsed = AcceptedSprints.model_validate(payload)
    assert parsed.accepted == [1, 2]
    assert parsed.current_target == 3
    assert parsed.last_evaluated_round == 5


# ---- real examples round-trip ----------------------------------------------


REAL_EXAMPLE_DIRS = sorted(
    Path(__file__).resolve().parents[1].glob("examples/e2e-test-*/.harness")
)


@pytest.mark.parametrize(
    "harness_dir", REAL_EXAMPLE_DIRS, ids=lambda p: p.parent.name
)
def test_real_artifacts_parse(harness_dir: Path):
    pairs = [
        ("design_tokens.json", DesignTokens),
        ("feature_list.json", FeatureList),
        ("sprint_plan.json", SprintPlan),
        ("ui_verification_plan.json", UIVerificationPlan),
        ("design/design_brief.json", DesignBrief),
        ("design/layout_contract.json", LayoutContract),
        ("design/asset_manifest.json", AssetManifest),
        ("accepted_sprints.json", AcceptedSprints),
        ("harness_state.json", HarnessState),
    ]
    for filename, model in pairs:
        path = harness_dir / filename
        if path.exists():
            model.model_validate_json(path.read_text())


@pytest.mark.parametrize(
    "harness_dir", REAL_EXAMPLE_DIRS, ids=lambda p: p.parent.name
)
def test_real_round_artifacts_parse(harness_dir: Path):
    for path in harness_dir.glob("grade_round_*.json"):
        Grades.model_validate_json(path.read_text())
    for path in harness_dir.glob("visual_manifest_round_*.json"):
        VisualManifest.model_validate_json(path.read_text())
