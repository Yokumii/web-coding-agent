from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agents.sdk_runner import AgentRunStats
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.round_artifacts import RoundArtifacts
from src.prompts.grading import apply_visual_review_scores, visual_review_failure
from src.utils.logger import get_logger

logger = get_logger(__name__)

VisualReviewer = Callable[..., Awaitable[tuple[dict[str, Any], AgentRunStats | None]]]
VisualNormalizer = Callable[[dict[str, Any], list[str]], dict[str, Any]]


@dataclass(frozen=True)
class VisualReviewRound:
    """Coordinates screenshot discovery, VLM review, and grade merging for one round."""

    config: HarnessConfig
    file_comm: FileComm
    workdir: Path
    round_num: int
    sprint_num: int
    sprint_context: dict[str, Any]

    @property
    def artifacts(self) -> RoundArtifacts:
        return RoundArtifacts(self.file_comm, self.round_num)

    def manifest(self) -> dict[str, Any] | None:
        manifest = self.file_comm.read_visual_manifest(self.round_num)
        if manifest:
            return manifest

        screenshot_refs = self.artifacts.visual_screenshot_refs(manifest=None)
        if not screenshot_refs:
            return None
        return {
            "round": self.round_num,
            "app_url": "",
            "screenshots": screenshot_refs,
            "notes": "",
        }

    async def apply(
        self,
        *,
        grades: dict[str, Any],
        manifest: dict[str, Any] | None = None,
        reviewer: VisualReviewer,
        normalizer: VisualNormalizer,
    ) -> tuple[dict[str, Any], AgentRunStats | None]:
        visual_manifest = manifest if manifest is not None else self.manifest()
        screenshot_paths = self.artifacts.visual_screenshot_refs(
            manifest=visual_manifest,
            grades=grades,
        )
        if not screenshot_paths:
            logger.warning(
                f"[bold yellow]Visual review[/] round {self.round_num} found no screenshots; "
                f"failing the appearance phase closed"
            )
            return visual_review_failure(grades, "no screenshots available"), None

        try:
            review, vision_stats = await reviewer(
                config=self.config,
                file_comm=self.file_comm,
                workdir=self.workdir,
                sprint_num=self.sprint_num,
                sprint_context=self.sprint_context,
                screenshot_paths=screenshot_paths,
            )
        except Exception as exc:
            logger.warning(
                f"[bold yellow]Visual review[/] round {self.round_num} failed; "
                f"failing the appearance phase closed: {exc}"
            )
            return visual_review_failure(
                grades, str(exc), infrastructure_failure=True
            ), None

        normalized = normalizer(review, screenshot_paths)
        return apply_visual_review_scores(grades, normalized), vision_stats
