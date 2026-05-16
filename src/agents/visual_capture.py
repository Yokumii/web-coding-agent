from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agents.sdk_runner import AgentRunStats
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.utils.logger import get_logger

logger = get_logger(__name__)


def build_visual_capture_requirements(*, round_num: int, app_url: str) -> list[str]:
    return [
        "Phase C: Deferred Visual Review Capture",
        f"- During this evaluator run, capture `.harness/visual_round_{round_num}_home.png` at the top of the page.",
        f"- If the page meaningfully scrolls, also capture `.harness/visual_round_{round_num}_mid.png` from a middle section.",
        f"- If the page meaningfully scrolls, also capture `.harness/visual_round_{round_num}_bottom.png` near the bottom section.",
        f"- Write `.harness/visual_manifest_round_{round_num}.json` with this schema:",
        json.dumps(
            {
                "round": round_num,
                "app_url": app_url,
                "screenshots": [
                    f".harness/visual_round_{round_num}_home.png",
                    f".harness/visual_round_{round_num}_mid.png",
                    f".harness/visual_round_{round_num}_bottom.png",
                ],
                "notes": "short paragraph describing what was captured",
            },
            indent=2,
        ),
        "- Only include screenshots that were actually created.",
        "- Use only relative paths such as `.harness/visual_round_1_home.png`.",
        "- Save screenshots via the browser screenshot tool filename argument.",
        "- Write the manifest with the Write tool only.",
        "- Keep the appearance verdict as a placeholder for the downstream VLM review; do not treat this capture step as the final visual score.",
    ]


async def run_visual_capture(
    config: HarnessConfig,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
    app_url: str,
) -> tuple[dict[str, Any], AgentRunStats]:
    del config, workdir, app_url
    logger.info(
        f"[bold magenta]Visual capture[/] round {round_num} collecting evaluator artifacts"
    )
    manifest = file_comm.read_visual_manifest(round_num) or _fallback_manifest(file_comm, round_num)
    logger.info(
        f"[bold magenta]Visual capture[/] round {round_num} found "
        f"{len(manifest.get('screenshots', []))} screenshot(s). Cost: $0.0000"
    )
    return manifest, AgentRunStats(
        cost_usd=0.0,
        duration_ms=None,
        duration_api_ms=None,
        token_usage={},
        usage={},
        model_usage={},
    )


def _fallback_manifest(file_comm: FileComm, round_num: int) -> dict[str, Any]:
    matches = sorted(file_comm.dir.glob(f"visual_round_{round_num}_*.png"))
    return {
        "round": round_num,
        "app_url": "",
        "screenshots": [f".harness/{path.name}" for path in matches],
        "notes": "",
    }
