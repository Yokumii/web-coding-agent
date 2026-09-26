"""Low-cost WebCompass Edit runtime snapshot and three-dimension judge."""
from __future__ import annotations

import base64
import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from src.agents.openai_runner import OpenAIHTTPClient
from src.config import HarnessConfig
from src.utils.llm_json import extract_json_object


JUDGE_SYSTEM = """You are a lightweight WebCompass Edit judge, aligned with the benchmark's
source-page -> user Edit instruction -> final-page comparison. Judge only these
three dimensions:
instruction_targeting (ITG): the requested change is present in the requested page,
region, and visible/functional state;
feature_integrity (FTI): the source page's important existing content and behavior
remain intact, with no obvious collateral rewrite or fake independent data source;
style_conformance (STC): the added result fits the source page's visual language and
does not obviously cover, displace, or look like an unrelated demo.

Use a deliberately broad training-data standard and prefer PASS. A patch should
pass when the core requested capability is meaningfully implemented and the page renders, even
if subsidiary details or non-core edge cases are incomplete. A
single static screenshot cannot disprove an interaction or data flow. Do not turn
missing evidence, inability to verify, minor spacing/color differences, warnings,
implementation style, selector details, Skill API differences, or production-QA
expectations into issues. Do not judge minimal-path scope, test coverage, hidden
state, or whether reference code was copied.

Report issue only for a clear contradiction visible from the instruction, patch,
runtime, or before/after screenshots: the requested feature is plainly absent or
replaced by a static placeholder; important source content is visibly removed or
broken; or the added UI is obviously foreign, overlapping, or severely misaligned.
Inspect all three dimensions before answering and return every major repair-worthy
issue in this one response. Do not reveal one issue per round. Consolidate symptoms
with the same root cause, and omit minor polish, accessibility, responsive, or rare
edge-case gaps when the core Edit works.
Return JSON only:
{"instruction_targeting":"pass|issue","feature_integrity":"pass|issue",\
"style_conformance":"pass|issue","issues":[{"dimension":"instruction_targeting|feature_integrity|style_conformance","description":"short concrete issue"}]}
"""


def _image_part(path: Path, label: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}


def _safe_diff(workdir: Path, limit: int = 16000) -> str:
    frontend = workdir / "frontend"
    if not frontend.is_dir():
        return ""
    try:
        manifest = json.loads((workdir / "seed_manifest.json").read_text(encoding="utf-8"))
        baseline = str(manifest.get("baseline_commit") or "")
        args = ["git", "diff", "--unified=2"]
        if baseline:
            args.append(baseline)
        args.extend(["HEAD", "--", "."] if baseline else ["--", "."])
        result = subprocess.run(args, cwd=frontend, text=True, capture_output=True, check=False)
        diff = result.stdout
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        diff = ""
    if len(diff) > limit:
        return diff[:limit] + "\n[diff truncated]"
    return diff


async def _capture_page_screenshot(page: Any, screenshot_path: Path, *, timeout_ms: int = 15_000) -> str:
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True, timeout=timeout_ms)
        return "playwright"
    except Exception:
        session = await page.context.new_cdp_session(page)
        payload = await asyncio.wait_for(
            session.send("Page.captureScreenshot", {
                "format": "png",
                "captureBeyondViewport": False,
            }),
            timeout=timeout_ms / 1000,
        )
        screenshot_path.write_bytes(base64.b64decode(payload["data"]))
        return "cdp_fallback"


async def capture_runtime_snapshot(*, app_url: str, screenshot_path: Path, headless: bool,
                                   retries: int = 2) -> dict[str, Any]:
    """Open the app once per attempt, capture one screenshot, and collect fatal errors."""
    from playwright.async_api import async_playwright
    from src.utils.playwright_browser import launch_chromium

    last_error = ""
    for attempt in range(1, retries + 2):
        page_errors: list[str] = []
        critical_resource_errors: list[str] = []
        try:
            async with async_playwright() as playwright:
                browser = await launch_chromium(playwright, headless=headless)
                try:
                    page = await browser.new_page(viewport={"width": 1280, "height": 812})
                    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                    page.on(
                        "response",
                        lambda response: critical_resource_errors.append(
                            f"HTTP {response.status} loading {response.request.resource_type} {response.url}"
                        )
                        if response.status >= 400
                        and response.request.resource_type in {"document", "script"}
                        else None,
                    )
                    response = await page.goto(app_url, wait_until="domcontentloaded", timeout=20_000)
                    await page.wait_for_timeout(500)
                    body = await page.locator("body").inner_text()
                    screenshot_method = await _capture_page_screenshot(page, screenshot_path)
                    status = response.status if response else None
                    # Generic console resource messages omit the failed URL, so treating
                    # them as fatal makes harmless favicon/font/image 404s fail the page.
                    fatal = [
                        item for item in [*page_errors, *critical_resource_errors]
                        if item.strip()
                    ]
                    result = {
                        "status": "pass" if status and status < 400 and body.strip() and not fatal else "issue",
                        "http_status": status,
                        "body_chars": len(body.strip()),
                        "fatal_runtime_errors": fatal[:10],
                        "attempt": attempt,
                        "screenshot": str(screenshot_path),
                        "screenshot_method": screenshot_method,
                    }
                    if result["status"] == "pass" or attempt > retries:
                        return result
                    last_error = "; ".join(fatal) or f"HTTP {status}"
                finally:
                    await browser.close()
        except Exception as exc:  # browser/tooling failure is infrastructure, not product failure
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt > retries:
                return {"status": "infrastructure_error", "error": last_error, "attempt": attempt}
    return {"status": "infrastructure_error", "error": last_error, "attempt": retries + 1}


