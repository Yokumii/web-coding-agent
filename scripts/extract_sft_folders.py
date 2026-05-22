#!/usr/bin/env python3
"""将 harness workdir 的产出整理为文件夹结构的 SFT 训练数据。

数据类型：
  plan     — 需求 → sprint 规划
  generate — 第一个 sprint，从零生成代码
  edit     — 新 sprint 首轮，在已有代码上添加新功能
  repair   — sprint 内的迭代修复

输出结构：
  output_dir/
    {workdir}/
      00_plan/
        input.md, output.md
      01_generate_sprint1/
        input.md, output.md
      02_edit_sprint2/
        input.md, output.md
      03_repair_sprint2_r4/
        input.md, output.md, screenshot_*.png

用法:
  python scripts/extract_sft_folders.py chat-ui-test -o sft_output
  python scripts/extract_sft_folders.py e2e-test-4 chat-ui-test -o sft_output
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def get_commit_hashes(frontend_dir: Path) -> list[dict[str, str]]:
    log = _git(["log", "--reverse", "--format=%H %s"], cwd=frontend_dir).strip()
    if not log:
        return []
    commits = []
    for line in log.splitlines():
        parts = line.split(" ", 1)
        commits.append({"hash": parts[0], "subject": parts[1] if len(parts) > 1 else ""})
    return commits


def extract_code_snapshot(frontend_dir: Path, commit_hash: str) -> dict[str, str]:
    binary_exts = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
        ".woff", ".woff2", ".ttf", ".eot", ".zip", ".tar", ".gz",
        ".mp3", ".mp4", ".webm", ".lock",
    }
    file_list = _git(["ls-tree", "-r", "--name-only", commit_hash], cwd=frontend_dir).strip()
    if not file_list:
        return {}
    snapshot: dict[str, str] = {}
    for fpath in file_list.splitlines():
        if Path(fpath).suffix.lower() in binary_exts:
            continue
        try:
            snapshot[fpath] = _git(["show", f"{commit_hash}:{fpath}"], cwd=frontend_dir)
        except RuntimeError:
            pass
    return snapshot


def extract_code_diff(frontend_dir: Path, hash_a: str, hash_b: str) -> str:
    return _git(["diff", hash_a, hash_b], cwd=frontend_dir)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def parse_round_info(subject: str) -> dict[str, Any]:
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


def format_code_snapshot_md(snapshot: dict[str, str]) -> str:
    lines: list[str] = []
    for fpath in sorted(snapshot.keys()):
        ext = Path(fpath).suffix.lstrip(".")
        lang = ext if ext else "text"
        lines.append(f"## `{fpath}`\n")
        lines.append(f"```{lang}")
        lines.append(snapshot[fpath].rstrip())
        lines.append("```\n")
    return "\n".join(lines)


def format_grade_md(grade: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"**Overall**: {'PASS' if grade.get('overall_passed') else 'FAIL'}")
    lines.append(f"**Sprint**: {grade.get('sprint')}")
    lines.append("")
    criteria = grade.get("criteria", {})
    for name, info in criteria.items():
        score = info.get("score", "?")
        passed = "PASS" if info.get("passed") else "FAIL"
        notes = info.get("notes", "")
        lines.append(f"### {name}: {score}/10 ({passed})")
        lines.append(notes)
        lines.append("")
    return "\n".join(lines)


def format_sprint_context_md(sprint: dict[str, Any], design_tokens: dict[str, Any] | None = None) -> str:
    lines: list[str] = []
    lines.append(f"# Sprint {sprint.get('number', '?')}: {sprint.get('title', '')}\n")
    lines.append(f"**Goal**: {sprint.get('goal', '')}\n")
    lines.append("## Deliverables")
    for d in sprint.get("deliverables", []):
        lines.append(f"- {d}")
    lines.append("")
    lines.append("## Exit Criteria")
    for c in sprint.get("exit_criteria", []):
        lines.append(f"- {c}")
    if design_tokens:
        lines.append("\n## Design Tokens")
        lines.append(f"```json\n{json.dumps(design_tokens, indent=2, ensure_ascii=False)}\n```")
    return "\n".join(lines)


def process_workdir(workdir: Path, output_root: Path) -> int:
    harness = workdir / ".harness"
    frontend_dir = workdir / "frontend"
    workdir_name = workdir.name

    if not harness.exists():
        print(f"[WARN] {workdir} 没有 .harness 目录，跳过", file=sys.stderr)
        return 0

    out_dir = output_root / workdir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    seq = 0

    # --- plan ---
    spec_path = harness / "spec.md"
    plan_path = harness / "sprint_plan.json"
    state_path = harness / "harness_state.json"
    state = load_json(state_path) if state_path.exists() else {}

    if spec_path.exists() and plan_path.exists():
        task_dir = out_dir / f"{seq:02d}_plan"
        task_dir.mkdir(exist_ok=True)

        (task_dir / "input.md").write_text(
            f"# 需求\n\n{load_text(spec_path)}",
            encoding="utf-8",
        )

        plan = load_json(plan_path)
        plan_lines = [f"# Sprint 规划\n\n共 {plan.get('total_sprints', '?')} 个 sprint\n"]
        for s in plan.get("sprints", []):
            plan_lines.append(f"## Sprint {s['number']}: {s['title']}")
            plan_lines.append(f"**Goal**: {s['goal']}\n")
            plan_lines.append("**Deliverables**:")
            for d in s.get("deliverables", []):
                plan_lines.append(f"- {d}")
            plan_lines.append("\n**Exit Criteria**:")
            for c in s.get("exit_criteria", []):
                plan_lines.append(f"- {c}")
            plan_lines.append("")
        (task_dir / "output.md").write_text("\n".join(plan_lines), encoding="utf-8")
        count += 1
        seq += 1

    # --- rounds ---
    if not frontend_dir.exists() or not (frontend_dir / ".git").exists():
        return count

    commits = get_commit_hashes(frontend_dir)
    if not commits:
        return count

    sprint_plan = load_json(plan_path) if plan_path.exists() else {}
    sprints_by_num: dict[int, dict[str, Any]] = {}
    for s in sprint_plan.get("sprints", []):
        sprints_by_num[s["number"]] = s

    design_tokens_path = harness / "design_tokens.json"
    design_tokens = load_json(design_tokens_path) if design_tokens_path.exists() else None

    # Track which sprint each round belongs to, and find the first round of each sprint
    sprint_first_round: dict[int, int] = {}
    round_infos: list[dict[str, Any]] = []
    for idx, commit in enumerate(commits):
        info = parse_round_info(commit["subject"])
        info["idx"] = idx
        info["hash"] = commit["hash"]
        round_n = info["round"] or (idx + 1)
        sprint_n = info["sprint"] or 1
        info["round"] = round_n
        info["sprint"] = sprint_n
        round_infos.append(info)
        if sprint_n not in sprint_first_round:
            sprint_first_round[sprint_n] = round_n

    for idx, info in enumerate(round_infos):
        round_n = info["round"]
        sprint_n = info["sprint"]
        commit_hash = info["hash"]
        sprint_ctx = sprints_by_num.get(sprint_n, {})

        grade_path = harness / f"grade_round_{round_n}.json"
        grade = load_json(grade_path) if grade_path.exists() else None
        passed = grade.get("overall_passed", False) if grade else False

        is_first_round_of_sprint = (sprint_first_round.get(sprint_n) == round_n)

        if is_first_round_of_sprint and sprint_n == 1:
            # --- generate: 第一个 sprint 的首轮 ---
            task_type = "generate"
            task_dir = out_dir / f"{seq:02d}_generate_sprint{sprint_n}"
            task_dir.mkdir(exist_ok=True)

            input_lines = [
                "# 任务类型: generate（从零生成）\n",
                format_sprint_context_md(sprint_ctx, design_tokens),
            ]
            (task_dir / "input.md").write_text("\n".join(input_lines), encoding="utf-8")

            code = extract_code_snapshot(frontend_dir, commit_hash)
            (task_dir / "output.md").write_text(
                f"# 生成代码\n\n{format_code_snapshot_md(code)}", encoding="utf-8",
            )

            if grade:
                (task_dir / "grade.md").write_text(
                    f"# 评审结果\n\n{format_grade_md(grade)}", encoding="utf-8",
                )
            count += 1
            seq += 1

        elif is_first_round_of_sprint and sprint_n > 1:
            # --- edit: 新 sprint 首轮，在已有代码上加功能 ---
            prev_commit = commits[idx - 1] if idx > 0 else None
            task_dir = out_dir / f"{seq:02d}_edit_sprint{sprint_n}"
            task_dir.mkdir(exist_ok=True)

            input_lines = [
                "# 任务类型: edit（在已有代码上添加新功能）\n",
                format_sprint_context_md(sprint_ctx, design_tokens),
            ]

            if prev_commit:
                prev_info = round_infos[idx - 1]
                prev_grade_path = harness / f"grade_round_{prev_info['round']}.json"
                if prev_grade_path.exists():
                    prev_grade = load_json(prev_grade_path)
                    input_lines.append(f"\n## 上一轮评审\n\n{format_grade_md(prev_grade)}")

                diff = extract_code_diff(frontend_dir, prev_commit["hash"], commit_hash)
                (task_dir / "output.md").write_text(
                    f"# 代码变更 (diff)\n\n```diff\n{diff}\n```", encoding="utf-8",
                )
            else:
                code = extract_code_snapshot(frontend_dir, commit_hash)
                (task_dir / "output.md").write_text(
                    f"# 生成代码\n\n{format_code_snapshot_md(code)}", encoding="utf-8",
                )

            (task_dir / "input.md").write_text("\n".join(input_lines), encoding="utf-8")

            if grade:
                (task_dir / "grade.md").write_text(
                    f"# 评审结果\n\n{format_grade_md(grade)}", encoding="utf-8",
                )

            # Copy screenshots from previous round for context
            if prev_commit:
                prev_round = round_infos[idx - 1]["round"]
                for png in sorted(harness.glob(f"visual_round_{prev_round}_*.png")):
                    shutil.copy2(png, task_dir / png.name)

            count += 1
            seq += 1

        else:
            # --- repair: sprint 内迭代修复 ---
            if idx == 0:
                continue

            prev_commit_info = round_infos[idx - 1]
            prev_commit_hash = prev_commit_info["hash"]
            prev_round = prev_commit_info["round"]

            task_dir = out_dir / f"{seq:02d}_repair_sprint{sprint_n}_r{round_n}"
            task_dir.mkdir(exist_ok=True)

            # input: feedback + grade
            input_lines = ["# 任务类型: repair（修复 evaluator 发现的问题）\n"]
            input_lines.append(f"**Sprint**: {sprint_n} — {sprint_ctx.get('title', '')}\n")

            feedback_path = harness / f"feedback_round_{prev_round}.md"
            if feedback_path.exists():
                input_lines.append(f"## Evaluator 反馈\n\n{load_text(feedback_path)}")

            prev_grade_path = harness / f"grade_round_{prev_round}.json"
            if prev_grade_path.exists():
                prev_grade = load_json(prev_grade_path)
                input_lines.append(f"\n## 评分详情\n\n{format_grade_md(prev_grade)}")

            (task_dir / "input.md").write_text("\n".join(input_lines), encoding="utf-8")

            # output: diff
            diff = extract_code_diff(frontend_dir, prev_commit_hash, commit_hash)
            (task_dir / "output.md").write_text(
                f"# 修复变更 (diff)\n\n```diff\n{diff}\n```", encoding="utf-8",
            )

            if grade:
                (task_dir / "grade.md").write_text(
                    f"# 修复后评审结果\n\n{format_grade_md(grade)}", encoding="utf-8",
                )

            # Copy screenshots from previous round
            for png in sorted(harness.glob(f"visual_round_{prev_round}_*.png")):
                shutil.copy2(png, task_dir / png.name)

            count += 1
            seq += 1

    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="将 harness workdir 整理为文件夹结构的 SFT 数据")
    parser.add_argument("workdirs", nargs="+", type=Path, help="harness 工作目录")
    parser.add_argument("-o", "--output", type=Path, default=Path("sft_output"), help="输出目录")
    args = parser.parse_args()

    total = 0
    for wd in args.workdirs:
        wd = wd.resolve()
        n = process_workdir(wd, args.output.resolve())
        total += n
        print(f"  {wd.name}: {n} 条", file=sys.stderr)

    print(f"共提取 {total} 条样本 → {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
