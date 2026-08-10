from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, TypeVar

from src.orchestration.schemas import (
    ALL_ARTIFACT_MODELS,
    AcceptedSprints,
    AssetManifest,
    DesignBrief,
    DesignTokens,
    FeatureList,
    Grades,
    HarnessState,
    LayoutContract,
    SprintPlan,
    UIVerificationPlan,
    VisualManifest,
    _Artifact,
)

T = TypeVar("T", bound=_Artifact)
_TEXT_ARTIFACTS = ("spec.md", "progress.md", "build_log.md")
_ROUND_TEXT_PATTERNS = ("feedback_round_*.md",)
_ROUND_IMAGE_PATTERNS = ("visual_round_*.png",)
_EDIT_SCOPE_PATTERNS = ("edit_scope_round_*.json",)
_PLANNING_TEXT_SCAFFOLDS = {
    "spec.md": (
        "# Draft Product - Working Title\n\n"
        "## Product Overview\n\n"
        "## Target Users\n\n"
        "## Feature Descriptions\n\n"
        "## Technical Architecture\n\n"
        "## Visual Design Direction\n"
    ),
    "progress.md": "# Progress Log\n",
}
_PLANNING_JSON_SCAFFOLDS = {
    "design_tokens.json": "{}\n",
    "feature_list.json": '{\n  "features": []\n}\n',
    "sprint_plan.json": '{\n  "total_sprints": 0,\n  "sprints": []\n}\n',
    "ui_verification_plan.json": '{\n  "sprints": []\n}\n',
}


