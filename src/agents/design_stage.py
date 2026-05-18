from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agents.image_generation import generate_image
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class DesignStageResult:
    metadata: dict[str, Any]


def _design_artifact_path(filename: str) -> str:
    return f".harness/design/{filename}"


def _build_design_brief(
    *,
    requested_mode: str,
    visual_strategy: str,
    reference_files: dict[str, str],
    aesthetic_intent: dict[str, Any],
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "requested_mode": requested_mode,
        "visual_strategy": visual_strategy,
        "reference_files": reference_files,
        "aesthetic_intent": aesthetic_intent,
        "responsive_strategy": {
            "desktop": "Use the design contract as the primary composition reference.",
            "mobile": "Preserve hierarchy while adapting to a single-column layout.",
        },
        "overlay_regions": [],
        "implementation_rules": [
            "Keep user-visible text in HTML, not baked into raster assets.",
            "Keep interactive controls as semantic HTML elements.",
            "Use any background image as a visual layer, not as a replacement for functional UI.",
        ],
    }
    if fallback_reason is not None:
        payload["fallback_reason"] = fallback_reason
    return payload


def _build_layout_contract(*, image_backed: bool) -> dict[str, Any]:
    return {
        "viewport_targets": ["1440x900", "390x844"],
        "regions": [],
        "safe_zones": [],
        "forbidden_overlay_zones": [],
        "asset_fit": (
            {"background_ui": "cover_desktop_contain_mobile"} if image_backed else {}
        ),
    }


def _build_asset_manifest(
    *,
    approved_concept_exists: bool,
    background_ui_usable: bool,
) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    if approved_concept_exists:
        assets.append(
            {
                "id": "approved_concept",
                "path": _design_artifact_path("approved_concept.png"),
                "usage": "visual_reference",
                "required": False,
            }
        )
    if background_ui_usable:
        assets.append(
            {
                "id": "background_ui",
                "path": _design_artifact_path("background_ui.png"),
                "usage": "full_bleed_background",
                "required": True,
            }
        )
    return {"assets": assets}


def _build_aesthetic_intent(file_comm: FileComm) -> dict[str, Any]:
    design_tokens = file_comm.read_design_tokens() or {}
    visual_experiment = design_tokens.get("visual_experiment")
    if isinstance(visual_experiment, dict):
        return {
            "design_hypothesis": str(visual_experiment.get("design_hypothesis", "")).strip(),
            "reason_for_image_first": str(
                visual_experiment.get("reason_for_image_first", "")
            ).strip(),
            "distinctive_features_to_preserve": list(
                visual_experiment.get("desired_break_from_web_templates", []) or []
            ),
            "non_css_visual_value": list(
                visual_experiment.get("visual_opportunities_beyond_css", []) or []
            ),
            "generic_patterns_to_avoid": list(
                visual_experiment.get("forbidden_generic_patterns", []) or []
            ),
        }
    return {
        "design_hypothesis": (
            "Use image-first generation to expand the visual space beyond "
            "what a code-only frontend agent would usually invent from text."
        ),
        "reason_for_image_first": (
            "Text-only prompting often converges on safe, generic web layouts."
        ),
        "distinctive_features_to_preserve": [],
        "non_css_visual_value": [],
        "generic_patterns_to_avoid": [
            "generic SaaS hero composition",
            "centered card grid",
            "glassmorphism defaults",
        ],
    }


def _build_concept_prompt(file_comm: FileComm) -> str:
    spec = file_comm.read_spec().strip()
    design_tokens = file_comm.read_design_tokens() or {}
    aesthetic_intent = _build_aesthetic_intent(file_comm)
    return (
        "Create a polished overall frontend concept image for the product below. "
        "The purpose of this image-first stage is not to restate a conventional "
        "web UI, but to explore a visually distinctive direction that a code-only "
        "frontend agent would be unlikely to invent from text alone. "
        "Show the actual application interface, not marketing art. "
        "Use the product specification, design tokens, and aesthetic intent as "
        "the source of truth. "
        "Avoid default AI-web conventions when they conflict with the aesthetic "
        "intent, including centered card grids, generic SaaS heroes, glassmorphism, "
        "soft gradient wallpaper, and stock landing-page composition. "
        "Prefer authored composition, memorable hierarchy, image-led or "
        "material-led structure, and visual ideas that justify the use of image "
        "generation while still leaving semantic room for later HTML overlays. "
        "The concept may contain representative text at this stage.\n\n"
        f"SPEC:\n{spec[:7000]}\n\n"
        f"DESIGN TOKENS:\n{design_tokens}\n\n"
        f"AESTHETIC INTENT:\n{aesthetic_intent}"
    )


