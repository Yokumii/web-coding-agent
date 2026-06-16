from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.orchestration.file_comm import FileComm
from src.orchestration.schemas import AssetManifest, DesignBrief, LayoutContract


_DESIGN_ARTIFACT_REFS = [
    f".harness/{DesignBrief.filename()}",
    f".harness/{LayoutContract.filename()}",
    f".harness/{AssetManifest.filename()}",
]


@dataclass(frozen=True)
class DesignContractContext:
    """Read-only view over design-stage artifacts for downstream agents."""

    design_brief: dict[str, Any] | None
    layout_contract: dict[str, Any] | None
    asset_manifest: dict[str, Any] | None

    @classmethod
    def load(cls, file_comm: FileComm) -> "DesignContractContext":
        return cls(
            design_brief=file_comm.read_design_brief(),
            layout_contract=file_comm.read_layout_contract(),
            asset_manifest=file_comm.read_asset_manifest(),
        )

    @property
    def available(self) -> bool:
        return self.design_brief is not None

    def required_refs(self) -> list[str]:
        return list(_DESIGN_ARTIFACT_REFS) if self.available else []

    def generator_guidance(self) -> str:
        if self.design_brief is None:
            return ""

        visual_strategy = str(self.design_brief.get("visual_strategy", "")).strip()
        aesthetic_intent = self.design_brief.get("aesthetic_intent")
        lines = [
            "Design Stage Guidance:",
            "- Read the design-stage artifacts before making layout decisions.",
            "- Treat `layout_contract.json` as the source of semantic overlay regions, responsive behavior, and safe zones.",
            "- Treat `asset_manifest.json` as the source of which raster assets are production assets and where they should be copied.",
        ]
        if isinstance(aesthetic_intent, dict):
            hypothesis = str(aesthetic_intent.get("design_hypothesis", "")).strip()
            preserve = [
                str(item).strip()
                for item in aesthetic_intent.get("distinctive_features_to_preserve", []) or []
                if str(item).strip()
            ]
            avoid = [
                str(item).strip()
                for item in aesthetic_intent.get("generic_patterns_to_avoid", []) or []
                if str(item).strip()
            ]
            if hypothesis:
                lines.append(f"- Preserve the design hypothesis: {hypothesis}")
            if preserve:
                lines.append(
                    "- Preserve the authored visual traits declared in the design brief: "
                    + "; ".join(preserve)
                    + "."
                )
            if avoid:
                lines.append(
                    "- Do not collapse the implementation back into these generic patterns: "
                    + "; ".join(avoid)
                    + "."
                )
        if visual_strategy == "image_backed_ui":
            lines.extend(
                [
                    "- Preserve the approved composition using the design contract and asset manifest.",
                    "- Keep user-visible text and interactive controls as semantic HTML overlays.",
                    "- Copy required production assets from `.harness/design/` into the frontend project before referencing them in code.",
                    "- Use the image layer for composition, material, and texture; rebuild all functional labels, controls, and state in HTML.",
                ]
            )
        elif visual_strategy == "concept_reference_only":
            lines.extend(
                [
                    "- Use the approved concept as a visual reference only; do not embed it as production UI.",
                    "- Preserve its hierarchy and composition while rebuilding all user-visible text and controls as semantic HTML.",
                    "- Do not assume a text-free background asset exists unless `asset_manifest.json` declares one.",
                ]
            )
        elif visual_strategy == "text_only_fallback":
            lines.append(
                "- The design stage fell back to text-only; continue from the planning artifacts without assuming image assets exist."
            )
        else:
            lines.append("- Follow the visual strategy declared in `design_brief.json`.")
        return "\n".join(lines)

    def evaluator_assessment_lines(self) -> list[str]:
        if not self.available:
            return []
        return [
            "Design Contract Assessment:",
            "- Check whether the implementation follows `design_brief.json` visual_strategy and aesthetic_intent.",
            "- Check whether semantic text and controls are placed according to `layout_contract.json` overlay regions and safe zones.",
            "- Check whether required production assets in `asset_manifest.json` are copied into the frontend and used as decorative visual layers, not as inaccessible functional UI.",
            "",
        ]

    def vision_payload(self) -> dict[str, Any] | None:
        if self.design_brief is None:
            return None
        return {
            "visual_strategy": self.design_brief.get("visual_strategy"),
            "aesthetic_intent": self.design_brief.get("aesthetic_intent"),
            "reference_files": self.design_brief.get("reference_files"),
            "layout_contract": self.layout_contract or {},
            "asset_manifest": self.asset_manifest or {},
        }