class FileComm:
    """封装 `workdir/.harness` 目录下的文件读写。"""

    def __init__(self, harness_dir: Path) -> None:
        self.dir = harness_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- 通用路径与文本读写 ----

    def _path(self, name: str) -> Path:
        return self.dir / name

    def _read_text(self, name: str) -> str:
        path = self._path(name)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def _write_text(self, name: str, content: str) -> Path:
        path = self._path(name)
        path.write_text(content, encoding="utf-8")
        return path

    def _read(self, model: type[T], **params: Any) -> T | None:
        path = self._path(model.filename(**params))
        if not path.exists():
            return None
        return model.model_validate_json(path.read_text(encoding="utf-8"))

    def _write(self, payload: _Artifact, **params: Any) -> Path:
        path = self._path(payload.filename(**params))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
        return path

    @property
    def design_dir(self) -> Path:
        return self.dir / "design"

    # ---- Markdown 产物 ----

    def write_spec(self, content: str) -> Path:
        return self._write_text("spec.md", content)

    def read_spec(self) -> str:
        return self._read_text("spec.md")

    def write_progress(self, content: str) -> Path:
        return self._write_text("progress.md", content)

    def read_progress(self) -> str:
        return self._read_text("progress.md")

    def append_progress_entry(self, entry: str) -> Path:
        existing = self.read_progress()
        content = entry if not existing else f"{existing.rstrip()}\n\n{entry}"
        return self.write_progress(content)

    def initialize_planning_artifacts(self) -> None:
        """预创建 planner 必需文件，供 agent 直接更新内容。"""
        for name, content in _PLANNING_TEXT_SCAFFOLDS.items():
            path = self._path(name)
            if not path.exists():
                self._write_text(name, content)

        for name, content in _PLANNING_JSON_SCAFFOLDS.items():
            path = self._path(name)
            if not path.exists():
                self._write_text(name, content)

    def is_planning_scaffold(self, name: str) -> bool:
        """判断文件是否仍是 harness 预创建的 planner 占位内容。"""
        path = self._path(name)
        if not path.exists():
            return False
        scaffold = _PLANNING_TEXT_SCAFFOLDS.get(name)
        if scaffold is None:
            scaffold = _PLANNING_JSON_SCAFFOLDS.get(name)
        if scaffold is None:
            return False
        return path.read_text(encoding="utf-8") == scaffold

    def write_feedback(self, round_num: int, content: str) -> Path:
        return self._write_text(f"feedback_round_{round_num}.md", content)

    def read_feedback(self, round_num: int) -> str:
        return self._read_text(f"feedback_round_{round_num}.md")

    def write_build_log(self, content: str) -> Path:
        return self._write_text("build_log.md", content)

    def read_build_log(self) -> str:
        return self._read_text("build_log.md")

    # ---- JSON 产物 ----

    def write_design_tokens(self, tokens: dict[str, Any]) -> Path:
        return self._write(DesignTokens.model_validate(tokens))

    def read_design_tokens(self) -> dict[str, Any] | None:
        model = self._read(DesignTokens)
        return model.model_dump() if model else None

    def write_target_profile(self, payload: dict[str, Any]) -> Path:
        return self._write_text(
            "target_profile.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def read_target_profile(self) -> dict[str, Any] | None:
        text = self._read_text("target_profile.json")
        return json.loads(text) if text else None

    def write_feature_list(self, feature_list: dict[str, Any]) -> Path:
        return self._write(FeatureList.model_validate(feature_list))

    def read_feature_list(self) -> dict[str, Any] | None:
        model = self._read(FeatureList)
        return model.model_dump() if model else None

    def write_sprint_plan(self, sprint_plan: dict[str, Any]) -> Path:
        return self._write(SprintPlan.model_validate(sprint_plan))

    def read_sprint_plan(self) -> dict[str, Any] | None:
        model = self._read(SprintPlan)
        return model.model_dump() if model else None

    def write_ui_verification_plan(self, verification_plan: dict[str, Any]) -> Path:
        model = UIVerificationPlan.model_validate(verification_plan)
        path = self._path(model.filename())
        path.write_text(
            model.model_dump_json(indent=2, exclude_unset=True), encoding="utf-8"
        )
        return path

    def read_ui_verification_plan(self) -> dict[str, Any] | None:
        model = self._read(UIVerificationPlan)
        # Keep legacy planning artifacts byte-semantically compatible: the
        # optional `actions` default must not appear merely because a newer
        # reader loaded an older action-less plan.
        return model.model_dump(exclude_unset=True) if model else None

    def write_design_brief(self, payload: dict[str, Any]) -> Path:
        return self._write(DesignBrief.model_validate(payload))

    def read_design_brief(self) -> dict[str, Any] | None:
        model = self._read(DesignBrief)
        return model.model_dump() if model else None

    def write_layout_contract(self, payload: dict[str, Any]) -> Path:
        return self._write(LayoutContract.model_validate(payload))

    def read_layout_contract(self) -> dict[str, Any] | None:
        model = self._read(LayoutContract)
        return model.model_dump() if model else None

    def write_asset_manifest(self, payload: dict[str, Any]) -> Path:
        return self._write(AssetManifest.model_validate(payload))

    def read_asset_manifest(self) -> dict[str, Any] | None:
        model = self._read(AssetManifest)
        return model.model_dump() if model else None

    def write_accepted_sprints(self, accepted_sprints: dict[str, Any]) -> Path:
        return self._write(AcceptedSprints.model_validate(accepted_sprints))

    def read_accepted_sprints(self) -> dict[str, Any] | None:
        model = self._read(AcceptedSprints)
        return model.model_dump() if model else None

    def write_grades(self, round_num: int, grades: dict[str, Any]) -> Path:
        return self._write(Grades.model_validate(grades), round_num=round_num)

    def read_grades(self, round_num: int) -> dict[str, Any] | None:
        model = self._read(Grades, round_num=round_num)
        return model.model_dump() if model else None

    def write_visual_manifest(self, round_num: int, payload: dict[str, Any]) -> Path:
        return self._write(VisualManifest.model_validate(payload), round_num=round_num)

    def read_visual_manifest(self, round_num: int) -> dict[str, Any] | None:
        model = self._read(VisualManifest, round_num=round_num)
        return model.model_dump() if model else None

    def write_state(self, state: dict[str, Any]) -> Path:
        return self._write(HarnessState.model_validate(state))

    def read_state(self) -> dict[str, Any] | None:
        model = self._read(HarnessState)
        return model.model_dump() if model else None

    # ---- 运行重置 ----

    def _unlink_matching(self, *patterns: str) -> None:
        for pattern in patterns:
            for path in self.dir.glob(pattern):
                path.unlink(missing_ok=True)

    def reset_run_artifacts(self) -> None:
        """在新一轮执行前清理本轮临时产物。"""
        for name in _TEXT_ARTIFACTS:
            self._path(name).unlink(missing_ok=True)
        self._path("target_profile.json").unlink(missing_ok=True)
        self._unlink_matching(
            *_ROUND_TEXT_PATTERNS, *_ROUND_IMAGE_PATTERNS, *_EDIT_SCOPE_PATTERNS
        )

        # 通过 schema 注册表删除 JSON 产物。
        for model in ALL_ARTIFACT_MODELS:
            try:
                static_name = model.filename()
            except TypeError:
                # 轮次相关文件需要 round_num 参数，这里改用 glob 删除。
                template = model.filename(round_num=0)
                pattern = template.replace("_0.json", "_*.json")
                self._unlink_matching(pattern)
            else:
                self._path(static_name).unlink(missing_ok=True)

        for subdir in ("logs", "traces", "design"):
            path = self._path(subdir)
            if path.exists():
                shutil.rmtree(path)
