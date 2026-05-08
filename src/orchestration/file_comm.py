from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


class FileComm:
    """Read/write handoff files in workdir/.harness/."""

    def __init__(self, harness_dir: Path) -> None:
        self.dir = harness_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def write_spec(self, content: str) -> Path:
        path = self.dir / "spec.md"
        path.write_text(content)
        return path

    def read_spec(self) -> str:
        path = self.dir / "spec.md"
        return path.read_text() if path.exists() else ""

    def write_design_tokens(self, tokens: dict[str, Any]) -> Path:
        return self._write_json("design_tokens.json", tokens)

    def read_design_tokens(self) -> dict[str, Any] | None:
        return self._read_json("design_tokens.json")

    def write_feature_list(self, feature_list: dict[str, Any]) -> Path:
        return self._write_json("feature_list.json", feature_list)

    def read_feature_list(self) -> dict[str, Any] | None:
        return self._read_json("feature_list.json")

    def write_sprint_plan(self, sprint_plan: dict[str, Any]) -> Path:
        return self._write_json("sprint_plan.json", sprint_plan)

    def read_sprint_plan(self) -> dict[str, Any] | None:
        return self._read_json("sprint_plan.json")

    def write_ui_verification_plan(self, verification_plan: dict[str, Any]) -> Path:
        return self._write_json("ui_verification_plan.json", verification_plan)

    def read_ui_verification_plan(self) -> dict[str, Any] | None:
        return self._read_json("ui_verification_plan.json")

    def write_accepted_sprints(self, accepted_sprints: dict[str, Any]) -> Path:
        return self._write_json("accepted_sprints.json", accepted_sprints)

    def read_accepted_sprints(self) -> dict[str, Any] | None:
        return self._read_json("accepted_sprints.json")

    def write_progress(self, content: str) -> Path:
        path = self.dir / "progress.md"
        path.write_text(content)
        return path

    def read_progress(self) -> str:
        path = self.dir / "progress.md"
        return path.read_text() if path.exists() else ""

    def append_progress_entry(self, entry: str) -> Path:
        path = self.dir / "progress.md"
        existing = self.read_progress()
        content = entry if not existing else f"{existing.rstrip()}\n\n{entry}"
        path.write_text(content)
        return path

    def write_feedback(self, round_num: int, content: str) -> Path:
        path = self.dir / f"feedback_round_{round_num}.md"
        path.write_text(content)
        return path

    def read_feedback(self, round_num: int) -> str:
        path = self.dir / f"feedback_round_{round_num}.md"
        return path.read_text() if path.exists() else ""

    def write_grades(self, round_num: int, grades: dict) -> Path:
        return self._write_json(f"grade_round_{round_num}.json", grades)

    def read_grades(self, round_num: int) -> dict | None:
        return self._read_json(f"grade_round_{round_num}.json")

    def write_visual_manifest(self, round_num: int, payload: dict[str, Any]) -> Path:
        return self._write_json(f"visual_manifest_round_{round_num}.json", payload)

    def read_visual_manifest(self, round_num: int) -> dict[str, Any] | None:
        return self._read_json(f"visual_manifest_round_{round_num}.json")

    def write_repair_targets(self, round_num: int, payload: dict[str, Any]) -> Path:
        return self._write_json(f"repair_targets_round_{round_num}.json", payload)

    def read_repair_targets(self, round_num: int) -> dict[str, Any] | None:
        return self._read_json(f"repair_targets_round_{round_num}.json")

    def write_repair_report(self, round_num: int, payload: dict[str, Any]) -> Path:
        return self._write_json(f"repair_report_round_{round_num}.json", payload)

    def read_repair_report(self, round_num: int) -> dict[str, Any] | None:
        return self._read_json(f"repair_report_round_{round_num}.json")

    def write_repair_incomplete(self, round_num: int, payload: dict[str, Any]) -> Path:
        return self._write_json(f"repair_incomplete_round_{round_num}.json", payload)

    def write_build_log(self, content: str) -> Path:
        path = self.dir / "build_log.md"
        path.write_text(content)
        return path

    def read_build_log(self) -> str:
        path = self.dir / "build_log.md"
        return path.read_text() if path.exists() else ""

    def write_state(self, state: dict) -> Path:
        return self._write_json("harness_state.json", state)

    def read_state(self) -> dict | None:
        return self._read_json("harness_state.json")

    def reset_run_artifacts(self) -> None:
        """Remove transient harness artifacts before a fresh run."""
        for pattern in (
            "spec.md",
            "design_tokens.json",
            "feature_list.json",
            "sprint_plan.json",
            "ui_verification_plan.json",
            "accepted_sprints.json",
            "progress.md",
            "build_log.md",
            "harness_state.json",
            "feedback_round_*.md",
            "grade_round_*.json",
            "visual_manifest_round_*.json",
            "visual_round_*.png",
            "repair_targets_round_*.json",
            "repair_report_round_*.json",
            "repair_incomplete_round_*.json",
        ):
            for path in self.dir.glob(pattern):
                path.unlink(missing_ok=True)

        for subdir in ("logs", "traces"):
            path = self.dir / subdir
            if path.exists():
                shutil.rmtree(path)

    def _write_json(self, filename: str, payload: dict[str, Any]) -> Path:
        path = self.dir / filename
        path.write_text(json.dumps(payload, indent=2))
        return path

    def _read_json(self, filename: str) -> dict[str, Any] | None:
        path = self.dir / filename
        if not path.exists():
            return None
        return json.loads(path.read_text())
