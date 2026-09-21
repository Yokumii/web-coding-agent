#!/usr/bin/env python3
"""Materialize verified Harness chains as WebCompass-protocol examples.

This exporter writes two deliberately separate views:

* benchmark-shaped JSONL and asset folders matching the official dataset; and
* ``model_io/*.jsonl`` audit rows showing the exact model-visible prompt and
  expected response.  The latter is not presented as an official dataset schema.

Generation/Edit examples come from accepted Harness chains.  Repair examples
must be supplied as existing official-taxonomy records; unrelated runtime or
business-logic failures are never relabeled as one of WebCompass's 11 defects.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import json
import shutil
import sys
import threading
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.export_trajectory_dataset import apply_patches, code_at_commit, make_patches
from src.orchestration.webcompass_protocol import (
    EDIT_TYPES,
    REPAIR_TYPES,
    markdown_repository,
    model_io_view,
    official_patches,
    parse_markdown_repository,
    search_replace_xml,
    validate_edit_repair_record,
    validate_generation_record,
)
from src.utils.playwright_browser import launch_chromium


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _successful_batch_row(path: Path, case_id: str) -> dict[str, Any]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    matches = [row for row in rows if row.get("id") == case_id and row.get("status") == "ok"]
    if not matches:
        raise ValueError(f"no successful batch row for {case_id!r} in {path}")
    row = matches[-1]
    if not isinstance(row.get("steps"), list) or not 4 <= len(row["steps"]) <= 12:
        raise ValueError(f"{case_id}: WebCompass compound Edit needs 4-12 accepted steps")
    if any(step.get("status") != "ok" for step in row["steps"]):
        raise ValueError(f"{case_id}: not every chain step is accepted")
    return row


def _task_descriptions(sequence: dict[str, Any], task_types: list[str]) -> list[dict[str, str]]:
    edits = sequence.get("edits")
    if not isinstance(edits, list) or len(edits) != len(task_types):
        raise ValueError("sequence edits and declared WebCompass task types must have equal length")
    unknown = sorted(set(task_types) - EDIT_TYPES)
    if unknown:
        raise ValueError(f"task types outside official WebCompass taxonomy: {unknown}")
    return [
        {"task_type": task_type, "description": str(edit["instruction"])}
        for task_type, edit in zip(task_types, edits, strict=True)
    ]


def _generation_document(sequence: dict[str, Any]) -> str:
    edits = sequence["edits"]
    lines = [
        "# Product goal",
        str(sequence["seed_summary"]),
        "",
        "# Existing verified behavior",
    ]
    lines.extend(f"- {item}" for item in sequence.get("existing_capabilities", []))
    lines.extend(["", "# Required incremental changes"])
    for index, edit in enumerate(edits, 1):
        lines.extend([f"## Task {index}", str(edit["instruction"])])
        acceptance = edit.get("acceptance") or []
        if acceptance:
            lines.append("Acceptance:")
            lines.extend(f"- {item}" for item in acceptance)
        preserve = edit.get("preserve") or []
        if preserve:
            lines.append("Preserve:")
            lines.extend(f"- {item}" for item in preserve)
    lines.extend([
        "",
        "# Delivery",
        "Return a complete runnable static web project implementing the product and every change above.",
    ])
    return "\n".join(lines)


def _generation_checklist(sequence: dict[str, Any]) -> list[dict[str, Any]]:
    edits = sequence["edits"]
    feature_points = 75 // len(edits)
    remainder = 75 - feature_points * len(edits)
    checklist: list[dict[str, Any]] = [{
        "task": "Does the page load correctly and run without errors?",
        "category": "Runnability",
        "operation_sequence": "1. Load the page 2. Check browser console 3. Check failed network requests",
        "expected_result": "The page renders with no blocking JavaScript error or missing local resource.",
        "criteria": "Full 10 points when runnable; blocking errors or a blank page receive 0.",
        "max_score": 10,
    }]
    for index, edit in enumerate(edits):
        acceptance = edit.get("acceptance") or [edit["instruction"]]
        checklist.append({
            "task": f"Verify accepted Edit task {index + 1}",
            "category": "Spec Implementation",
            "operation_sequence": " ".join(
                f"{step}. {text}" for step, text in enumerate(acceptance[:4], 1)
            ),
            "expected_result": " ".join(str(item) for item in acceptance),
            "criteria": "Full points only when the observable accepted behavior is preserved.",
            "max_score": feature_points + (1 if index < remainder else 0),
        })
    checklist.append({
        "task": "Verify visual coherence and responsive layout",
        "category": "Design Quality",
        "operation_sequence": "1. Inspect the main viewport 2. Resize to a narrow viewport 3. Check spacing and readability",
        "expected_result": "The accepted visual hierarchy remains coherent and usable at desktop and mobile widths.",
        "criteria": "Full 15 points for coherent layout, readable typography, and responsive behavior.",
        "max_score": 15,
    })
    if sum(int(item["max_score"]) for item in checklist) != 100:
        raise AssertionError("generated checklist must total 100")
    return checklist


def _safe_write_code(root: Path, code: list[dict[str, str]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    for item in code:
        path = (root / item["path"]).resolve()
        if resolved_root not in path.parents:
            raise ValueError(f"unsafe code path: {item['path']}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["code"], encoding="utf-8")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


async def _render_code(
    code: list[dict[str, str]], destination: Path, *, route: str = "/index.html",
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    with TemporaryDirectory(prefix="webcompass-example-") as temp:
        root = Path(temp)
        _safe_write_code(root, code)
        handler = functools.partial(_QuietHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        if not route.startswith("/") or route.startswith("//"):
            raise ValueError(f"unsafe capture route: {route!r}")
        url = f"http://127.0.0.1:{port}{route}"
        external_requests: list[str] = []
        console_errors: list[str] = []
        page_errors: list[str] = []
        failed_requests: list[dict[str, Any]] = []
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with async_playwright() as playwright:
                browser = await launch_chromium(playwright, headless=True)
                try:
                    page = await browser.new_page(viewport={"width": 1440, "height": 900})
                    page.on(
                        "request",
                        lambda request: external_requests.append(request.url)
                        if urllib.parse.urlsplit(request.url).hostname not in {"127.0.0.1", "localhost"}
                        else None,
                    )
                    page.on(
                        "console",
                        lambda message: console_errors.append(message.text)
                        if message.type == "error"
                        else None,
                    )
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.on(
                        "response",
                        lambda response: failed_requests.append(
                            {"url": response.url, "status": response.status}
                        )
                        if response.status >= 400
                        else None,
                    )
                    page.on(
                        "requestfailed",
                        lambda request: failed_requests.append(
                            {"url": request.url, "error": request.failure or "request failed"}
                        ),
                    )
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    await page.wait_for_timeout(1_000)
                    for action in actions or []:
                        kind = str(action.get("action") or "")
                        if kind == "click":
                            await page.click(str(action["selector"]))
                        elif kind == "wait":
                            await page.wait_for_timeout(int(action["ms"]))
                        else:
                            raise ValueError(f"unsupported capture action: {kind!r}")
                    await page.screenshot(path=str(destination), type="jpeg", quality=88, full_page=True)
                    title = await page.title()
                    body_text_chars = await page.locator("body").inner_text()
                finally:
                    await browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    return {
        "url": route,
        "title": title,
        "body_text_chars": len(body_text_chars),
        "console_errors": console_errors,
        "page_errors": page_errors,
        "failed_requests": failed_requests,
        "external_requests": sorted(set(external_requests)),
        "screenshot": str(destination),
    }


def _append_readme(code: list[dict[str, str]]) -> list[dict[str, str]]:
    if any(item["path"] == "README.md" for item in code):
        return code
    return [
        *code,
        {
            "path": "README.md",
            "code": "# Run locally\n\nFrom this directory, run `python -m http.server 8000`, then open `http://localhost:8000`.\n",
        },
    ]


def _sha(code: list[dict[str, str]]) -> str:
    payload = json.dumps(code, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _with_expected_output(view: dict[str, Any], output: str) -> dict[str, Any]:
    return {
        **view,
        "expected_output": output,
        "note": "Audit serialization only; official benchmark JSONL remains in the task directories.",
    }


async def _export_chain(
    *, config: dict[str, Any], base: Path, output: Path
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    case_id = str(config["id"])
    result_path = _resolve(base, str(config["result_jsonl"]))
    sequence_path = _resolve(base, str(config["sequence"]))
    result = _successful_batch_row(result_path, case_id)
    sequence = _read_json(sequence_path)
    task_types = [str(value) for value in config["task_types"]]
    descriptions = _task_descriptions(sequence, task_types)
    first_step, last_step = result["steps"][0], result["steps"][-1]
    source_commit = str(first_step["ground_truth"]["source_commit"])
    target_commit = str(last_step["ground_truth"]["target_commit"])
    source_code = code_at_commit(Path(first_step["workdir"]) / "frontend", source_commit)
    target_code = code_at_commit(Path(last_step["workdir"]) / "frontend", target_commit)
    native_patches = make_patches(source_code, target_code, "compound_edit")
    patches = official_patches(native_patches)
    if apply_patches(source_code, patches) != target_code:
        raise ValueError(f"{case_id}: direct compound patch does not replay to final target")

    source_capture = config.get("source_capture") or {
        "name": "screenshot_index.jpg", "route": "/index.html"
    }
    source_shot = (
        output / "editing" / "sp" / case_id / "src" / str(source_capture["name"])
    )
    target_captures = config.get("target_captures") or [{
        "name": "screenshot_index.jpg", "route": "/index.html"
    }]
    source_render = await _render_code(
        source_code,
        source_shot,
        route=str(source_capture.get("route") or "/index.html"),
        actions=list(source_capture.get("actions") or []),
    )
    target_renders: list[dict[str, Any]] = []
    image_shots: list[Path] = []
    for capture in target_captures:
        name = str(capture["name"])
        target_shot = output / "audit" / case_id / name
        target_renders.append(await _render_code(
            target_code,
            target_shot,
            route=str(capture.get("route") or "/index.html"),
            actions=list(capture.get("actions") or []),
        ))
        image_shot = output / "image" / case_id / "screenshots" / name
        image_shot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target_shot, image_shot)
        image_shots.append(image_shot)

    checklist = _generation_checklist(sequence)
    document = _generation_document(sequence)
    common_generation = {
        "repo": "harness/webcoding",
        "instance_id": case_id,
        "base_commit": source_commit,
        "problem_statement": checklist,
        "meta": {
            "class": str(config["generation_class"]),
            "difficulty": str(config["difficulty"]),
            "construction": "accepted-harness-edit-chain",
        },
        "working_dir": "/testbed",
    }
    text_generation = {**common_generation, "instruction": document}
    image_generation = dict(common_generation)
    validate_generation_record(text_generation, modality="text")
    validate_generation_record(image_generation, modality="image")

    edit_record = {
        "instance_id": case_id,
        "task": "edit",
        "task_type": task_types,
        "difficulty": str(config["difficulty"]),
        "description": descriptions,
        "src_code": source_code,
        "dst_code": target_code,
        "src_screenshot": [source_shot.name],
        "dst_screenshot": [],
        "label_modified_files": patches,
        "resources": [],
    }
    validate_edit_repair_record(edit_record)

    generation_answer = _append_readme(target_code)
    markdown_answer = markdown_repository(generation_answer)
    if parse_markdown_repository(markdown_answer) != sorted(
        generation_answer, key=lambda item: item["path"]
    ):
        raise ValueError(f"{case_id}: generation Markdown output is not reversible")
    edit_answer = search_replace_xml(patches)
    answer_root = output / "answers" / case_id
    _safe_write_code(answer_root, generation_answer)

    image_relatives = [str(path.relative_to(output)) for path in image_shots]
    source_relative = str(source_shot.relative_to(output))
    views = {
        "text-generation": [
            _with_expected_output(model_io_view(text_generation, task_name="text-generation"), markdown_answer)
        ],
        "image-generation": [
            _with_expected_output(
                model_io_view(image_generation, task_name="image-generation", image_paths=image_relatives),
                markdown_answer,
            )
        ],
        "text-editing": [
            _with_expected_output(model_io_view(edit_record, task_name="text-editing"), edit_answer)
        ],
        "image-editing": [
            _with_expected_output(
                model_io_view(edit_record, task_name="image-editing", image_paths=[source_relative]),
                edit_answer,
            )
        ],
    }
    audit = {
        "instance_id": case_id,
        "source_commit": source_commit,
        "target_commit": target_commit,
        "accepted_steps": len(result["steps"]),
        "patch_replay": "exact",
        "patch_count": len(patches),
        "source_code_sha256": _sha(source_code),
        "target_code_sha256": _sha(target_code),
        "task_type_assignment": "explicit_case_manifest",
        "source_render": source_render,
        "target_renders": target_renders,
        "known_visual_issues": list(config.get("known_visual_issues") or []),
        "existing_harness_evidence": [
            str(Path(step["workdir"]) / ".harness" / "accepted_tapes.jsonl")
            for step in result["steps"]
        ],
    }
    return {
        "text_generation": text_generation,
        "image_generation": image_generation,
        "edit": edit_record,
        "audit": audit,
    }, views


def _first_remote_jsonl_row(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.readline())


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=60) as response:
        destination.write_bytes(response.read())


def _export_reference_repair(
    *, config: dict[str, Any], output: Path
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    record = _first_remote_jsonl_row(str(config["data_url"]))
    validate_edit_repair_record(record)
    if record["task"] != "repair" or not record["dst_code"] or not record["label_modified_files"]:
        raise ValueError("reference Repair must contain public target code and exact labels")
    if apply_patches(record["src_code"], record["label_modified_files"]) != record["dst_code"]:
        raise ValueError("official reference Repair label does not replay exactly")
    instance_id = record["instance_id"]
    base_url = str(config["assets_base_url"]).rstrip("/") + "/" + urllib.parse.quote(instance_id)
    source_images: list[str] = []
    target_images: list[str] = []
    for name in record["src_screenshot"]:
        destination = output / "repair" / "sp" / instance_id / "src" / name
        _download(f"{base_url}/src/{urllib.parse.quote(name)}", destination)
        source_images.append(str(destination.relative_to(output)))
    for name in record["dst_screenshot"]:
        destination = output / "repair" / "sp" / instance_id / "dst" / name
        _download(f"{base_url}/dst/{urllib.parse.quote(name)}", destination)
        target_images.append(str(destination.relative_to(output)))
    response = search_replace_xml(record["label_modified_files"])
    text_view = _with_expected_output(
        model_io_view(record, task_name="diagnostic-repair"), response
    )
    visual_view = _with_expected_output(
        model_io_view(
            record,
            task_name="visual-diagnostic-repair",
            image_paths=source_images,
            target_image_paths=target_images,
        ),
        response,
    )
    _safe_write_code(output / "answers" / instance_id, record["dst_code"])
    return record, {
        "diagnostic-repair": [text_view],
        "visual-diagnostic-repair": [visual_view],
    }


async def export_examples(manifest_path: Path, output: Path) -> dict[str, Any]:
    manifest = _read_json(manifest_path)
    base = manifest_path.parent
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty output: {output}")
    output.mkdir(parents=True, exist_ok=True)
    chain_results = []
    views: dict[str, list[dict[str, Any]]] = {
        name: []
        for name in (
            "text-generation", "image-generation", "text-editing", "image-editing",
            "diagnostic-repair", "visual-diagnostic-repair",
        )
    }
    for config in manifest.get("chains", []):
        result, case_views = await _export_chain(config=config, base=base, output=output)
        chain_results.append(result)
        for name, rows in case_views.items():
            views[name].extend(rows)

    repair_record, repair_views = _export_reference_repair(
        config=manifest["reference_repair"], output=output
    )
    for name, rows in repair_views.items():
        views[name].extend(rows)

    _write_jsonl(
        output / "text" / "generation" / "data.jsonl",
        [item["text_generation"] for item in chain_results],
    )
    _write_jsonl(
        output / "image" / "generation" / "data.jsonl",
        [item["image_generation"] for item in chain_results],
    )
    _write_jsonl(output / "editing" / "sp" / "data.jsonl", [item["edit"] for item in chain_results])
    _write_jsonl(output / "repair" / "sp" / "data.jsonl", [repair_record])
    for name, rows in views.items():
        _write_jsonl(output / "model_io" / f"{name}.jsonl", rows)
    _write_json(output / "audit" / "chains.json", [item["audit"] for item in chain_results])
    known_visual = [
        f"{item['audit']['instance_id']}: {issue}"
        for item in chain_results
        for issue in item["audit"].get("known_visual_issues", [])
    ]
    external_cases = [
        item["audit"]["instance_id"]
        for item in chain_results
        if item["audit"]["source_render"].get("external_requests")
        or any(
            render.get("external_requests")
            for render in item["audit"].get("target_renders", [])
        )
    ]
    summary = {
        "schema": "WebCompass public JSONL layout plus non-official model_io audit views",
        "chain_cases": len(chain_results),
        "official_reference_repair_cases": 1,
        "task_view_counts": {name: len(rows) for name, rows in views.items()},
        "release_ready": False,
        "release_blockers": [
            "Explicit Edit taxonomy assignments require independent semantic review.",
            "The Repair view is an official reference protocol case, not a Harness-derived natural Repair.",
            *(
                ["External requests must be localized before release: " + ", ".join(external_cases)]
                if external_cases else []
            ),
            *known_visual,
        ],
    }
    _write_json(output / "SUMMARY.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = asyncio.run(export_examples(args.manifest.resolve(), args.output.resolve()))
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
