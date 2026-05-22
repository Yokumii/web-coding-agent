#!/usr/bin/env python3
"""从 web-coding-agent harness 工作目录中提取 SFT 训练数据。

产出 4 类样本：
  plan          — 需求 spec → sprint 规划
  generate      — sprint 上下文 → 代码快照
  repair        — evaluator 反馈 + 旧代码 → 修复 diff
  visual_repair — 截图 + 反馈 → 修复 diff（多模态）

用法:
  python scripts/extract_sft_data.py e2e-test-4 -o sft_data.jsonl
  python scripts/extract_sft_data.py e2e-test-* -o sft_data.jsonl
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def get_commit_hashes(frontend_dir: Path) -> list[dict[str, str]]:
    """返回按时间正序排列的 commit 列表 [{hash, subject}]。"""
    log = _git(["log", "--reverse", "--format=%H %s"], cwd=frontend_dir).strip()
    if not log:
        return []
    commits = []
    for line in log.splitlines():
        parts = line.split(" ", 1)
        commits.append({"hash": parts[0], "subject": parts[1] if len(parts) > 1 else ""})
    return commits


def extract_code_snapshot(frontend_dir: Path, commit_hash: str) -> dict[str, str]:
    """提取指定 commit 的所有文本文件内容，返回 {相对路径: 内容}。"""
    file_list = _git(["ls-tree", "-r", "--name-only", commit_hash], cwd=frontend_dir).strip()
    if not file_list:
        return {}

    snapshot: dict[str, str] = {}
    for fpath in file_list.splitlines():
        if _is_binary_filename(fpath):
            continue
        try:
            content = _git(["show", f"{commit_hash}:{fpath}"], cwd=frontend_dir)
            snapshot[fpath] = content
        except RuntimeError:
            pass
    return snapshot


def extract_code_diff(frontend_dir: Path, hash_a: str, hash_b: str) -> str:
    return _git(["diff", hash_a, hash_b], cwd=frontend_dir)


def _is_binary_filename(name: str) -> bool:
    binary_exts = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
        ".woff", ".woff2", ".ttf", ".eot",
        ".zip", ".tar", ".gz",
        ".mp3", ".mp4", ".webm",
        ".lock",
    }
    return Path(name).suffix.lower() in binary_exts


# ---------------------------------------------------------------------------
# File loaders
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_trace(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def encode_screenshot(path: Path) -> str:
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}"


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------

def parse_round_info(subject: str) -> dict[str, Any]:
    """从 commit subject 解析 round/sprint/mode 信息。

    示例: "round 02 / sprint_2 (repair): generator output"
    """
    info: dict[str, Any] = {"round": 0, "sprint": 0, "mode": "generate"}
    parts = subject.split("/")
    if len(parts) >= 2:
        try:
            info["round"] = int(parts[0].replace("round", "").strip())
        except ValueError:
            pass
        rest = parts[1].strip()
        if "sprint_" in rest:
            sprint_part = rest.split("sprint_")[1]
            try:
                info["sprint"] = int(sprint_part.split()[0].split("(")[0])
            except ValueError:
                pass
        if "(repair)" in rest:
            info["mode"] = "repair"
        elif "(generate)" in rest:
            info["mode"] = "generate"
    return info


def get_phase_cost(state: dict[str, Any], phase_key: str) -> float:
    return state.get("costs", {}).get(phase_key, 0.0)


def get_phase_model(state: dict[str, Any], phase_key: str) -> str:
    metrics = state.get("phase_metrics", {}).get(phase_key, {})
    model_usage = metrics.get("model_usage", {})
    for model_name in model_usage:
        if model_name != "model":
            return model_name
    if "model" in model_usage:
        return model_usage["model"]
    return ""


# ---------------------------------------------------------------------------
# Sample builders
# ---------------------------------------------------------------------------

def build_plan_sample(workdir: Path, workdir_name: str) -> dict[str, Any] | None:
    harness = workdir / ".harness"
    spec_path = harness / "spec.md"
    plan_path = harness / "sprint_plan.json"
    trace_path = harness / "traces" / "planner.jsonl"
    state_path = harness / "harness_state.json"

    if not spec_path.exists() or not plan_path.exists():
        return None

    state = load_json(state_path) if state_path.exists() else {}

    sample: dict[str, Any] = {
        "id": f"{workdir_name}__plan",
        "type": "plan",
        "source_workdir": workdir_name,
        "query": load_text(spec_path),
        "response": load_json(plan_path),
        "trace": load_trace(trace_path) if trace_path.exists() else [],
        "metadata": {
            "cost_usd": get_phase_cost(state, "planner"),
            "model": get_phase_model(state, "planner"),
            "prompt": state.get("prompt", ""),
        },
    }
    return sample


def build_generate_sample(
    workdir: Path,
    workdir_name: str,
) -> dict[str, Any] | None:
    """Type 2: generate — spec/PRD → 最终代码快照 + 全部轮次轨迹。"""
    harness = workdir / ".harness"
    frontend_dir = workdir / "frontend"
    state_path = harness / "harness_state.json"
    spec_path = harness / "spec.md"

    if not frontend_dir.exists() or not (frontend_dir / ".git").exists():
        return None

    state = load_json(state_path) if state_path.exists() else {}
    commits = get_commit_hashes(frontend_dir)
    if not commits:
        return None

    last_commit = commits[-1]
    last_round = parse_round_info(last_commit["subject"]).get("round", len(commits))
    code_snapshot = extract_code_snapshot(frontend_dir, last_commit["hash"])

    last_grade_path = harness / f"grade_round_{last_round}.json"
    last_grade = load_json(last_grade_path) if last_grade_path.exists() else None

    all_traces: list[dict[str, Any]] = []
    for rn in range(1, last_round + 1):
        gen_tp = harness / "traces" / f"generator_round_{rn}.jsonl"
        eval_tp = harness / "traces" / f"evaluator_round_{rn}.jsonl"
        round_trace: dict[str, Any] = {"round": rn}
        if gen_tp.exists():
            round_trace["generator"] = load_trace(gen_tp)
        if eval_tp.exists():
            round_trace["evaluator"] = load_trace(eval_tp)
        all_traces.append(round_trace)

    total_cost = sum(
        get_phase_cost(state, f"{phase}_r{rn}")
        for rn in range(1, last_round + 1)
        for phase in ("generator", "evaluator", "visual_score")
    )

    query = load_text(spec_path) if spec_path.exists() else state.get("prompt", "")

    return {
        "id": f"{workdir_name}__generate",
        "type": "generate",
        "source_workdir": workdir_name,
        "query": query,
        "response": code_snapshot,
        "trace": all_traces,
        "grade": last_grade,
        "metadata": {
            "total_rounds": last_round,
            "passed": last_grade.get("overall_passed", False) if last_grade else False,
            "cost_usd": total_cost,
            "model": get_phase_model(state, f"generator_r1"),
            "commit_hash": last_commit["hash"],
            "accepted_sprints": state.get("accepted_sprints", []),
        },
    }


def build_round_samples(
    workdir: Path,
    workdir_name: str,
) -> list[dict[str, Any]]:
    """Type 3 & 4: repair / visual_repair 样本，每轮迭代一条。"""
    harness = workdir / ".harness"
    frontend_dir = workdir / "frontend"
    state_path = harness / "harness_state.json"

    if not frontend_dir.exists() or not (frontend_dir / ".git").exists():
        return []

    state = load_json(state_path) if state_path.exists() else {}
    commits = get_commit_hashes(frontend_dir)
    if not commits:
        return []

    sprint_plan = load_json(harness / "sprint_plan.json") if (harness / "sprint_plan.json").exists() else {}
    sprints_by_num: dict[int, dict[str, Any]] = {}
    for s in sprint_plan.get("sprints", []):
        sprints_by_num[s["number"]] = s

    samples: list[dict[str, Any]] = []

    for idx, commit in enumerate(commits):
        info = parse_round_info(commit["subject"])
        round_n = info["round"] or (idx + 1)
        sprint_n = info["sprint"] or 1
        mode = info["mode"]

        grade_path = harness / f"grade_round_{round_n}.json"
        grade = load_json(grade_path) if grade_path.exists() else None
        passed = grade.get("overall_passed", False) if grade else False

        gen_trace_path = harness / "traces" / f"generator_round_{round_n}.jsonl"
        eval_trace_path = harness / "traces" / f"evaluator_round_{round_n}.jsonl"

        code_snapshot = extract_code_snapshot(frontend_dir, commit["hash"])

        sprint_ctx = sprints_by_num.get(sprint_n, {})

        # --- Type 3 & 4: repair / visual_repair ---
        if idx > 0:
            prev_commit = commits[idx - 1]
            prev_round = parse_round_info(prev_commit["subject"]).get("round", idx)
            prev_grade_path = harness / f"grade_round_{prev_round}.json"
            prev_grade = load_json(prev_grade_path) if prev_grade_path.exists() else None
            feedback_path = harness / f"feedback_round_{prev_round}.md"
            feedback = load_text(feedback_path) if feedback_path.exists() else ""

            code_before = extract_code_snapshot(frontend_dir, prev_commit["hash"])
            code_diff = extract_code_diff(frontend_dir, prev_commit["hash"], commit["hash"])

            repair_query = {
                "feedback": feedback,
                "grade": prev_grade,
                "sprint_context": sprint_ctx,
            }

            repair_sample: dict[str, Any] = {
                "id": f"{workdir_name}__round_{round_n}__repair",
                "type": "repair",
                "source_workdir": workdir_name,
                "query": json.dumps(repair_query, ensure_ascii=False),
                "code_before": code_before,
                "code_diff": code_diff,
                "response": code_snapshot,
                "trace": {
                    "generator": load_trace(gen_trace_path) if gen_trace_path.exists() else [],
                    "evaluator": load_trace(eval_trace_path) if eval_trace_path.exists() else [],
                },
                "grade_before": prev_grade,
                "grade_after": grade,
                "metadata": {
                    "round": round_n,
                    "sprint": sprint_n,
                    "mode": mode,
                    "passed": passed,
                    "cost_usd": get_phase_cost(state, f"generator_r{round_n}"),
                    "model": get_phase_model(state, f"generator_r{round_n}"),
                    "commit_hash": commit["hash"],
                    "prev_commit_hash": prev_commit["hash"],
                },
            }
            samples.append(repair_sample)

            # visual_repair: 只有当前一轮有截图时才生成
            screenshot_paths = list(harness.glob(f"visual_round_{prev_round}_*.png"))
            visual_manifest_path = harness / f"visual_manifest_round_{prev_round}.json"

            if screenshot_paths:
                screenshots_b64 = [encode_screenshot(p) for p in sorted(screenshot_paths)]
                visual_manifest = load_json(visual_manifest_path) if visual_manifest_path.exists() else {}

                visual_sample: dict[str, Any] = {
                    "id": f"{workdir_name}__round_{round_n}__visual_repair",
                    "type": "visual_repair",
                    "source_workdir": workdir_name,
                    "query": json.dumps(repair_query, ensure_ascii=False),
                    "screenshot_base64": screenshots_b64,
                    "visual_review": visual_manifest,
                    "code_before": code_before,
                    "code_diff": code_diff,
                    "response": code_snapshot,
                    "trace": {
                        "generator": load_trace(gen_trace_path) if gen_trace_path.exists() else [],
                        "evaluator": load_trace(eval_trace_path) if eval_trace_path.exists() else [],
                    },
                    "grade_before": prev_grade,
                    "grade_after": grade,
                    "metadata": {
                        "round": round_n,
                        "sprint": sprint_n,
                        "mode": mode,
                        "passed": passed,
                        "cost_usd": get_phase_cost(state, f"generator_r{round_n}"),
                        "model": get_phase_model(state, f"generator_r{round_n}"),
                        "commit_hash": commit["hash"],
                        "prev_commit_hash": prev_commit["hash"],
                        "screenshot_files": [p.name for p in sorted(screenshot_paths)],
                    },
                }
                samples.append(visual_sample)

    return samples


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def extract_workdir(workdir: Path) -> list[dict[str, Any]]:
    workdir_name = workdir.name
    samples: list[dict[str, Any]] = []

    plan_sample = build_plan_sample(workdir, workdir_name)
    if plan_sample:
        samples.append(plan_sample)

    gen_sample = build_generate_sample(workdir, workdir_name)
    if gen_sample:
        samples.append(gen_sample)

    samples.extend(build_round_samples(workdir, workdir_name))
    return samples


def main() -> None:
    parser = argparse.ArgumentParser(description="从 harness workdir 提取 SFT 训练数据")
    parser.add_argument("workdirs", nargs="+", type=Path, help="harness 工作目录路径")
    parser.add_argument("-o", "--output", type=Path, default=Path("sft_data.jsonl"), help="输出 JSONL 路径")
    parser.add_argument("--no-screenshots", action="store_true", help="跳过截图 base64 编码（减小体积）")
    parser.add_argument("--no-trace", action="store_true", help="跳过 trace 轨迹数据")
    parser.add_argument("--stats", action="store_true", help="输出统计信息")
    args = parser.parse_args()

    all_samples: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {}

    for wd in args.workdirs:
        wd = wd.resolve()
        if not (wd / ".harness").exists():
            print(f"[WARN] {wd} 没有 .harness 目录，跳过", file=sys.stderr)
            continue

        samples = extract_workdir(wd)

        for s in samples:
            if args.no_trace:
                s.pop("trace", None)
            if args.no_screenshots:
                s.pop("screenshot_base64", None)

            stype = s["type"]
            type_counts[stype] = type_counts.get(stype, 0) + 1

        all_samples.extend(samples)

    with open(args.output, "w", encoding="utf-8") as f:
        for sample in all_samples:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")

    total = len(all_samples)
    print(f"已提取 {total} 条样本 → {args.output}", file=sys.stderr)

    if args.stats or total > 0:
        for stype, count in sorted(type_counts.items()):
            print(f"  {stype}: {count}", file=sys.stderr)


if __name__ == "__main__":
    main()
