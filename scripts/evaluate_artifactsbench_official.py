"""Run the official ArtifactsBench checklist-guided Gemini judge on harness outputs.

The prompt and score extraction intentionally follow Tencent-Hunyuan/
ArtifactsBenchmark at commit 88c968b.  Only the HTTP transport is adapted from
the repository's internal ``model_marker`` API to an OpenAI-compatible endpoint.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from pathlib import Path

from openai import OpenAI


PROMPT = """You are a seasoned and meticulous code review expert, proficient in multiple programming languages, front-end technologies, and interaction design. Your task is to conduct an in-depth analysis and scoring of the received [question] and [answer]. The [answer] may include source code (in various programming languages), algorithm implementations, data structure designs, system architecture diagrams, front-end visualization code (such as HTML/SVG/JavaScript), interaction logic descriptions, and related technical explanations. Please leverage your coding expertise and aesthetic experience to thoroughly examine the [answer] content from the following dimensions and provide scores along with detailed review comments. You should be very strict and cautious when giving full marks for each dimension.

Role Definition

Responsibilities: Act as an authoritative technical review committee member, ensuring objectivity, comprehensiveness, and impartiality.
Attitude: Rigorous, professional, and unsparing, adept at identifying details and potential risks.
Additional Traits: Possess exceptional aesthetic talent, with high standards for visual appeal and user experience.

I have only extracted the last segment of HTML or SVG code from the provided answer for visualization. The content is adaptively scrolled to capture the entire page.

**Scoring Criteria:**

{checklist}

- The final output should be a JSON object containing the dimensions above, following this example:
```json
{{
  "Overall Score": "35"
}}
``` Reason:...

Please score the following question according to the standards above:

--------Problem starts--------
{question}
--------Problem ends--------

--------Answer starts--------
{answer}
--------Answer ends--------
"""

SCORE_PATTERNS = (
    r'"Overall Score":\s*(")?(\d+(?:\.\d+)?|\d+-\d+)(")?',
    r'"overallScore":\s*(")?(\d+(?:\.\d+)?|\d+-\d+)(")?',
    r'"总体打分":\s*(")?(\d+(?:\.\d+)?|\d+-\d+)(")?',
)


def extract_score(text: str) -> str | None:
    for pattern in SCORE_PATTERNS:
        matches = re.findall(pattern, text)
        if matches:
            return matches[-1][1]
    return None


def collect_answer(frontend: Path, max_chars: int = 100_000) -> str:
    parts: list[str] = []
    ignored = {"node_modules", ".git", "dist", ".harness"}
    for path in sorted(frontend.rglob("*")):
        if not path.is_file() or any(part in ignored for part in path.parts):
            continue
        if path.suffix.lower() not in {".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".json", ".svg"}:
            continue
        relative = path.relative_to(frontend)
        parts.append(f"\n===== {relative} =====\n{path.read_text(errors='replace')}")
        if sum(map(len, parts)) >= max_chars:
            break
    return "".join(parts)[:max_chars]


def screenshot_paths(case_dir: Path) -> list[Path]:
    harness = case_dir / ".harness"
    rounds = sorted(harness.glob("visual_manifest_round_*.json"))
    if not rounds:
        return []
    manifest_path = rounds[-1]
    manifest = json.loads(manifest_path.read_text())
    selected = [(case_dir / item).resolve() for item in manifest.get("screenshots", [])]
    round_num = int(manifest_path.stem.rsplit("_", 1)[-1])
    for path in sorted(harness.glob(f"visual_round_{round_num}_*.png")):
        resolved = path.resolve()
        if resolved not in selected:
            selected.append(resolved)
    return selected[:3]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gemini-2.5-pro-06-17")
    parser.add_argument("--max-tokens", type=int, default=8192)
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.queries.read_text().splitlines() if line.strip()]
    completed: dict[int, dict] = {}
    recovered_existing = False
    if args.output.exists():
        for line in args.output.read_text().splitlines():
            if line.strip():
                item = json.loads(line)
                if item.get("gemini_ans") is None and item.get("gemini_reason"):
                    item["gemini_ans"] = extract_score(str(item["gemini_reason"]))
                    recovered_existing = item["gemini_ans"] is not None or recovered_existing
                if item.get("gemini_ans") is not None:
                    completed[int(item["index"])] = item

    client = OpenAI(
        api_key=os.environ["EVALUATOR_OFFICIAL_API_KEY"],
        base_url=os.environ["EVALUATOR_OFFICIAL_BASE_URL"],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if recovered_existing:
        args.output.write_text("".join(
            json.dumps(item, ensure_ascii=False) + "\n"
            for _, item in sorted(completed.items())
        ))
    for row in rows:
        index = int(row["index"])
        if index in completed:
            continue
        case_dir = args.runs / f"index_{index:04d}"
        images = screenshot_paths(case_dir)
        if len(images) < 3 or not all(path.exists() for path in images):
            print(json.dumps({"index": index, "status": "missing_screenshots"}), flush=True)
            continue
        answer = collect_answer(case_dir / "frontend")
        content: list[dict] = [{
            "type": "text",
            "text": PROMPT.format(
                checklist=row.get("checklist"), question=row["question"], answer=answer
            ),
        }]
        for path in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()},
            })
        response = client.chat.completions.create(
            model=args.model,
            messages=[{"role": "user", "content": content}],
            max_tokens=args.max_tokens,
            timeout=600,
        )
        reason = response.choices[0].message.content or ""
        result = dict(row)
        result.update(
            answer=answer,
            gemini_reason=reason,
            gemini_ans=extract_score(reason),
            judge_model=args.model,
            screenshot_paths=[str(path) for path in images],
            judge_usage=(response.usage.model_dump() if response.usage else {}),
        )
        completed[index] = result
        args.output.write_text("".join(
            json.dumps(item, ensure_ascii=False) + "\n"
            for _, item in sorted(completed.items())
        ))
        print(json.dumps({"index": index, "gemini_ans": result["gemini_ans"]}), flush=True)

    scores = [float(item["gemini_ans"]) for item in completed.values() if item.get("gemini_ans") is not None]
    print(json.dumps({"scored": len(scores), "average": sum(scores) / len(scores) if scores else None}), flush=True)


if __name__ == "__main__":
    main()
