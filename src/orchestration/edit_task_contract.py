"""First-class task-mode and route contract for scoped Edit runs."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


CONTRACT_NAME = "edit_task_contract.json"


class EditTaskContractError(ValueError):
    """The requested Edit cannot be frozen into a safe source/route contract."""


def normalize_target_routes(routes: Iterable[str]) -> list[str]:
    output: list[str] = []
    for raw in routes:
        route = str(raw).strip()
        parsed = urlsplit(route)
        segments = parsed.path.replace("\\", "/").split("/")
        fragment_segments = parsed.fragment.split("/")
        fragment_ok = not parsed.fragment or (
            parsed.fragment.startswith("/")
            and "\\" not in parsed.fragment
            and "://" not in parsed.fragment
            and "?" not in parsed.fragment
            and all(segment not in {".", ".."} for segment in fragment_segments)
        )
        if (
            not route.startswith("/") or route.startswith("//") or "\\" in route
            or parsed.scheme or parsed.netloc or parsed.query or not fragment_ok
            or any(segment in {".", ".."} for segment in segments)
        ):
            raise EditTaskContractError(
                f"target route must be one same-origin pathname or bounded hash-router path: {route!r}"
            )
        path = "/" if parsed.path == "/" else parsed.path.rstrip("/")
        fragment = parsed.fragment.rstrip("/")
        normalized = path + (f"#{fragment}" if fragment else "")
        if normalized not in output:
            output.append(normalized)
    return output


def _git(frontend: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=frontend, text=True, capture_output=True
    )
    if result.returncode != 0:
        raise EditTaskContractError(
            f"git {' '.join(args)} failed for Edit seed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _ensure_baseline_commit(frontend: Path) -> str:
    new_repo = not (frontend / ".git").exists()
    if new_repo:
        _git(frontend, "init", "-b", "main")
    exclude = frontend / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        generated = (
            "\n# Harness-local generated/runtime exclusions\n"
            "node_modules/\ndist/\nbuild/\n.next/\ncoverage/\n.vite/\n.DS_Store\n"
        )
        if "# Harness-local generated/runtime exclusions" not in existing:
            exclude.write_text(existing.rstrip() + generated, encoding="utf-8")
    if new_repo:
        _git(frontend, "add", "--all")
        result = subprocess.run(
            [
                "git", "-c", "user.name=Harness", "-c",
                "user.email=harness@localhost", "commit", "-m",
                "chore: accepted edit baseline",
            ],
            cwd=frontend, text=True, capture_output=True,
        )
        if result.returncode != 0:
            raise EditTaskContractError(
                f"could not create Edit baseline commit: {result.stderr.strip()}"
            )
    status = _git(frontend, "status", "--porcelain")
    if status:
        raise EditTaskContractError(
            "Edit mode requires a clean frontend worktree so unrelated pre-existing "
            "changes cannot enter the training patch. Commit or move them first."
        )
    try:
        return _git(frontend, "rev-parse", "HEAD")
    except EditTaskContractError:
        result = subprocess.run(
            [
                "git", "-c", "user.name=Harness", "-c",
                "user.email=harness@localhost", "commit", "--allow-empty", "-m",
                "chore: accepted edit baseline",
            ],
            cwd=frontend, text=True, capture_output=True,
        )
        if result.returncode != 0:
            raise EditTaskContractError(
                f"could not create empty Edit baseline commit: {result.stderr.strip()}"
            )
        return _git(frontend, "rev-parse", "HEAD")


def resolve_task_mode(workdir: Path, requested: str) -> str:
    if requested not in {"auto", "generate", "edit"}:
        raise EditTaskContractError(f"unsupported task mode: {requested!r}")
    if requested == "generate" and (workdir / "seed_manifest.json").is_file():
        raise EditTaskContractError(
            "explicit generate mode conflicts with seed_manifest.json; use Edit/auto or a fresh workdir"
        )
    if requested != "auto":
        return requested
    if (workdir / "seed_manifest.json").is_file():
        return "edit"
    return "generate"


def prepare_edit_task_contract(
    workdir: Path, *, requested_target_routes: Iterable[str]
) -> dict[str, Any]:
    workdir = workdir.resolve()
    normalized_routes = normalize_target_routes(requested_target_routes)
    frontend = workdir / "frontend"
    if not frontend.is_dir():
        raise EditTaskContractError(
            "Edit mode requires an existing workdir/frontend project. For copied seeds, "
            "prepare the seed first or point the batch task at an existing Edit workdir."
        )
    baseline = _ensure_baseline_commit(frontend)
    seed_path = workdir / "seed_manifest.json"
    if seed_path.is_file():
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        declared = str(seed.get("baseline_commit") or "")
        if not declared:
            raise EditTaskContractError("seed_manifest.json is missing baseline_commit")
        try:
            _git(frontend, "cat-file", "-e", f"{declared}^{{commit}}")
        except EditTaskContractError as exc:
            raise EditTaskContractError(
                "seed_manifest.json baseline_commit is unavailable in frontend Git"
            ) from exc
        baseline = declared
        current_head = _git(frontend, "rev-parse", "HEAD")
        if current_head != baseline:
            raise EditTaskContractError(
                "prepared seed baseline is not the current frontend HEAD; create a fresh "
                "Edit workdir so earlier changes cannot leak into this task"
            )
    else:
        seed_path.write_text(json.dumps({
            "status": "ok",
            "source_frontend": str(frontend),
            "source_evaluation": "inline_explicit_edit",
            "baseline_commit": baseline,
            "asset_policy": "preserve_existing_project",
            "external_asset_urls": [],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    contract = {
        "schema_version": "edit-task-contract-v1",
        "owner": "harness",
        "task_mode": "edit",
        "baseline_commit": baseline,
        "requested_target_routes": normalized_routes,
        "protect_non_target_routes": True,
        "source_policy": {
            "preserve_unrelated_files": True,
            "existing_source_requires_exact_patch": True,
            "fail_on_unresolved_route": True,
            "fail_on_planner_route_drift": True,
        },
    }
    harness = workdir / ".harness"
    harness.mkdir(parents=True, exist_ok=True)
    (harness / CONTRACT_NAME).write_text(
        json.dumps(contract, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return contract


def read_edit_task_contract(workdir: Path) -> dict[str, Any] | None:
    path = Path(workdir) / ".harness" / CONTRACT_NAME
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "edit-task-contract-v1":
        raise EditTaskContractError(f"unsupported Edit task contract: {path}")
    return payload