def _build_background_prompt() -> str:
    return (
        "Transform the provided approved frontend concept into a text-free "
        "background UI asset for implementation. Preserve the composition, "
        "imagery, decorative structure, and major container geometry. Remove "
        "all body text, labels, button text, and any baked-in typography that "
        "must remain editable or accessible in HTML. Keep the result suitable "
        "for semantic DOM overlays."
    )


async def _try_generate_missing_assets(
    *,
    config: HarnessConfig,
    file_comm: FileComm,
    approved_concept: Path,
    background_ui: Path,
) -> None:
    if not config.design_image_api_key:
        return

    if not approved_concept.exists():
        try:
            await generate_image(
                config=config,
                prompt=_build_concept_prompt(file_comm),
                output_path=approved_concept,
                reference_images=None,
            )
            logger.info("[bold magenta]DESIGN phase[/] generated approved concept image.")
        except Exception as exc:
            logger.warning(
                "[bold yellow]DESIGN phase[/] concept generation failed; "
                f"continuing with fallback policy: {exc}"
            )

    if approved_concept.exists() and not background_ui.exists():
        try:
            await generate_image(
                config=config,
                prompt=_build_background_prompt(),
                output_path=background_ui,
                reference_images=[approved_concept],
            )
            logger.info("[bold magenta]DESIGN phase[/] generated text-free background image.")
        except Exception as exc:
            logger.warning(
                "[bold yellow]DESIGN phase[/] background generation failed; "
                f"continuing with fallback policy: {exc}"
            )


async def run_design_stage(
    config: HarnessConfig,
    file_comm: FileComm,
    workdir: Path,
) -> DesignStageResult:
    """Create design assets and the implementation contract for image-first runs."""
    del workdir  # reserved for the later image-generation implementation

    file_comm.design_dir.mkdir(parents=True, exist_ok=True)
    approved_concept = file_comm.design_dir / "approved_concept.png"
    background_ui = file_comm.design_dir / "background_ui.png"
    await _try_generate_missing_assets(
        config=config,
        file_comm=file_comm,
        approved_concept=approved_concept,
        background_ui=background_ui,
    )
    has_approved_concept = approved_concept.exists()
    has_background_ui = background_ui.exists()
    image_backed = has_approved_concept and has_background_ui
    aesthetic_intent = _build_aesthetic_intent(file_comm)

    if image_backed:
        visual_strategy = "image_backed_ui"
        design_mode = "image_backed_ui"
        design_status = "accepted"
        reference_files = {
            "approved_concept": _design_artifact_path("approved_concept.png"),
            "background_ui": _design_artifact_path("background_ui.png"),
        }
        fallback_reason = None
        logger.info("[bold magenta]DESIGN phase[/] adopted pre-seeded image assets.")
    elif has_approved_concept:
        visual_strategy = "concept_reference_only"
        design_mode = "concept_reference_only"
        design_status = "partial_reference_only"
        reference_files = {
            "approved_concept": _design_artifact_path("approved_concept.png"),
        }
        fallback_reason = "background_ui_unavailable"
        logger.info(
            "[bold magenta]DESIGN phase[/] adopted approved concept as a "
            "reference-only asset because no text-free background UI was found."
        )
    else:
        visual_strategy = "text_only_fallback"
        design_mode = "text_only_fallback"
        design_status = "fallback_text_only"
        reference_files = {}
        fallback_reason = (
            "approved_concept_unavailable" if has_background_ui else "image_assets_unavailable"
        )
        logger.info(
            "[bold magenta]DESIGN phase[/] no pre-seeded image assets found; "
            "recorded text-only fallback."
        )

    file_comm.write_design_brief(
        _build_design_brief(
            requested_mode=config.design_mode,
            visual_strategy=visual_strategy,
            reference_files=reference_files,
            aesthetic_intent=aesthetic_intent,
            fallback_reason=fallback_reason,
        )
    )
    file_comm.write_layout_contract(_build_layout_contract(image_backed=image_backed))
    file_comm.write_asset_manifest(
        _build_asset_manifest(
            approved_concept_exists=has_approved_concept,
            background_ui_usable=image_backed,
        )
    )

    metadata = {
        "requested_design_mode": config.design_mode,
        "design_mode": design_mode,
        "design_status": design_status,
        "approved_concept_path": (
            _design_artifact_path("approved_concept.png") if has_approved_concept else None
        ),
        "background_ui_path": (
            _design_artifact_path("background_ui.png") if image_backed else None
        ),
    }
    return DesignStageResult(metadata=metadata)
