from pathlib import Path

from src.orchestration.design_contract import DesignContractContext
from src.orchestration.file_comm import FileComm


def _write_design_contract(file_comm: FileComm, *, visual_strategy: str = "image_backed_ui") -> None:
    file_comm.write_design_brief(
        {
            "requested_mode": "image-first",
            "visual_strategy": visual_strategy,
            "reference_files": {"background_ui": ".harness/design/background_ui.png"},
            "aesthetic_intent": {
                "design_hypothesis": "Use poster-like asymmetry.",
                "distinctive_features_to_preserve": ["poster-like asymmetry"],
                "generic_patterns_to_avoid": ["centered card grid"],
            },
            "responsive_strategy": {"desktop": "Layered", "mobile": "Stacked"},
            "overlay_regions": [{"id": "hero"}],
            "visual_success_criteria": ["Preserve hierarchy."],
            "implementation_rules": ["Keep text in HTML."],
            "fallback_reason": (
                "image_assets_unavailable"
                if visual_strategy == "text_only_fallback"
                else None
            ),
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


def test_missing_design_contract_is_empty(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    context = DesignContractContext.load(file_comm)

    assert context.available is False
    assert context.required_refs() == []
    assert context.generator_guidance() == ""
    assert context.evaluator_assessment_lines() == []
    assert context.vision_payload() is None


def test_design_contract_required_refs_are_standardized(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_design_contract(file_comm)

    context = DesignContractContext.load(file_comm)

    assert context.available is True
    assert context.required_refs() == [
        ".harness/design/design_brief.json",
        ".harness/design/layout_contract.json",
        ".harness/design/asset_manifest.json",
    ]


def test_image_backed_generator_guidance_includes_strategy_details(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_design_contract(file_comm)

    guidance = DesignContractContext.load(file_comm).generator_guidance()

    assert "Design Stage Guidance:" in guidance
    assert "Use poster-like asymmetry." in guidance
    assert "poster-like asymmetry" in guidance
    assert "centered card grid" in guidance
    assert "Copy required production assets from `.harness/design/`" in guidance


def test_text_fallback_generator_guidance_preserves_existing_instruction(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_design_contract(file_comm, visual_strategy="text_only_fallback")

    guidance = DesignContractContext.load(file_comm).generator_guidance()

    assert "The design stage fell back to text-only" in guidance


def test_evaluator_assessment_lines_are_present_only_when_contract_exists(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_design_contract(file_comm)

    lines = DesignContractContext.load(file_comm).evaluator_assessment_lines()

    assert lines[0] == "Design Contract Assessment:"
    assert any("design_brief.json" in line for line in lines)
    assert any("layout_contract.json" in line for line in lines)
    assert any("asset_manifest.json" in line for line in lines)


def test_vision_payload_contains_design_contract_parts(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_design_contract(file_comm)

    payload = DesignContractContext.load(file_comm).vision_payload()

    assert payload == {
        "visual_strategy": "image_backed_ui",
        "aesthetic_intent": {
            "design_hypothesis": "Use poster-like asymmetry.",
            "distinctive_features_to_preserve": ["poster-like asymmetry"],
            "generic_patterns_to_avoid": ["centered card grid"],
        },
        "reference_files": {"background_ui": ".harness/design/background_ui.png"},
        "layout_contract": {
            "viewport_targets": ["1440x900"],
            "regions": [{"id": "hero"}],
            "safe_zones": [],
            "forbidden_overlay_zones": [],
            "asset_fit": {"background_ui": "cover"},
            "responsive_rules": ["Keep controls visible."],
        },
        "asset_manifest": {
            "assets": [{"id": "background_ui"}],
            "generation_records": [],
            "implementation_notes": ["Copy production assets."],
        },
    }
