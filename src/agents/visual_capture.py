from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agents.sdk_runner import AgentRunStats, build_agent_run_stats, run_sdk_agent
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.prompts.visual_capture import VISUAL_CAPTURE_SYSTEM_PROMPT
from src.utils.logger import get_logger

logger = get_logger(__name__)


async def run_visual_capture(
    config: HarnessConfig,
    file_comm: FileComm,
    workdir: Path,
    round_num: int,
    app_url: str,
) -> tuple[dict[str, Any], AgentRunStats]:
    logger.info(
        f"[bold magenta]Visual capture[/] round {round_num} starting at {app_url}"
    )
    prompt = _build_visual_capture_prompt(
        round_num=round_num,
        app_url=app_url,
        workdir=workdir,
    )
    response, total_cost, _assistant_text, permission_denials = await run_sdk_agent(
        prompt=prompt,
        config=config,
        workdir=workdir,
        model=config.evaluator_model,
        system_prompt=VISUAL_CAPTURE_SYSTEM_PROMPT,
        max_turns=40,
        allow_bash=False,
        allow_playwright=True,
        trace_path=file_comm.dir / "traces" / f"visual_capture_round_{round_num}.jsonl",
    )
    if permission_denials:
        logger.warning(
            f"[bold yellow]Visual capture[/] completed with permission denials: {permission_denials}"
        )

    manifest = file_comm.read_visual_manifest(round_num) or _fallback_manifest(file_comm, round_num)
    logger.info(
        f"[bold magenta]Visual capture[/] round {round_num} captured "
        f"{len(manifest.get('screenshots', []))} screenshot(s). Cost: ${total_cost:.4f}"
    )
    return manifest, build_agent_run_stats(response, model=config.evaluator_model)


def _build_visual_capture_prompt(*, round_num: int, app_url: str, workdir: Path) -> str:
    lines = [
        f"Application URL: {app_url}",
        f"Round: {round_num}",
        "",
        "Goal:",
        "- Capture representative screenshots for external visual review.",
        "",
        "Required Steps:",
        "1. Open the application URL.",
        "2. Wait until the page is stable enough for screenshots.",
        f"3. Capture `.harness/visual_round_{round_num}_home.png` at the top of the page.",
        f"4. If the page meaningfully scrolls, capture `.harness/visual_round_{round_num}_mid.png` from a middle section.",
        f"5. If the page meaningfully scrolls, capture `.harness/visual_round_{round_num}_bottom.png` near the bottom section.",
        f"6. Write `.harness/visual_manifest_round_{round_num}.json` with this schema:",
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
        "",
        "Rules:",
        "- `.harness` already exists. Do not create directories.",
        "- Do not call Bash.",
        "- Only include screenshots that were actually created.",
        "- Use only relative paths such as `.harness/visual_round_1_home.png`.",
        "- Save screenshots via the browser screenshot tool filename argument.",
        "- Write the manifest with the Write tool only.",
        "- Do not write grades or evaluation results.",
        f"Workdir: {workdir}",
    ]
    return "\n".join(lines)


def _fallback_manifest(file_comm: FileComm, round_num: int) -> dict[str, Any]:
    matches = sorted(file_comm.dir.glob(f"visual_round_{round_num}_*.png"))
    return {
        "round": round_num,
        "app_url": "",
        "screenshots": [f".harness/{path.name}" for path in matches],
        "notes": "",
    }
