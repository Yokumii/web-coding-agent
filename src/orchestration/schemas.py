"""定义 `.harness/*.json` 各类产物的 Pydantic 模型。"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class _Artifact(BaseModel):
    """所有 `.harness` JSON 产物的基类。"""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def filename(cls, **params: Any) -> str:  # pragma: no cover - overridden
        raise NotImplementedError(
            f"{cls.__name__}.filename(**params) was not overridden"
        )


# ---- 规划阶段产物 -----------------------------------------------------------


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class VisualExperiment(BaseModel):
    """`design_tokens.json.visual_experiment` 的结构约束。"""

    model_config = ConfigDict(extra="forbid")

    design_hypothesis: NonEmptyString
    reason_for_image_first: NonEmptyString
    desired_break_from_web_templates: list[NonEmptyString] = Field(min_length=1)
    visual_opportunities_beyond_css: list[NonEmptyString] = Field(min_length=1)
    forbidden_generic_patterns: list[NonEmptyString] = Field(min_length=1)


class DesignTokens(_Artifact):
    """`design_tokens.json`，记录 planner 生成的设计系统定义。"""

    theme_name: str
    color: dict[str, Any]
    typography: dict[str, Any]
    spacing: dict[str, Any]
    radius: dict[str, Any]
    motion: dict[str, Any]
    style_rules: list[Any]
    anti_patterns: list[Any]
    visual_experiment: VisualExperiment
    # 这些字段在历史执行记录中出现过，因此保留为可选项。
    themes: dict[str, Any] | None = None
    shadow: dict[str, Any] | None = None

    @classmethod
    def filename(cls, **params: Any) -> str:
        return "design_tokens.json"


class Feature(BaseModel):
    """`feature_list.json` 中的单个功能项。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    priority: str
    depends_on: list[Any]
    description: str
    acceptance_criteria: list[Any]
    # `status` 初始由 planner 写为 planned，运行过程中会被 harness 更新。
    status: str
    sprint: int = Field(ge=1)


class FeatureList(_Artifact):
    """`feature_list.json`，记录全部规划功能。"""

    features: list[Feature]

    @classmethod
    def filename(cls, **params: Any) -> str:
        return "feature_list.json"


class Sprint(BaseModel):
    """`sprint_plan.json` 中的单个 sprint。"""

    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    title: str
    goal: str
    feature_ids: list[str] = Field(min_length=1)
    deliverables: list[str] = Field(min_length=1)
    exit_criteria: list[str] = Field(min_length=1)


class SprintPlan(_Artifact):
    """`sprint_plan.json`，按顺序描述各个 sprint。"""

    total_sprints: int = Field(ge=1)
    sprints: list[Sprint]

    @classmethod
    def filename(cls, **params: Any) -> str:
        return "sprint_plan.json"