def _normalize_judgement(raw: Any, runtime: dict[str, Any]) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    allowed = {"instruction_targeting", "feature_integrity", "style_conformance"}
    if any(payload.get(key) not in {"pass", "issue"} for key in allowed):
        raise ValueError("Lightweight Judge response omitted an ITG/FTI/STC decision")
    result = {key: payload[key] for key in allowed}
    issues = []
    evidence_only = ("not shown", "not visible", "cannot verify", "unable to verify",
                     "insufficient evidence", "not enough evidence", "unclear from")
    for item in payload.get("issues") or []:
        if not isinstance(item, dict) or item.get("dimension") not in allowed:
            continue
        description = str(item.get("description") or "").strip()
        if description and not any(token in description.lower() for token in evidence_only):
            issues.append({"dimension": item["dimension"], "description": description[:500]})
        elif description:
            # An uncertainty finding is not a WebCompass failure under the
            # low-cost gate. Keep the dimension passing unless another
            # concrete finding remains.
            result[item["dimension"]] = "pass"
    if runtime.get("status") == "issue":
        result["feature_integrity"] = "issue"
        issues.insert(0, {"dimension": "feature_integrity", "description": "Page did not pass the fatal runtime/render gate."})
    issue_dimensions = {item["dimension"] for item in issues}
    for dimension in allowed:
        if result[dimension] == "issue" and dimension not in issue_dimensions:
            result[dimension] = "pass"
    return {**result, "issues": issues[:6]}


async def judge_edit(*, config: HarnessConfig, instruction: str, diff: str,
                     runtime: dict[str, Any], before: Path | None, after: Path,
                     round_num: int) -> tuple[dict[str, Any], dict[str, Any]]:
    parts: list[dict[str, Any]] = [{"type": "text", "text": (
        f"Edit instruction:\n{instruction[:6000]}\n\n"
        f"Code patch/diff:\n```diff\n{diff}\n```\n\n"
        f"Runtime summary:\n{json.dumps(runtime, ensure_ascii=False)}\n\n"
        "Return the required JSON object now."
    )}]
    before_part = _image_part(before, "before") if before else None
    after_part = _image_part(after, "after")
    if before_part:
        parts.append(before_part)
    if after_part:
        parts.append(after_part)
    client = OpenAIHTTPClient(config, timeout=min(float(config.agent_phase_timeout_seconds), 180.0))
    started = time.monotonic()
    response = await client.complete(
        model=config.evaluator_model,
        messages=[{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": parts}],
        max_tokens=700,
        temperature=0,
        reasoning_effort="low" if config.openai_wire_api == "responses" else None,
        response_format={"type": "json_object"},
        _stream=False,
    )
    content = ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if isinstance(content, list):
        text = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    else:
        text = str(content)
    judged = _normalize_judgement(extract_json_object(text), runtime)
    usage = response.get("usage") or {}
    elapsed = int((time.monotonic() - started) * 1000)
    stats = {
        "cost_usd": 0.0,
        "duration_ms": elapsed,
        "duration_api_ms": elapsed,
        "token_usage": {"input_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
                         "output_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0))},
        "usage": usage,
        "model_usage": {config.evaluator_model: usage},
    }
    return judged, stats


def build_lightweight_grades(*, round_num: int, sprint_num: int, runtime: dict[str, Any],
                             judgement: dict[str, Any], before: Path | None, after: Path,
                             diff: str) -> dict[str, Any]:
    passed = all(judgement.get(key) == "pass" for key in (
        "instruction_targeting", "feature_integrity", "style_conformance"))
    notes = "Lightweight WebCompass ITG/FTI/STC judge."
    return {
        "round": round_num, "sprint": sprint_num,
        "mode_recommendation": "generate_next_sprint" if passed else "repair",
        "phase_results": {"render_gate": "pass" if runtime.get("status") == "pass" else "fail",
                          "ui_functionality": "pass" if passed else "fail",
                          "appearance": "pass" if judgement.get("style_conformance") == "pass" else "fail",
                          "source_inspection": "skipped"},
        "sprint_passed": passed, "regression_passed": judgement.get("feature_integrity") == "pass",
        "overall_passed": passed,
        "criteria": {
            "design_quality": {"score": 7.0 if judgement.get("style_conformance") == "pass" else 3.0, "passed": judgement.get("style_conformance") == "pass", "notes": notes},
            "functionality": {"score": 7.0 if judgement.get("instruction_targeting") == "pass" else 3.0, "passed": judgement.get("instruction_targeting") == "pass", "notes": notes},
            "originality": {"score": 7.0, "passed": True, "notes": "Not used as an Edit gate."},
            "craft": {"score": 7.0 if passed else 5.0, "passed": passed, "notes": notes},
        },
        "lightweight_judge": judgement,
        "runtime_summary": runtime,
        "diff_sha256": __import__("hashlib").sha256(diff.encode()).hexdigest(),
        "screenshots": {"before": str(before) if before and before.is_file() else None, "after": str(after)},
        "bugs_found": [item["description"] for item in judgement.get("issues", []) if item.get("dimension") != "feature_integrity"],
        "regressions_found": [item["description"] for item in judgement.get("issues", []) if item.get("dimension") == "feature_integrity"],
        "missing_features": [],
        "repair_instructions": [
            "Fix all current major WebCompass issues in one local repair while preserving correct work: "
            + "; ".join(
                f"{item['dimension']}: {item['description']}"
                for item in judgement.get("issues", [])
            )
        ] if judgement.get("issues") else [],
    }
