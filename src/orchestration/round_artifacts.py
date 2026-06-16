from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.orchestration.file_comm import FileComm
from src.orchestration.schemas import Grades, VisualManifest


@dataclass(frozen=True)
class RoundArtifacts:
    """Centralized names and discovery rules for one harness round."""

    file_comm: FileComm
    round_num: int

    @property
    def feedback_name(self) -> str:
        return f"feedback_round_{self.round_num}.md"

    @property
    def grade_name(self) -> str:
        return Grades.filename(round_num=self.round_num)

    @property
    def visual_manifest_name(self) -> str:
        return VisualManifest.filename(round_num=self.round_num)

    @property
    def feedback_path(self) -> Path:
        return self.file_comm.dir / self.feedback_name

    @property
    def grade_path(self) -> Path:
        return self.file_comm.dir / self.grade_name

    @property
    def visual_manifest_path(self) -> Path:
        return self.file_comm.dir / self.visual_manifest_name

    @property
    def feedback_ref(self) -> str:
        return self._harness_ref(self.feedback_name)

    @property
    def grade_ref(self) -> str:
        return self._harness_ref(self.grade_name)

    @property
    def visual_manifest_ref(self) -> str:
        return self._harness_ref(self.visual_manifest_name)

    @property
    def visual_capture_refs(self) -> list[str]:
        return [
            self._harness_ref(f"visual_round_{self.round_num}_{position}.png")
            for position in ("home", "mid", "bottom")
        ]

    def previous_existing_refs(self) -> list[str]:
        previous_round = self.round_num - 1
        if previous_round < 1:
            return []

        previous = RoundArtifacts(self.file_comm, previous_round)
        refs: list[str] = []
        if previous.feedback_path.exists():
            refs.append(previous.feedback_ref)
        if previous.grade_path.exists():
            refs.append(previous.grade_ref)
        return refs

    def trace_path(self, agent_name: str) -> Path:
        return self.file_comm.dir / "traces" / f"{agent_name}_round_{self.round_num}.jsonl"

    def visual_screenshot_refs(
        self,
        *,
        manifest: dict[str, Any] | None,
        grades: dict[str, Any] | None = None,
    ) -> list[str]:
        manifest_refs = self._normalized_screenshot_refs(manifest)
        if manifest_refs:
            return manifest_refs

        if isinstance(grades, dict):
            appearance_review = grades.get("appearance_review")
            grade_refs = self._normalized_screenshot_refs(appearance_review)
            if grade_refs:
                return grade_refs

        matches = sorted(self.file_comm.dir.glob(f"visual_round_{self.round_num}_*.png"))
        return [self._harness_ref(path.name) for path in matches]

    @staticmethod
    def _harness_ref(name: str) -> str:
        return f".harness/{name}"

    @staticmethod
    def _normalized_screenshot_refs(payload: dict[str, Any] | None) -> list[str]:
        if not isinstance(payload, dict):
            return []
        screenshots = payload.get("screenshots")
        if not isinstance(screenshots, list):
            return []
        return [str(item).strip() for item in screenshots if str(item).strip()]