class UIVerificationCheck(BaseModel):
    """`ui_verification_plan.json` 中的一条 UI 检查项。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    feature_id: str
    task: str
    expected_result: str
    critical: bool
    category: str
    # Exact same-origin browser route for this check. Optional only so older
    # single-page plans continue to load as the root route.
    route: str = "/"
    # Planner-authored, declarative browser steps.  Optional for backwards
    # compatibility with existing runs; new plans are asked to provide them.
    actions: list[dict[str, Any]] = Field(default_factory=list)


class UIVerificationSprint(BaseModel):
    """`ui_verification_plan.json` 中的单个 sprint 节点。"""

    model_config = ConfigDict(extra="forbid")

    sprint: int = Field(ge=1)
    checks: list[UIVerificationCheck]


class UIVerificationPlan(_Artifact):
    """`ui_verification_plan.json`，记录按 sprint 划分的 UI 检查项。"""

    sprints: list[UIVerificationSprint]

    @classmethod
    def filename(cls, **params: Any) -> str:
        return "ui_verification_plan.json"


class DesignBrief(_Artifact):
    """`design/design_brief.json`，连接 design 阶段与 generator。"""

    requested_mode: str
    visual_strategy: str
    reference_files: dict[str, str]
    aesthetic_intent: dict[str, Any]
    responsive_strategy: dict[str, Any]
    overlay_regions: list[dict[str, Any]]
    visual_success_criteria: list[str] = Field(default_factory=list)
    implementation_rules: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None

    @classmethod
    def filename(cls, **params: Any) -> str:
        return "design/design_brief.json"


class LayoutContract(_Artifact):
    """`design/layout_contract.json`，描述 overlay 区域与响应式约束。"""

    viewport_targets: list[str]
    regions: list[dict[str, Any]]
    safe_zones: list[dict[str, Any]] = Field(default_factory=list)
    forbidden_overlay_zones: list[dict[str, Any]] = Field(default_factory=list)
    asset_fit: dict[str, Any] = Field(default_factory=dict)
    responsive_rules: list[str] = Field(default_factory=list)

    @classmethod
    def filename(cls, **params: Any) -> str:
        return "design/layout_contract.json"


class AssetManifest(_Artifact):
    """`design/asset_manifest.json`，记录 design 阶段输出资产。"""

    assets: list[dict[str, Any]] = Field(default_factory=list)
    generation_records: list[dict[str, Any]] = Field(default_factory=list)
    implementation_notes: list[str] = Field(default_factory=list)

    @classmethod
    def filename(cls, **params: Any) -> str:
        return "design/asset_manifest.json"


# ---- 每轮产物 ---------------------------------------------------------------


class Criterion(BaseModel):
    """`Grades.criteria` 中的一项评分结果。"""

    model_config = ConfigDict(extra="forbid")

    score: float
    passed: bool
    notes: str = ""


class UICheck(BaseModel):
    """`grade_round_N.json::ui_checks` 中的一条检查结果。"""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    feature_id: str
    critical: bool
    task: str
    expected_result: str
    # 历史上出现过额外状态，因此保持为 str，避免过早收紧。
    status: str
    notes: str = ""


class ExitCriterionResult(BaseModel):
    """`Grades` 中的一条目标 sprint 退出条件检查结果。"""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str
    feature_id: str
    critical: bool
    criterion: str
    passed: bool
    notes: str = ""


class AppearanceReview(BaseModel):
    """`Grades` 中由视觉评分模块写入的外观评分块。"""

    model_config = ConfigDict(extra="forbid")

    screenshots: list[str] = Field(default_factory=list)
    render_stability: int | float | None = None
    content_relevance: int | float | None = None
    layout_harmony: int | float | None = None
    modernness_memorability: int | float | None = None
    token_adherence: int | float | None = None
    notes: str = ""


PhaseResultValue = Literal["pass", "fail", "skipped"]


class Grades(_Artifact):
    """`grade_round_N.json`，记录 evaluator 与视觉评分的综合结果。"""

    round: int = Field(ge=1)
    criteria: dict[str, Criterion]
    overall_passed: bool

    sprint: int | None = None
    mode_recommendation: str | None = None
    phase_results: dict[str, str] | None = None
    sprint_passed: bool | None = None
    regression_passed: bool | None = None
    target_exit_criteria_results: list[ExitCriterionResult] | None = None
    ui_checks: list[UICheck] | None = None
    appearance_review: AppearanceReview | None = None
    bugs_found: list[Any] = Field(default_factory=list)
    regressions_found: list[Any] = Field(default_factory=list)
    missing_features: list[Any] = Field(default_factory=list)
    repair_instructions: list[Any] = Field(default_factory=list)
    evaluation_infrastructure_failure: dict[str, Any] | None = None
    edit_guard: dict[str, Any] | None = None
    edit_scope_audit: str | None = None
    minimality_certificate: dict[str, Any] | None = None

    @classmethod
    def filename(cls, *, round_num: int, **params: Any) -> str:
        return f"grade_round_{round_num}.json"


class VisualManifest(_Artifact):
    """`visual_manifest_round_N.json`，记录截图清单与捕获元数据。"""

    round: int = Field(ge=1)
    app_url: str
    screenshots: list[str] = Field(default_factory=list)
    notes: str = ""

    @classmethod
    def filename(cls, *, round_num: int, **params: Any) -> str:
        return f"visual_manifest_round_{round_num}.json"


# ---- 跨轮状态 ---------------------------------------------------------------


class AcceptedSprints(_Artifact):
    """`accepted_sprints.json`，记录已验收 sprint 与当前目标。"""

    accepted: list[int] = Field(default_factory=list)
    current_target: int = Field(ge=0)
    last_evaluated_round: int = Field(ge=0)

    @classmethod
    def filename(cls, **params: Any) -> str:
        return "accepted_sprints.json"


class HarnessState(_Artifact):
    """`harness_state.json`，用于恢复执行的检查点文件。"""

    last_completed_phase: str | None = None
    round_num: int | None = None
    prompt: str | None = None
    costs: dict[str, float] | None = None
    phase_metrics: dict[str, dict[str, Any]] | None = None
    current_sprint: int | None = None
    generator_mode: str | None = None
    accepted_sprints: list[int] | None = None
    accepted_sprints_payload: dict[str, Any] | None = None
    last_verdict: str | None = None
    requested_design_mode: str | None = None
    design_mode: str | None = None
    design_status: str | None = None
    approved_concept_path: str | None = None
    background_ui_path: str | None = None
    timestamp: str | None = None

    @classmethod
    def filename(cls, **params: Any) -> str:
        return "harness_state.json"


# ---- 注册表 -----------------------------------------------------------------


ALL_ARTIFACT_MODELS: list[type[_Artifact]] = [
    DesignTokens,
    FeatureList,
    SprintPlan,
    UIVerificationPlan,
    DesignBrief,
    LayoutContract,
    AssetManifest,
    AcceptedSprints,
    Grades,
    VisualManifest,
    HarnessState,
]


__all__ = [
    "AcceptedSprints",
    "AppearanceReview",
    "Criterion",
    "DesignBrief",
    "DesignTokens",
    "AssetManifest",
    "ExitCriterionResult",
    "Feature",
    "FeatureList",
    "Grades",
    "HarnessState",
    "LayoutContract",
    "Sprint",
    "SprintPlan",
    "UICheck",
    "UIVerificationCheck",
    "UIVerificationPlan",
    "UIVerificationSprint",
    "VisualExperiment",
    "VisualManifest",
    "ALL_ARTIFACT_MODELS",
]
