# Image-First Design Integration Proposal

**English** | [简体中文](#中文版本)

## Summary

The current harness is text-first:

1. `planner` turns a short prompt into product and sprint artifacts.
2. `generator` reads those text artifacts and implements the frontend sprint by sprint.
3. `evaluator` and the dedicated visual scorer judge the generated frontend after code already exists.

This is a strong workflow for functional decomposition, but it leaves a gap in visual control:

- `design_tokens.json` constrains style, but it does not define a concrete composition.
- The generator still has to invent layout, focal hierarchy, and image treatment from text.
- The visual scorer is mostly a post-hoc reviewer, not a source of a stable visual target.

The explored image-first workflow addresses that gap:

1. Generate a complete overall design image.
2. Ask an agent to judge whether the concept is usable.
3. Generate a text-free background UI image from the accepted concept.
4. Have the coding agent build on top of that image so the generated visual can be embedded reliably in the final page.

This should be introduced as a new **Design** stage between `plan` and `build`, not as an ad-hoc trick inside the existing generator.

## Recommended Placement In The Harness

Current flow:

```text
plan -> build -> evaluate -> repair/build -> ...
```

Recommended flow:

```text
plan -> design -> build -> evaluate -> repair/build -> ...
```

The design stage should run once after planning succeeds and before Sprint 1 implementation starts.

Why this placement is preferable:

- Planner already defines product intent, style tokens, and sprint scope.
- The design stage can turn those textual constraints into a concrete visual target.
- Generator can then implement against both the textual contract and the visual contract.
- Evaluator can compare the coded page not only against functionality, but also against the approved visual reference.
- Checkpointing remains clean: `plan`, `design`, then the existing round-based `build_rN` / `evaluate_rN`.

Do **not** place this inside every generator round by default. Re-generating a base image every sprint would create style drift and make it hard to compare later rounds.

## Proposed New Artifacts

Store all new files under `.harness/design/`.

```text
.harness/design/
|-- concept_round_1.png
|-- concept_review_round_1.json
|-- approved_concept.png
|-- background_ui.png
|-- design_brief.json
|-- layout_contract.json
`-- asset_manifest.json
```

### `concept_round_N.png`

A full design proposal image generated from:

- `spec.md`
- `design_tokens.json`
- target device classes
- any explicit user visual request

It may contain representative text during ideation.

### `concept_review_round_N.json`

Structured review by a design-review agent:

```json
{
  "round": 1,
  "accepted": true,
  "scores": {
    "spec_alignment": 4,
    "visual_distinctiveness": 5,
    "layout_feasibility": 4,
    "implementation_feasibility": 4,
    "text_legibility_plan": 4
  },
  "strengths": [],
  "risks": [],
  "revision_instructions": []
}
```

This is the gate that prevents attractive but unusable concept art from becoming the implementation target.

### `approved_concept.png`

The accepted overall design image after concept review.

### `background_ui.png`

The text-free, implementation-ready background UI image.

It should preserve:

- composition
- imagery
- decorative structure
- major container geometry

It should remove:

- body text
- labels
- buttons containing generated text
- any baked-in typography that must remain editable or accessible in HTML

This image is the stable visual asset the frontend can actually embed.

### `design_brief.json`

The bridge between image generation and coding:

```json
{
  "visual_strategy": "image_backed_ui",
  "reference_files": {
    "approved_concept": ".harness/design/approved_concept.png",
    "background_ui": ".harness/design/background_ui.png"
  },
  "responsive_strategy": {
    "desktop": "background image plus semantic overlay controls",
    "mobile": "crop-safe central composition with stacked overlay controls"
  },
  "overlay_regions": [
    {
      "id": "hero_title",
      "kind": "text",
      "bounds_hint": "top-left third",
      "priority": "high"
    }
  ],
  "implementation_rules": [
    "Keep user-visible text in HTML, not baked into the image.",
    "Use the background image as a visual layer, not as a replacement for semantic UI.",
    "All interactive controls must remain native HTML elements."
  ]
}
```

### `layout_contract.json`

More explicit machine-readable geometry and component intent:

```json
{
  "viewport_targets": ["1440x900", "390x844"],
  "regions": [
    {
      "id": "primary_panel",
      "role": "main_content",
      "desktop_bounds": {"x": 0.08, "y": 0.12, "w": 0.42, "h": 0.54},
      "mobile_behavior": "full_width_stack"
    }
  ],
  "safe_zones": [],
  "forbidden_overlay_zones": [],
  "asset_fit": {
    "background_ui": "cover_desktop_contain_mobile"
  }
}
```

This is useful because the generator should not infer all spatial relationships from pixels alone.

### `asset_manifest.json`

Tracks image assets and expected usage:

```json
{
  "assets": [
    {
      "id": "background_ui",
      "path": ".harness/design/background_ui.png",
      "usage": "full_bleed_background",
      "required": true
    }
  ]
}
```

## Proposed New Agents

### 1. `design_synthesizer`

Responsibilities:

- Read planning artifacts.
- Generate one or more concept images.
- Record prompt metadata and seed metadata when available.

Inputs:

- `spec.md`
- `design_tokens.json`
- optional external image references

Outputs:

- `concept_round_N.png`

### 2. `design_reviewer`

Responsibilities:

- Decide whether a concept is actually adoptable.
- Reject images that are beautiful but unusable, text-heavy, inaccessible, or impossible to implement responsively.

Inputs:

- `concept_round_N.png`
- `spec.md`
- `design_tokens.json`

Outputs:

- `concept_review_round_N.json`

Gate:

- Stop after a small bounded number of retries.
- If no concept is accepted, fall back to the current text-first pipeline rather than deadlock the run.

### 3. `background_ui_synthesizer`

Responsibilities:

- Convert the accepted concept into a text-free visual substrate.
- Produce the actual image asset intended for embedding in the frontend.

Inputs:

- `approved_concept.png`
- `design_tokens.json`
- text-removal instructions

Outputs:

- `background_ui.png`

### 4. `design_contract_builder`

Responsibilities:

- Convert the approved image into structured implementation guidance.
- Produce `design_brief.json`, `layout_contract.json`, and `asset_manifest.json`.

This stage may be combined with `design_reviewer` initially to keep the first implementation smaller.

## Generator Integration

The generator should remain responsible for code, not image ideation.

In `generate` mode, add required reads:

- `.harness/design/design_brief.json`
- `.harness/design/layout_contract.json`
- `.harness/design/asset_manifest.json`

The prompt should add instructions like:

```text
Use the approved image-first design contract when implementing the frontend.
Preserve the composition of `.harness/design/background_ui.png`.
Overlay editable HTML text and interactive controls according to `layout_contract.json`.
Do not rasterize functional text or controls into the background image.
```

For file placement, the generator should copy or reference approved assets into `frontend/public/assets/` or `frontend/src/assets/`.

Important constraint:

- The background image should support the interface, not replace the interface.
- Accessibility, editable content, runtime state, and interactivity must remain DOM-native.

## Evaluator And Visual Scorer Integration

The current evaluator already checks:

- functionality through Playwright
- screenshots through a dedicated visual scorer

Extend the dedicated visual review with reference-aware criteria:

```json
{
  "criteria_scores": {
    "design_quality": {},
    "originality": {},
    "craft": {},
    "reference_fidelity": {},
    "overlay_integrity": {},
    "responsive_preservation": {}
  }
}
```

Recommended new checks:

1. **Reference fidelity**
   - Does the coded page preserve the approved concept's composition, palette, and visual hierarchy?

2. **Overlay integrity**
   - Are text and controls cleanly layered over the background image without visible collision or illegible overlap?

3. **Responsive preservation**
   - Does the design still hold together at mobile and desktop screenshot sizes?

4. **Semantic UI preservation**
   - Did the generator avoid baking important text or controls into the raster asset?

The visual scorer should compare:

- `approved_concept.png`
- `background_ui.png`
- generated page screenshots
- `design_tokens.json`
- `layout_contract.json`

## Why The Background-UI Step Matters

Generating only one polished overall mockup is not enough.

If the mockup already contains text:

- embedded text becomes blurry or wrong when cropped
- localization becomes impossible
- semantic HTML is lost
- the generator may duplicate text on top of rasterized text

The text-free background step is the key practical insight in the explored workflow:

- it preserves AI-generated richness
- it keeps text editable
- it makes the image easier to embed stably
- it lets the agent implement real controls on top of a stable visual substrate

This is the part that differentiates the workflow from a generic "generate a mockup first" idea.

## Recommended Control Flow

```text
PHASE 1: PLAN
  planner -> text artifacts

PHASE 1.5: DESIGN
  design_synthesizer -> concept image
  design_reviewer -> accept / reject
  if rejected and retries remain:
      revise concept
  if accepted:
      background_ui_synthesizer -> text-free background
      design_contract_builder -> brief + layout contract + asset manifest
  if no concept accepted:
      mark design mode = fallback_text_only

PHASE 2+: BUILD / EVALUATE
  generator consumes both plan artifacts and design artifacts
  evaluator checks behavior
  vision scorer checks visual quality plus reference fidelity
```

## Fallbacks And Failure Policy

The design stage should not make the harness brittle.

Recommended fallback behavior:

1. Image generation unavailable:
   - continue with existing text-first flow
   - write `design_mode = "text_only_fallback"`

2. Concept rejected repeatedly:
   - fall back after a bounded retry count
   - preserve rejection metadata for analysis

3. Background extraction fails:
   - use the approved concept only as a reference
   - do not embed a text-heavy image directly into production UI

4. Generator cannot honor the image-backed layout:
   - evaluator should prefer functional correctness over exact visual fidelity for blocking sprint acceptance
   - visual penalties should inform repair without making every layout mismatch fatal

## Suggested Checkpoint Changes

Add one new checkpoint phase:

```text
design
```

New state fields:

```json
{
  "design_mode": "image_backed_ui",
  "design_status": "accepted",
  "approved_concept_path": ".harness/design/approved_concept.png",
  "background_ui_path": ".harness/design/background_ui.png"
}
```

Resume behavior:

- If `design` checkpoint exists, skip regeneration and reuse the approved assets.
- If only `concept_round_N.png` exists without an accepted checkpoint, resume review rather than blindly regenerating everything.

## Suggested Benchmarks And Ablations

The project already integrates WebGen-Bench, but WebGen-Bench does not directly measure reference-image fidelity.

Recommended evaluation setup:

### Baseline

```text
text-only planner + generator
```

### Variant A

```text
text-only plan + approved concept reference
```

### Variant B

```text
text-only plan + approved concept + text-free background UI + layout contract
```

### Compare

- first-round acceptance rate
- number of repair rounds
- visual appearance grade
- reference fidelity score
- manual preference judgments
- UI task success rate
- implementation cost

The most important comparison is not only whether the page looks better, but whether the image-first path reduces ambiguity enough to lower repair cost while preserving functional success.

## Implementation Roadmap

### Phase 1: Documentation And Artifact Contract

- Add this design document.
- Define schemas for the new design artifacts.
- Extend `FileComm` with typed read/write helpers for design artifacts.

### Phase 2: Minimal Image-First Prototype

- Add `run_design_stage()`.
- Use one concept image, one acceptance review, one background image.
- Gate it behind a CLI flag such as `--design-mode image-first`.

### Phase 3: Generator Consumption

- Add new required reads to generator prompt building.
- Copy approved assets into the frontend project.
- Add tests proving the generator prompt exposes the design contract.

### Phase 4: Visual Evaluation

- Extend the visual scorer prompt and schema with reference-aware metrics.
- Capture desktop and mobile screenshots for comparison.

### Phase 5: Experimentation

- Run ablations on a small task subset first.
- Only then decide whether image-first should become the default path.

## Risks

1. **Overfitting to beautiful static shots**
   - Risk: visually impressive pages with poor interaction.
   - Mitigation: keep functionality checks blocking; visual checks should not replace them.

2. **Responsive mismatch**
   - Risk: a strong desktop concept fails on mobile.
   - Mitigation: generate both desktop and mobile references, or require crop-safe composition.

3. **Baked-in semantics**
   - Risk: text and buttons become pixels.
   - Mitigation: require `background_ui.png` to be text-free and keep controls in HTML.

4. **Cost growth**
   - Risk: extra image generation and VLM passes raise run cost.
   - Mitigation: bounded retries, caching, optional mode flag, and ablation before defaulting on.

5. **Concept drift across sprints**
   - Risk: later work drifts away from the accepted visual identity.
   - Mitigation: reuse one approved reference set across all sprints unless an explicit redesign event occurs.

## Recommendation

Introduce the workflow as an **optional image-first design mode** first:

```text
--design-mode text-only        # current behavior
--design-mode image-first      # new path
```

Do not replace the current pipeline immediately.

The explored workflow is most valuable when:

- visual identity is central to the task
- the page benefits from rich imagery or an art-directed hero
- text-only prompting repeatedly produces generic layouts

It is less necessary for:

- utilitarian dashboards
- dense admin tools
- tasks where visual polish is secondary to information architecture

The strongest contribution is not "AI can generate a pretty image."
The stronger idea is:

> Use an accepted concept image to anchor design intent, then derive a text-free background UI asset that can be embedded stably while the frontend agent keeps semantics, text, and interactions in code.

That gives the harness a concrete visual target without sacrificing the existing sprint-based engineering discipline.

---

# 中文版本

## 摘要

当前 harness 采用的是 text-first 工作流：

1. `planner` 将简短提示扩展为产品与 Sprint 产物。
2. `generator` 读取这些文本产物，并按 Sprint 逐步实现前端。
3. `evaluator` 与独立视觉评分器在代码已经生成之后，对结果进行判断。

这套流程很适合做功能拆解，但在视觉控制上仍有缺口：

- `design_tokens.json` 能约束风格，却不能定义具体构图。
- generator 仍需仅凭文字自行发明布局、视觉焦点与图像处理方式。
- 视觉评分器更多是事后审阅者，而不是稳定视觉目标的来源。

探索中的 image-first 工作流正是为了解决这个缺口：

1. 先生成完整的整体设计图。
2. 再由 agent 判断这个概念图是否可采用。
3. 从通过审查的概念图生成无文字的 `background_ui.png`。
4. 让编码 agent 在这张稳定视觉底图之上实现真实页面，从而使图像资产可以可靠嵌入最终页面。

这个能力更适合作为 `plan` 与 `build` 之间新增的 **Design** 阶段，而不是塞进现有 generator 里的临时技巧。

## 在 Harness 中的推荐位置

当前流程：

```text
plan -> build -> evaluate -> repair/build -> ...
```

推荐流程：

```text
plan -> design -> build -> evaluate -> repair/build -> ...
```

Design 阶段应在规划完成之后、Sprint 1 开始实现之前只运行一次。

这样放置更合理，因为：

- Planner 已经给出了产品意图、风格 token 和 Sprint 范围。
- Design 阶段可以把这些文字约束转换成具体视觉目标。
- Generator 随后可以同时依据文本契约和视觉契约实现页面。
- Evaluator 不仅可以检查功能，也可以对照已批准的视觉参考判断实现结果。
- Checkpoint 仍然清晰：`plan`、`design`，之后继续沿用现有基于轮次的 `build_rN` / `evaluate_rN`。

默认情况下，**不要**在每一轮 generator 中重新生成基础图像。否则会造成视觉漂移，也会让后续轮次很难进行一致比较。

## 建议新增的产物

所有新增文件都放在 `.harness/design/` 下。

```text
.harness/design/
|-- concept_round_1.png
|-- concept_review_round_1.json
|-- approved_concept.png
|-- background_ui.png
|-- design_brief.json
|-- layout_contract.json
`-- asset_manifest.json
```

### `concept_round_N.png`

一张完整设计提案图，生成依据包括：

- `spec.md`
- `design_tokens.json`
- 目标设备类型
- 用户显式提出的视觉要求

在概念探索阶段，它可以包含代表性文字。

### `concept_review_round_N.json`

由设计审查 agent 输出的结构化评审：

```json
{
  "round": 1,
  "accepted": true,
  "scores": {
    "spec_alignment": 4,
    "visual_distinctiveness": 5,
    "layout_feasibility": 4,
    "implementation_feasibility": 4,
    "text_legibility_plan": 4
  },
  "strengths": [],
  "risks": [],
  "revision_instructions": []
}
```

这个 gate 的作用，是防止“好看但不可用”的概念图直接变成实现目标。

### `approved_concept.png`

通过概念审查后的整体设计图。

### `background_ui.png`

无文字、可直接用于实现的背景 UI 图。

它应保留：

- 构图
- 图像元素
- 装饰结构
- 主要容器几何关系

它应移除：

- 正文文字
- 标签
- 含有生成文字的按钮
- 任何必须在 HTML 中保持可编辑、可访问的内嵌排版

这张图才是前端最终可以稳定嵌入的视觉资产。

### `design_brief.json`

连接图像生成与代码实现的桥梁：

```json
{
  "visual_strategy": "image_backed_ui",
  "reference_files": {
    "approved_concept": ".harness/design/approved_concept.png",
    "background_ui": ".harness/design/background_ui.png"
  },
  "responsive_strategy": {
    "desktop": "background image plus semantic overlay controls",
    "mobile": "crop-safe central composition with stacked overlay controls"
  },
  "overlay_regions": [
    {
      "id": "hero_title",
      "kind": "text",
      "bounds_hint": "top-left third",
      "priority": "high"
    }
  ],
  "implementation_rules": [
    "Keep user-visible text in HTML, not baked into the image.",
    "Use the background image as a visual layer, not as a replacement for semantic UI.",
    "All interactive controls must remain native HTML elements."
  ]
}
```

### `layout_contract.json`

更明确的机器可读几何与组件意图：

```json
{
  "viewport_targets": ["1440x900", "390x844"],
  "regions": [
    {
      "id": "primary_panel",
      "role": "main_content",
      "desktop_bounds": {"x": 0.08, "y": 0.12, "w": 0.42, "h": 0.54},
      "mobile_behavior": "full_width_stack"
    }
  ],
  "safe_zones": [],
  "forbidden_overlay_zones": [],
  "asset_fit": {
    "background_ui": "cover_desktop_contain_mobile"
  }
}
```

这样做的意义在于：generator 不应该只靠像素反推全部空间关系。

### `asset_manifest.json`

追踪图像资产及其预期用途：

```json
{
  "assets": [
    {
      "id": "background_ui",
      "path": ".harness/design/background_ui.png",
      "usage": "full_bleed_background",
      "required": true
    }
  ]
}
```

## 建议新增的 Agents

### 1. `design_synthesizer`

职责：

- 读取规划产物。
- 生成一张或多张概念图。
- 在可用时记录 prompt 与 seed 元数据。

输入：

- `spec.md`
- `design_tokens.json`
- 可选的外部图像参考

输出：

- `concept_round_N.png`

### 2. `design_reviewer`

职责：

- 判断概念图是否真正可采用。
- 拒绝那些虽然好看，但文字过重、不可访问、无法响应式实现或工程上不可落地的图像。

输入：

- `concept_round_N.png`
- `spec.md`
- `design_tokens.json`

输出：

- `concept_review_round_N.json`

Gate：

- 只允许少量有界重试。
- 如果始终没有概念图通过，则回退到当前 text-first 流程，而不是让运行陷入死锁。

### 3. `background_ui_synthesizer`

职责：

- 将已通过的概念图转换成无文字的视觉底图。
- 产出真正用于嵌入前端的图像资产。

输入：

- `approved_concept.png`
- `design_tokens.json`
- 去文字说明

输出：

- `background_ui.png`

### 4. `design_contract_builder`

职责：

- 将已批准的图像转换成结构化实现说明。
- 产出 `design_brief.json`、`layout_contract.json` 与 `asset_manifest.json`。

在首版实现中，这一步可以先与 `design_reviewer` 合并，以减小工程体量。

## Generator 集成

Generator 仍应负责写代码，而不是负责图像构思。

在 `generate` 模式下，新增必读文件：

- `.harness/design/design_brief.json`
- `.harness/design/layout_contract.json`
- `.harness/design/asset_manifest.json`

Prompt 中应加入类似说明：

```text
Use the approved image-first design contract when implementing the frontend.
Preserve the composition of `.harness/design/background_ui.png`.
Overlay editable HTML text and interactive controls according to `layout_contract.json`.
Do not rasterize functional text or controls into the background image.
```

在文件放置上，generator 应将批准后的资产复制或引用到 `frontend/public/assets/` 或 `frontend/src/assets/`。

重要约束：

- 背景图应支撑界面，而不是替代界面。
- 可访问性、可编辑内容、运行时状态与交互能力都必须保持 DOM 原生。

## Evaluator 与视觉评分器集成

当前 evaluator 已经会检查：

- 通过 Playwright 进行功能验证
- 通过独立视觉评分器审阅截图

应将专用视觉审查扩展为带参考图的评估标准：

```json
{
  "criteria_scores": {
    "design_quality": {},
    "originality": {},
    "craft": {},
    "reference_fidelity": {},
    "overlay_integrity": {},
    "responsive_preservation": {}
  }
}
```

建议新增检查：

1. **Reference fidelity**
   - 编码后的页面是否保留了已批准概念图的构图、配色与视觉层级？

2. **Overlay integrity**
   - 文本与控件是否干净地叠放在背景图之上，没有碰撞或难以辨认的情况？

3. **Responsive preservation**
   - 在移动端与桌面端截图尺寸下，设计是否仍然成立？

4. **Semantic UI preservation**
   - generator 是否避免把重要文字与控件烘焙进位图资产？

视觉评分器应对比：

- `approved_concept.png`
- `background_ui.png`
- 页面截图
- `design_tokens.json`
- `layout_contract.json`

## 为什么 `background_ui` 这一步很关键

只生成一张精美整体 mockup 并不够。

如果 mockup 本身已经带文字：

- 裁切后文字会变糊或出错
- 无法做本地化
- 语义化 HTML 丢失
- generator 可能会在位图文字上再次叠加真实文字

无文字背景图这一步，是该工作流中最关键的实践洞察：

- 保留 AI 生成图像的丰富度
- 让文字继续可编辑
- 让图像更容易稳定嵌入
- 让 agent 仍可在稳定底图之上实现真实控件

这正是它区别于普通“先生成一张 mockup”方案的地方。

## 推荐控制流

```text
PHASE 1: PLAN
  planner -> text artifacts

PHASE 1.5: DESIGN
  design_synthesizer -> concept image
  design_reviewer -> accept / reject
  if rejected and retries remain:
      revise concept
  if accepted:
      background_ui_synthesizer -> text-free background
      design_contract_builder -> brief + layout contract + asset manifest
  if no concept accepted:
      mark design mode = fallback_text_only

PHASE 2+: BUILD / EVALUATE
  generator consumes both plan artifacts and design artifacts
  evaluator checks behavior
  vision scorer checks visual quality plus reference fidelity
```

## 回退与失败策略

Design 阶段不应让 harness 变得脆弱。

建议的回退行为：

1. 图像生成不可用：
   - 继续现有 text-first 流程
   - 写入 `design_mode = "text_only_fallback"`

2. 概念图反复被拒绝：
   - 在有界重试后回退
   - 保留拒绝元数据用于分析

3. 背景抽取失败：
   - 只把已批准概念图当作参考
   - 不要把含大量文字的图像直接嵌入生产 UI

4. Generator 无法遵循 image-backed 布局：
   - 阻塞性 Sprint 验收应优先保证功能正确
   - 视觉惩罚可以指导 repair，但不应让每一个布局偏差都变成致命错误

## 建议的 Checkpoint 变更

新增一个 checkpoint phase：

```text
design
```

新增状态字段：

```json
{
  "design_mode": "image_backed_ui",
  "design_status": "accepted",
  "approved_concept_path": ".harness/design/approved_concept.png",
  "background_ui_path": ".harness/design/background_ui.png"
}
```

恢复行为：

- 如果存在 `design` checkpoint，则跳过重新生成并复用已批准资产。
- 如果只存在 `concept_round_N.png`，却没有已通过的 checkpoint，则应从 review 继续，而不是盲目全部重做。

## 建议的 Benchmark 与消融实验

项目已经接入 WebGen-Bench，但 WebGen-Bench 并不直接衡量参考图保真度。

建议的评估设置：

### Baseline

```text
text-only planner + generator
```

### Variant A

```text
text-only plan + approved concept reference
```

### Variant B

```text
text-only plan + approved concept + text-free background UI + layout contract
```

### 对比指标

- 首轮通过率
- repair 轮数
- 视觉外观评分
- 参考图保真评分
- 人工偏好判断
- UI 任务成功率
- 实现成本

最重要的对比不只是页面是否更好看，而是 image-first 路径是否足以降低歧义、减少 repair 成本，同时仍保持功能成功率。

## 实施路线图

### Phase 1: 文档与产物契约

- 添加本文档。
- 定义新的 design artifacts schema。
- 为 design artifacts 扩展 `FileComm` 的类型化读写 helper。

### Phase 2: 最小 Image-First 原型

- 增加 `run_design_stage()`。
- 使用一张概念图、一次通过审查、一张背景图。
- 通过 `--design-mode image-first` 这样的 CLI flag 进行开关控制。

### Phase 3: Generator 消费能力

- 为 generator prompt 构造增加新的必读文件。
- 将已批准资产复制进前端项目。
- 增加测试，证明 generator prompt 已暴露 design contract。

### Phase 4: 视觉评估

- 为视觉评分器 prompt 与 schema 增加 reference-aware 指标。
- 同时采集桌面端与移动端截图用于比对。

### Phase 5: 实验

- 先在一小批任务上做消融。
- 再决定是否将 image-first 变成默认路径。

## 风险

1. **过度拟合漂亮静态图**
   - 风险：页面很好看，但交互很差。
   - 缓解：保持功能检查为阻塞项，视觉检查不能取代它。

2. **响应式不匹配**
   - 风险：桌面端概念图很强，但移动端失败。
   - 缓解：生成桌面端与移动端两套参考，或要求构图在裁切上足够安全。

3. **语义被烘焙进位图**
   - 风险：文字和按钮都变成像素。
   - 缓解：要求 `background_ui.png` 无文字，并将控件保留在 HTML 中。

4. **成本上升**
   - 风险：额外图像生成和 VLM 调用提高运行成本。
   - 缓解：有界重试、缓存、可选模式开关，以及在默认启用前先做消融。

5. **Sprint 之间概念漂移**
   - 风险：后续工作逐渐偏离已批准的视觉身份。
   - 缓解：除非发生显式 redesign 事件，否则所有 Sprint 复用同一套已批准参考资产。

## 建议

先把该工作流作为**可选 image-first design mode** 引入：

```text
--design-mode text-only        # 当前行为
--design-mode image-first      # 新路径
```

不要立刻替换现有 pipeline。

该工作流最有价值的场景：

- 视觉身份是任务核心
- 页面需要丰富图像或经过 art direction 的 hero
- text-only prompting 反复产出泛化布局

不太需要它的场景：

- 实用型 dashboard
- 高密度后台工具
- 视觉 polish 次于信息架构的任务

真正最有价值的贡献，不是“AI 能生成漂亮图片”。
更强的想法是：

> 用通过审查的概念图锚定设计意图，再派生出无文字背景 UI 资产，使其能够稳定嵌入；与此同时，前端 agent 仍然用代码维护语义、文字与交互。

这样，harness 就能获得一个具体视觉目标，同时不牺牲现有基于 Sprint 的工程纪律。
