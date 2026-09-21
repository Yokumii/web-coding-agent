#!/usr/bin/env python3
"""Mine browser-evidenced inspiration cards from URLs or local projects."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from inspiration_library.deep_browser_exploration import deep_explore_url, deep_explore_project
from inspiration_library.doc_api import DocApiClient
from inspiration_library.dynamic_capability_retrieval import (
    extract_seed_capabilities,
    merge_capability_pool,
    pool_snapshot_sha256,
    validate_capability_extraction,
    validate_live_card_evidence,
)
from inspiration_library.linear_edit_queries import load_json_response, read_jsonl
from inspiration_library.one_shot_capability_retrieval import compact_planner_card
from inspiration_library.production_browser import observe_url


def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def load_sources(path: Path, mode: str = "url") -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for row in rows:
        seed_id = str(row.get("seed_id") or "").strip()
        entry_url = str(row.get("entry_url") or "").strip()
        parsed = urlparse(entry_url)
        if not seed_id or seed_id in seen_ids:
            raise ValueError(f"invalid or duplicate seed_id: {seed_id!r}")
        project_path = str(row.get("project_path") or "").strip()
        if mode == "local_project":
            if entry_url or not project_path or not Path(project_path).is_absolute():
                raise ValueError("local_project mode requires absolute project_path and no entry_url")
            source_key = str(Path(project_path).resolve())
            source = {"seed_id":seed_id, "project_path":source_key}
        elif mode == "url":
            if project_path or parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError(f"url mode requires entry_url and no project_path: {entry_url}")
            source_key = entry_url
            source = {"seed_id":seed_id, "entry_url":entry_url}
        else:
            raise ValueError(f"unsupported mode: {mode}")
        if source_key in seen_urls:
            raise ValueError(f"duplicate entry_url: {entry_url}")
        seen_ids.add(seed_id)
        seen_urls.add(source_key)
        if "ready_selector" in row:
            if not isinstance(row["ready_selector"], str) or not row["ready_selector"].strip():
                raise ValueError("ready_selector must be a nonempty selector")
            source["ready_selector"] = row["ready_selector"]
        if "target_edit_types" in row:
            targets = row["target_edit_types"]
            if not isinstance(targets, list) or not all(isinstance(x, str) and x.strip() for x in targets):
                raise ValueError("target_edit_types must be an array of nonempty strings")
            source["target_edit_types"] = list(dict.fromkeys(targets))
        if "mining_focus" in row:
            if not isinstance(row["mining_focus"], str):
                raise ValueError("mining_focus must be a string")
            source["mining_focus"] = row["mining_focus"]
        if "expected_observations" in row:
            expected = row["expected_observations"]
            if not isinstance(expected, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in expected.items()):
                raise ValueError("expected_observations must map type names to observation questions")
            if not set(expected).issubset(source.get("target_edit_types", [])):
                raise ValueError("expected_observations must refer to target_edit_types")
            source["expected_observations"] = expected
        normalized.append(source)
    if not normalized:
        raise ValueError("URL source manifest is empty")
    return normalized


def compact_live_card(card: dict[str, Any]) -> dict[str, Any]:
    compact = compact_planner_card(card, source_slices=card.get("source_slices", []))
    return {
        **compact,
        "mode": "url",
        "source_seed_id": card["source_seed_id"],
        "source_kind": "live_url",
        "source_url": card["source_url"],
        "observation_evidence": card.get("observation_evidence", []),
        "evidence_reference_status": card.get("evidence_reference_status", "not_checked"),
        "source_capture_status": card.get("source_capture_status", "not_requested"),
        "source_reference_policy": card.get("source_reference_policy", {}),
        "component_artifacts": card.get("component_artifacts", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("url", "local_project"), default="url",
                        help="Input source type; both modes run on the physical host")
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--stage", choices=("scan", "full"), default="full")
    parser.add_argument("--exploration-rounds", type=int, default=1, choices=(0, 1, 2))
    parser.add_argument("--max-paths", type=int, default=10)
    parser.add_argument("--max-actions", type=int, default=6)
    parser.add_argument("--api-timeout-seconds", type=float, default=180.0)
    parser.add_argument("--seed-id", action="append", default=[])
    parser.add_argument("--browser-proxy", help="Browser-only HTTP/SOCKS proxy; API stays direct")
    parser.add_argument("--source-mode", choices=("reference", "examples", "none"), default="reference")
    parser.add_argument("--product-patterns", action="store_true",
                        help="Extract product purposes/workflows and classify their observed capabilities")
    parser.add_argument("--max-source-regions", type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()
    if args.mode == "local_project" and args.source_mode != "reference":
        parser.error("local_project uses project source slices; --source-mode applies to url mode")
    if args.browser_proxy:
        os.environ["WEBCODING_BROWSER_PROXY"] = args.browser_proxy

    sources = load_sources(args.sources, args.mode)
    if args.seed_id:
        requested = set(args.seed_id)
        available = {row["seed_id"] for row in sources}
        missing = requested - available
        if missing:
            raise ValueError(f"unknown seed-id values: {sorted(missing)}")
        sources = [row for row in sources if row["seed_id"] in requested]
    args.run_dir.mkdir(parents=True, exist_ok=True)
    identity = {"version": "inspiration-v7-card-first-closure", "mode":args.mode, "sources": sources,
                "model": os.environ.get("DOC_API_CHAT_MODEL", "qwen3.8-max"),
                "base_url": os.environ.get("DOC_API_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                "rounds": args.exploration_rounds, "max_paths": args.max_paths,
                "max_actions": args.max_actions, "browser_proxy": os.environ.get("WEBCODING_BROWSER_PROXY"),
                "source_mode": args.source_mode, "max_source_regions": args.max_source_regions}
    if args.product_patterns:
        identity["product_patterns"] = "v1"
    identity_path = args.run_dir / "run_identity.json"
    if identity_path.exists():
        if json.loads(identity_path.read_text()) != identity:
            raise ValueError("Run identity changed; use a fresh run directory")
    elif any(args.run_dir.iterdir()):
        raise ValueError("Existing run lacks current evidence identity; use a fresh run directory")
    write_json(identity_path, identity)
    write_json(
        args.run_dir / "input_manifest.json",
        {
            "schema_version": "webcoding-live-url-mining-input-v1",
            "source_count": len(sources),
            "mode": args.mode,
            "network_policy": "online_readonly" if args.mode == "url" else "local_only",
            "sources": sources,
        },
    )

    if args.stage == "scan":
        rows: list[dict[str, Any]] = []
        for index, source in enumerate(sources, 1):
            destination = args.run_dir / "browser_scan" / safe_id(source["seed_id"])
            try:
                if args.mode == "url":
                    observation = observe_url(source["entry_url"], destination, ready_selector=source.get("ready_selector"))
                else:
                    from inspiration_library.production_browser import observe_project
                    observation = observe_project(Path(source["project_path"]), destination)
                baseline = observation["baseline"]
                row = {
                    **source,
                    "status": observation["status"],
                    "status_scope": "browser_collection",
                    "title": baseline.get("title"),
                    "control_count": len(baseline.get("interactive", [])),
                    "visible_text_chars": len(str(baseline.get("visible_text") or "")),
                    "embedded_documents": baseline.get("embedded_documents", []),
                    "remote_request_count": len(observation.get("remote_requests", [])),
                    "console_error_count": len(observation.get("console_errors", [])),
                    "page_error_count": len(observation.get("page_errors", [])),
                }
            except Exception as exc:
                row = {
                    **source,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            rows.append(row)
            print(
                f"[scan {index}/{len(sources)}] {source['seed_id']} {row['status']}",
                flush=True,
            )
        write_jsonl(args.run_dir / "scan_results.jsonl", rows)
        write_json(
            args.run_dir / "scan_summary.json",
            {
                "source_count": len(rows),
                "status_scope": "browser_collection",
                "successful_count": sum(row["status"] == "ok" for row in rows),
                "failed_count": sum(row["status"] != "ok" for row in rows),
            },
        )
        return 0

    key = os.environ.get("DOC_API_KEY")
    if not key:
        raise ValueError("DOC_API_KEY is required for full mining")
    extractions: list[dict[str, Any]] = []
    product_patterns = []
    classifications = {}
    with DocApiClient(
        api_key=key,
        log_dir=args.run_dir / "provider",
        timeout_seconds=args.api_timeout_seconds,
        request_attempts=3,
    ) as client:
        for index, source in enumerate(sources, 1):
            seed_key = safe_id(source["seed_id"])
            exploration_args = dict(
                output_dir=args.run_dir / "browser_deep" / seed_key,
                client=client,
                request_prefix=f"live__{seed_key}",
                max_rounds=args.exploration_rounds,
                max_paths=args.max_paths,
                max_actions=args.max_actions,
            )
            if args.mode == "url":
                observation = deep_explore_url(entry_url=source["entry_url"],
                    ready_selector=source.get("ready_selector"),
                    exploration_targets={key:source[key] for key in
                        ("target_edit_types", "mining_focus", "expected_observations") if key in source},
                    **exploration_args)
            else:
                observation = deep_explore_project(project=Path(source["project_path"]), **exploration_args)
            extraction_path = args.run_dir / "extractions" / f"{seed_key}.json"
            response_path = (
                args.run_dir
                / "provider"
                / "responses"
                / f"live__{seed_key}__extract.txt"
            )
            if extraction_path.exists():
                extraction = json.loads(extraction_path.read_text(encoding="utf-8"))
                if args.mode == "url":
                    validate_live_card_evidence(extraction, observation)
            elif response_path.exists():
                extraction = validate_capability_extraction(
                    load_json_response(response_path),
                    seed_id=source["seed_id"],
                    source_url=source.get("entry_url"),
                    source_project=source.get("project_path"),
                    observation=observation if args.mode == "url" else None,
                )
                write_json(extraction_path, extraction)
            else:
                extraction = extract_seed_capabilities(
                    seed=source,
                    observation=observation,
                    client=client,
                    request_id=f"live__{seed_key}__extract",
                )
                write_json(extraction_path, extraction)
            def product_stage(item):
                from inspiration_library.product_sessions import extract_product_pattern
                pattern_path = args.run_dir / "product_patterns" / f"{seed_key}.json"
                if pattern_path.exists():
                    saved = json.loads(pattern_path.read_text())
                    return saved["pattern"], saved["classifications"]
                pattern, labels = extract_product_pattern(source, item, client,
                    f"live__{seed_key}__product_pattern")
                write_json(pattern_path, {"pattern": pattern, "classifications": labels})
                return pattern, labels

            # Only product synthesis runs in the extra thread; reference capture retains
            # its own synchronous browser. Both finish before extraction/client changes.
            with ThreadPoolExecutor(max_workers=1) as stages:
                product_future = (
                    stages.submit(product_stage, extraction)
                    if args.product_patterns and extraction["capabilities"]
                    else None
                )
                if args.mode == "url" and args.source_mode != "none":
                    from inspiration_library.live_component_sources import capture_component_sources
                    reference_path = args.run_dir / "reference_extractions" / f"{seed_key}.json"
                    if reference_path.exists():
                        extraction = json.loads(reference_path.read_text(encoding="utf-8"))
                        validate_live_card_evidence(extraction, observation)
                    else:
                        from tempfile import mkdtemp
                        component_root = args.run_dir / "components" / seed_key
                        component_root.mkdir(parents=True, exist_ok=True)
                        # A failed attempt remains intact; explicit resume gets a fresh child.
                        attempt = Path(mkdtemp(prefix="attempt_", dir=component_root))
                        extraction = capture_component_sources(extraction, observation,
                            attempt / "sources", max_regions=args.max_source_regions,
                            include_runtime_fragments=args.source_mode == "reference")
                        write_json(reference_path, extraction)
                if product_future is not None:
                    pattern, labels = product_future.result()
                    if pattern is not None:
                        product_patterns.append(pattern)
                    classifications.update(labels)
            extractions.append(extraction)
            print(
                f"[mine {index}/{len(sources)}] {source['seed_id']} "
                f"cards={len(extraction['capabilities'])}",
                flush=True,
            )

    raw_pool = merge_capability_pool(extractions, {"capabilities": []})
    if args.mode == "url":
        pool = [compact_live_card(card) for card in raw_pool]
    else:
        from inspiration_library.one_shot_capability_retrieval import compile_source_slices
        pool = [{**compact_planner_card(card, source_slices=compile_source_slices(card)),
                 "mode":"local_project",
                 "source_kind":"local_project", "source_seed_id":card["source_seed_id"],
                 "source_project":card["source_project"],
                 "observation_evidence":card.get("observation_evidence", [])} for card in raw_pool]
    pool.sort(key=lambda card: card["capability_id"])
    if args.product_patterns:
        for card in pool:
            card["product_id"] = card["source_seed_id"]
            card["classification"] = classifications[card["capability_id"]]
        write_jsonl(args.run_dir / "product_patterns.jsonl", product_patterns)
    write_jsonl(args.run_dir / "capability_pool.jsonl", pool)
    write_json(
        args.run_dir / "pool_manifest.json",
        {
            "schema_version": "webcoding-live-url-capability-pool-v1",
            "status": "ok",
            "mode": args.mode,
            "source_kind": "live_url" if args.mode == "url" else "local_project",
            "source_count": len(extractions),
            "capability_count": len(pool),
            "pool_snapshot_sha256": pool_snapshot_sha256(pool),
            "cards_with_source_slices": sum(bool(row["source_slices"]) for row in pool),
            "cards_with_browser_evidence": sum(bool(row["observation_evidence"]) for row in pool),
            "cards_with_validated_references": sum(row.get("evidence_reference_status") == "validated" for row in pool),
            "source_reference_mode": args.source_mode,
            "abstained_source_count": sum(not item["capabilities"] for item in extractions),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
