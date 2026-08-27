# Web Coding Agent

[English](README.md) | **简体中文**

本仓库是对 [Anthropic 长时运行 harness 设计工作](https://www.anthropic.com/engineering/harness-design-long-running-apps)中**前端部分**的简易复现。

当前实现刻意保持**仅前端**：

- `planner`：将一段简短提示扩展为雄心勃勃的产品规格与 Sprint 计划
- 可选的 `design`：当 `design_mode=image-first` 时插入在 planning 与 build 之间，产出设计契约，并在配置完整时生成图像参考资产
- `generator`：在 `workdir/frontend` 下逐 Sprint 构建浏览器端前端应用，模式可为 `generate` 或 `repair`
- `evaluator`：使用 Playwright MCP 对运行中的前端进行功能性测试
- 独立的视觉打分器（vision scorer）审阅截图并覆盖外观相关评分

当前 harness **不包含后端生成与后端运行时**。

## 项目职责

本仓库是独立的**正向 agentic 数据 producer**。源码、测试、prompt 与 exporter
在这里单独版本化，运行产物默认留在 Git 之外：

- `./runs/agentic/`：任务 workdir、checkpoint、trace、截图和导出轨迹
- `./logs/agentic/`：launcher、API probe 与 seed 同步的持久化日志

需要把大规模产物放到外部数据盘时，设置 `WEB_CODING_DATA_ROOT`。数据抓取、
逆向/受控构造与 release 组装可以保留在其他仓库；双方通过显式路径和 schema
衔接，不再依赖固定的父子或同级目录布局。

### 当前 Edit 主线数据目标

数据生产的基本单元是一次已验收状态转移：
`accepted S_k + 本轮增量指令 → accepted S_(k+1)`。一次 Edit 固定为一个 Sprint，
即使这项连贯改动同时涉及多个页面或文件；新的 accepted checkpoint 会成为下一轮 Seed。
从零任务先得到第一个 accepted checkpoint，之后同样沿 Edit 主线继续。

Generate 和 Repair 从这条历史派生，而不是各跑一套流水线：累计到任一 checkpoint 的
需求派生 `checkpoint_generate`；累计完整需求到终态派生 `complete_generate`；同一 Edit
Sprint 中有真实浏览器证据的失败版本到后续恢复派生 `natural_repair`。三者共享 lineage，
并按角色分开统计。

显式 Edit 默认最多 10 个 build/evaluate 循环，但通过即停，不要求凑满 10 轮。失败轮只有
在存在可定位证据时才进入下一次 Repair，文件和行数范围由 harness 动态给出，避免开放式
探索。目标覆盖多页、多文件和多模态任务。单元测试通过只证明机械约束生效；真实模型质量
与成本仍需小样本校准。

## 状态

已实现的能力：

- 基于 Claude Agent SDK 的执行
- Planner / 可选 Design Stage / Generator / Evaluator 流水线
- 基于 Sprint 的推进，支持 `generate` / `repair` 两种生成模式
- Sprint 大小硬约束（默认每个 Sprint ≤10 deliverables 与 ≤10 exit_criteria，可配置且由 validator 强制），既限制无边界任务，又不为满足重试规则而拆散一个完整的多页功能
- 仅前端运行时管理
- 可选的 image-first 设计阶段，可写出 `design_brief.json`、`layout_contract.json` 与 `asset_manifest.json`
- 可选的设计图生成能力，可产出 `approved_concept.png` 与 `background_ui.png`，图像资产缺失时自动回退为仅文本设计契约
- 基于 Playwright MCP 的功能性评估
- 评估器拥有只读 Bash（可 `cat`/`grep`/`python3 -m json.tool` 读取产物，但不能修改源码）
- DOM/ARIA/内部状态优先的确定性评估；只有视觉类别或图片输入才进入截图/视觉模型
- 付费 evaluator 格式错误不自动二次调用；视觉重试默认是 0，只有明确授权配置后才启用
- 跨 plan / build / evaluate 各阶段的恢复 / 检查点支持；进程中断后可从 trace 验证并恢复已完成的 Planner 产物或模型已写出的根 Generate 源码，不要求模型重写有效结果
- 为基于 SDK 的 agent 调用记录 JSONL trace
- 为基于 SDK 的 agent 调用生成 Claude HTTP trace 配套文件：`*.http.jsonl` 保持为源 trace，旁边生成 `*.http.html` 供浏览器查看
- 前端运行时失败的本地日志
- 按阶段记录成本，分别执行 planner/generator/evaluator 累计上限，并设有总预算硬上限；失败或中断调用的 append-only trace 用量会计入恢复后的阶段累计值，不会在 resume 时清零
- 增量 Edit 的 DOM 契约保护：显式 Edit 从已验收 seed 建立基线；Generate 的第二个及以后 Sprint 从上一已验收 checkpoint 建立逐路由语义 DOM/ARIA 基线。每个基线双采样，不稳定则 fail closed；v4 contract 只开放目标 selector 对应的最深 fragment（每目标路由最多四个），同一 `main` 内的兄弟 fragment 和所有非目标路由仍受保护；检查独立于截图/像素评分。
- harness 主导的最小路径引导：每条 UI check 必须声明站内 route。harness 根据静态 HTML 页面、具体的文件系统路由、显式 React Router 映射、源码热点和 import/link 边建立页面归属及 change cone；目标路由本地文件优先开放，非目标文件保持关闭。若多页共用一个源码文件，只有能由字面路由机械定位出的目标页顶层对象/类/函数区域可以开放；每个 exact patch 必须完全位于该区域内，同文件兄弟页面模块与整文件覆盖继续被拒绝，所有结果写入 append-only ledger。
- 0805 全 40 类 Edit 的 typed browser contract：新计划禁止任意 `evaluate` JavaScript 与 hash-router URL 状态，每条 flow 必须以一至四个相关且有界的 DOM/text/value/count/pathname/attribute/ARIA/focus/storage/console assertions 结束。Tab 检查必须给出确定的起始 selector，初始空状态检查必须排在同一路由的写状态流程之前。真实 Chromium 还执行 hover、右键、拖拽、内存文件上传、异步 selector 等待、reload 和 print/color-scheme 媒体模拟。历史 `evaluate` 只可回放，不能进入新正式出口。
- accepted checkpoint tape：通过的 typed flow 按 requirement/impact/Sprint/round 追加保存。普通 Edit 只重放受影响检查，并给每个受保护路由保留一个关键 sentinel；每第 5 个 accepted Edit 与缺少新元数据的历史 tape 做全量重放。已验收行为丢失是具体 regression，tape 损坏或超预算属于基础设施失败。
- 反事实最小性证书：功能性 patch atom 在隔离的真实浏览器候选中逐个删除；目标局部 CSS 还必须由已通过的目标路由视觉复核覆盖，因为功能 oracle 无法判断视觉必要性。若失败只来自证据策略更新，后续 round 保持源码逐字节不变，并链接上一次真正 applied + validated 的 mutation ledger，不再让 Generator 为评分器修复而碰代码。新策略证书必须精确记录 source/destination（Repair 还记录真实 failure round）。
- AIR 单次任务生成完整读取最多 48 个源码文件/140K 字符且不静默截断；更大项目明确拒绝并转入工具读取路径，不会把部分上下文伪称为 `all_files_included`。
- 一等 Edit 入口：`--task-mode edit` 冻结干净的既有 frontend Git 基线，并物化一张紧凑 Edit card，保存精确增量指令、add/refine/replace/withdraw 需求关系、影响标签、目标路由/检查、冲突与视觉策略。连贯的多页/多文件 Edit 仍是一个 Sprint，独立改动在上游拆分。
- 证据驱动 Repair：确定性失败跳过付费语义 judge，直接写 `repair_packet_round_N.json`；下一轮只接收失败检查/回归、证据引用、允许源码路径和动态文件/行数预算。无法定位的失败不会启动开放式 Repair。
- 原生 trajectory 对新增文件使用显式 `operation=create_file` 与 `content`，不再用空 `search` 冒充 patch；WebCompass v2 继续只接收唯一 search/replace，并明确跳过这类原生新增文件记录。
- 多类型用户输入：重复使用 `--input` 可加入 PNG/JPEG/WebP/GIF 或受限大小的文本/源码文件。输入按内容哈希暂存在 `.harness/inputs/`；图片会作为真正的多模态消息送入 Planner、Generator 和视觉复核，并进入 image-edit v2 导出。
- 通用并发 `scripts/run_batch.py`：独立端口、单 case 超时、逐条 append 状态、成功 case 断点跳过、可选 verified seed、Edit 路由与多模态输入。
- `scripts/export_run_folders.py`：只消费严格 trajectory exporter 已接受的记录，生成人工可读目录，不再从 commit/Sprint 猜测正式任务类型。
- 严格 Edit-first 谱系：相邻 accepted checkpoint 是 canonical Edit；`checkpoint_generate` 与 `complete_generate` 是累计需求的派生视图；真实失败到同 Sprint 恢复是 `natural_repair`。正式 Edit/Repair 还必须具备稳定 v4 fragment scope、applied + validated 最小路径 ledger、`certified` 反事实证书、typed accepted tape 与可精确重放 patch；JSONL 只追加且可断点幂等。

### Edit 事务、Generate checkpoint 与回归保护

Generate 的 Sprint 1 从零建立第一个已验收 checkpoint；Sprint 2 及以后在修改前
冻结上一 checkpoint，并写入 `.harness/edit_dom_source_sprint_N.json`。因此低层
generator 即使仍显示 `mode=generate`，其 `trajectory_role` 也是
`incremental_edit`，会经过相同的 change cone、DOM/ARIA 和 minimality 门禁。

外部 Edit 可以由 `scripts/prepare_forward_edit_seed.py` 创建的 `seed_manifest.json`
触发，也可以通过 `--task-mode edit` 显式启动。显式 Edit 要求
`workdir/frontend` 干净，会冻结/核验 Git baseline 并写出
`.harness/edit_task_contract.json`。首次 edit build 前，harness 会启动该 seed 并写入
`.harness/edit_dom_baseline.json`：其中是 landmark、role、`data-testid` root 与
语义控件的 DOM/ARIA 指纹，也包含可聚焦控件是否确实能获得键盘焦点和稳定的后代 anchor；不是截图。
generator 启动前，harness 会结合可执行 action selector 与源码依赖边写入
`.harness/minimal_path_plan_round_N.json` 和 harness 自己持有的
`.harness/edit_scope_round_N.json`，例如：

```json
{"owner":"harness","allowed_root_keys":["main:unnamed"],"allow_new_roots":false}
```

模型不能修改这两个策略文件。已有源码只能用唯一匹配的 exact patch 修改；整文件覆盖、
越出 change cone、过宽 patch 以及 Bash 文件/依赖写操作会在执行前被拒绝。依赖文件只有
在 plan 中存在明确 import/link 边时才可进入范围；决策写入
`.harness/minimal_path_ledger_round_N.jsonl`。

显式 Edit 的 Planner 只允许一个 Sprint，并写出 `.harness/edit_card.json`。同一项
产品语义需要同时修改两个页面或多个文件时仍是一个事务；彼此独立的需求由上游拆开。
默认 `EDIT_MAX_ROUNDS=10` 只是失败恢复上限，通过后立即退出。每个失败轮必须写出
可定位的 `repair_packet_round_N.json`，否则不会继续让模型开放式排查。

多路由契约按 Edit 收窄：显式声明的多个目标路由只是全局上界；当前事务只开放其
typed browser checks 覆盖的页面，其他页面仍受保护。路由无法解析或 planner
漂移到未授权页面时，在修改源码前失败。
Stop gate 还会把最终 Git diff 与 ledger 中实际成功的 mutation 对账，阻止构建脚本或
provider 特殊工具间接改动受保护源码。

DOM 契约最多允许两个已命名 baseline surface 内发生变化；其他 surface 被删除或语义变化，
或未授权新增 surface，都会作为 regression 使该轮失败，并写入
`grade_round_N.json::edit_guard`。该门禁约束 edit 的边界；它不能替代正常 browser
evaluator 对需求是否真正实现的验证。

### 图片与其他任务输入

```bash
uv run harness "只在商品页增加与参考图一致的筛选器" \
  --workdir ./runs/agentic/catalog-edit \
  --task-mode edit \
  --target-route /catalog \
  --input ./references/filter.png \
  --input ./references/requirements.md \
  --playwright-headless
```

输入清单位于 `.harness/task_inputs.json`，记录 SHA-256、媒体类型、大小和暂存路径。
图片单文件上限 20 MiB，文本/源码单文件 2 MiB，总输入 50 MiB。图片不是仅以路径
写入 prompt，而是 provider-native image block；视觉复核会明确区分用户参考图和
harness 自己渲染的页面截图。

### 通用批跑与人工目录导出

批跑 JSONL 可包含 `id`、`prompt`、`task_mode`、`inputs`、`target_routes`，Edit
还可包含成对的 `seed_frontend` / `seed_evaluation`：

```bash
uv run python scripts/run_batch.py ./tasks.jsonl \
  --output-dir ./runs/agentic/batch-001 \
  --results ./runs/agentic/batch-001/results.jsonl \
  --workers 4 --base-port 6100 --timeout-seconds 1800
```

每个 case 结束即 append `status=ok|incomplete|timeout|error` 并落盘；重跑只跳过
最新状态为 `ok` 的 ID。`incomplete` 表示 harness 正常返回但尚未获得 completed
verdict，应使用 `--resume-harness` 续跑 checkpoint。人工查看目录必须从严格 exporter 生成：

```bash
uv run python scripts/export_run_folders.py \
  --run-dir ./runs/agentic/catalog-edit \
  --output-dir ./runs/agentic/catalog-edit-review
```

该工具不覆盖已有目录，也不自行推断 Edit/Repair 标签。

## 环境要求

- Python `>=3.11`
- `uv`
- Node.js + npm
- `.env` 或环境变量中存在 `ANTHROPIC_API_KEY`

Playwright MCP 在评估阶段通过 `npx` 启动，因此机器上必须可用 Node/npm。

如果使用 `DESIGN_MODE=image-first`，且希望设计阶段自动生成新的栅格图资产，还需要提供 `DESIGN_IMAGE_API_KEY`。若该变量缺失，设计阶段仍会执行并写出文本设计契约，但会回退到 `text_only_fallback`，除非 `.harness/design/` 中已经手工准备好图像资产。

## 安装

```bash
uv sync
```

如果尚未设置 API key：

```bash
cp .env.example .env
```

然后将 Anthropic key 写入 `.env`：

```bash
ANTHROPIC_API_KEY=...
```

可选的端点覆盖（`.env`）：

```bash
ANTHROPIC_BASE_URL=https://your-proxy.example.com
```

可选的模型覆盖（`.env`）：

```bash
PLANNER_MODEL=claude-sonnet-4-6
GENERATOR_MODEL=claude-sonnet-4-6
EVALUATOR_MODEL=claude-sonnet-4-6
EVALUATOR_VISION_MODEL=claude-sonnet-4-6
```

可选的设计阶段配置（`.env`）：

```bash
DESIGN_MODE=text-only                   # 或 "image-first"
DESIGN_IMAGE_API_KEY=                  # 仅在需要自动生成设计图片时必填
DESIGN_IMAGE_BASE_URL=https://right.codes/draw
DESIGN_IMAGE_MODEL=gpt-image-2
DESIGN_IMAGE_SIZE=1024x1024
DESIGN_IMAGE_TIMEOUT_SECONDS=180
```

可选的独立视觉打分器配置（`.env`，用于外观审阅；未设置时回退到 `EVALUATOR_MODEL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`）：

```bash
EVALUATOR_VISION_MODEL=claude-sonnet-4-6
EVALUATOR_VISION_API_KEY=...
EVALUATOR_VISION_BASE_URL=...
EVALUATOR_VISION_ENDPOINT_TYPE=anthropic   # 或 "openai" 表示 OpenAI 兼容的 chat completions
EVALUATOR_VISION_MAX_TOKENS=4096
EVALUATOR_VISION_MAX_RETRIES=0             # 默认不自动重试付费请求
EVALUATOR_VISION_RETRY_BASE_DELAY=2.0      # 指数退避基础秒数（默认 2.0）
```

可选的运行时 / planner 调优（`.env`）：

```bash
MAX_DELIVERABLES_PER_SPRINT=5      # validator 硬上限；调高可放宽 Sprint 大小
MAX_EXIT_CRITERIA_PER_SPRINT=5     # validator 对 exit_criteria 的硬上限
MAX_BUDGET_USD=150
MAX_ROUNDS=3
EDIT_MAX_ROUNDS=10                 # Edit 上限；通过立即停止
EDIT_FULL_REPLAY_INTERVAL=5        # 定期全量历史回归
FRONTEND_PORT=5173
PLAYWRIGHT_HEADLESS=false
```

## 配置优先级

下面这些运行时配置都遵循同一优先级：

1. CLI 参数
2. 环境变量
3. 内置默认值

模型选择：

- `PLANNER_MODEL`
- `GENERATOR_MODEL`
- `EVALUATOR_MODEL`
- `EVALUATOR_VISION_MODEL`
- `PLANNER_SCOPE_MODE`（默认 `query-aligned`；`expansive-data` 恢复原先用于数据构造的 5–10 Sprint 扩张路线）

CLI 覆盖：

- `--planner-model`
- `--generator-model`
- `--evaluator-model`
- `--evaluator-vision-model`
- `--planner-scope-mode query-aligned|expansive-data`

运行参数：

- `MAX_BUDGET_USD` ↔ `--max-budget`
- `MAX_ROUNDS` ↔ `--max-rounds`
- `EDIT_MAX_ROUNDS` ↔ `--edit-max-rounds`
- `FRONTEND_PORT` ↔ `--frontend-port`
- `DESIGN_MODE` ↔ `--design-mode`
- `PLANNER_SCOPE_MODE` ↔ `--planner-scope-mode`
- `PLAYWRIGHT_HEADLESS` ↔ `--playwright-headless` / `--no-playwright-headless`

内置默认值：

- 模型：`claude-sonnet-4-6`
- 最大预算：`150`
- 最大轮次：`3`
- Edit 最大轮次：`10`（上限，不是必须轮数）
- 前端端口：`5173`
- 设计模式：`text-only`
- Playwright headless：`false`

设计图生成仅支持通过环境变量配置：

- `DESIGN_IMAGE_API_KEY`
- `DESIGN_IMAGE_BASE_URL`
- `DESIGN_IMAGE_MODEL`
- `DESIGN_IMAGE_SIZE`
- `DESIGN_IMAGE_TIMEOUT_SECONDS`

## 设计阶段

当 `design_mode=image-first` 时，harness 会在 planning 与 build 之间插入一个设计检查点：

1. Planner 写出 `design_tokens.json`，其中必须包含 `visual_experiment` 结构。
2. Design Stage 将结构化实现指导写入 `.harness/design/`。
3. 如果图像生成配置可用，harness 会尝试创建：
   - `approved_concept.png`：完整概念图参考
   - `background_ui.png`：供语义化 HTML 覆盖的无文字背景图资产
4. Generator 在构建前会读取这些设计契约。

设计阶段支持三种结果：

- `image_backed_ui`：两张图都存在，构建阶段使用完整的 image-backed 契约
- `concept_reference_only`：仅存在 `approved_concept.png`，构建阶段把它作为视觉参考，但不把它当作正式背景资产
- `text_only_fallback`：可用图像资产都缺失，构建阶段仅依据文本设计契约继续执行

## Trace 文件

每次基于 SDK 的 agent 运行都会在本次运行目录的 `.harness/traces/` 下写入 trace 产物。SDK trace 是记录 harness 事件的 JSONL 文件。配套的 Claude HTTP trace 使用同名前缀并追加 `.http.jsonl`，例如 `planner.http.jsonl`、`generator_round_1.http.jsonl` 或 `evaluator_round_1.http.jsonl`。

HTTP JSONL 文件关闭后，harness 会在同目录生成同名前缀的自包含 HTML 文件，例如 `planner.http.html`。该 HTML 文件可在浏览器中打开，提供完整 trace 浏览器，包括 turn 侧栏、path 筛选、主题与语言控制、token 与 duration 摘要、用户消息、assistant text、tool use、thinking block、request JSON、response JSON 与 SSE events。JSONL 文件仍然是源产物。

## 快速开始

`uv run python -m src.main "<prompt>"` 与 `uv run harness "<prompt>"` 等价 —— 后者是 `[project.scripts]` 注册的入口。下面的示例选用更短的形式。

仅运行 Planner：

```bash
uv run python -m src.main "Build a bold counter app with increment and decrement buttons" \
  --workdir ./e2e-plan-only \
  --plan-only
```

最小端到端运行：

```bash
uv run python -m src.main "Build a bold counter app with increment and decrement buttons" \
  --workdir ./e2e-test-1 \
  --max-rounds 3 \
  --max-budget 20 \
  --playwright-headless
```

恢复一次中断的运行：

```bash
uv run python -m src.main "Build a bold counter app with increment and decrement buttons" \
  --workdir ./e2e-test-1 \
  --resume \
  --playwright-headless
```

启用可选的 image-first 设计阶段：

```bash
uv run python -m src.main "Build a bold counter app with increment and decrement buttons" \
  --workdir ./e2e-image-first \
  --design-mode image-first \
  --max-rounds 3 \
  --max-budget 20 \
  --playwright-headless
```

为 planner / generator / evaluator 显式指定模型：

```bash
uv run python -m src.main "Build a bold counter app with increment and decrement buttons" \
  --workdir ./e2e-test-1 \
  --planner-model claude-opus-4-1 \
  --generator-model claude-sonnet-4-6 \
  --evaluator-model claude-sonnet-4-6 \
  --playwright-headless
```

## 在 Docker 中运行

仓库提供了容器化运行器，便于隔离且可复现地运行。容器在进程内工具门控之上额外强制一层 OS 级沙箱：非 root 用户、只读根文件系统、放弃 Linux capabilities、no-new-privileges、pids / memory / cpu 限额，以及仅 loopback 的端口绑定。

依赖：Docker 24+ 与 v2 `compose` 插件，以及 `.env` 或宿主环境中的 `ANTHROPIC_API_KEY`（容器只会转发宿主上确实存在的环境变量）。

通过 `Makefile` 的常用流程：

```bash
# 构建镜像（一次构建，之后命中缓存）
make build

# 在容器内跑测试套件
make test

# 仅 Planner 的烟雾测试；输出会出现在宿主的 ./workdir/.harness/
make plan-only PROMPT="Build a bold counter app"

# 完整的 build-evaluate 循环，Playwright headless
make run PROMPT="Build a bold counter app"

# 把 harness 输出指向另一个宿主目录
make run PROMPT="Build a bold counter app" WORKDIR=./e2e-counter

# 进入镜像中的 bash（适合临时调试）
make shell

# 删除已构建的镜像
make clean
```

容器内 `/app/workdir` 在宿主上对应的路径由 make 变量 `WORKDIR` 控制（默认 `./workdir`）。harness 写入的所有文件——生成的 `frontend/`、planner 规格、Sprint 计划、各轮评分、trace——都会立即出现在宿主上，且容器运行期间可在宿主侧编辑，便于手改前端再让评估器重新打分。

不通过 `make` 直接运行：

```bash
docker compose run --rm harness "Build a bold counter app" \
  --workdir /app/workdir --plan-only
```

通过先导出 compose 变量来覆盖 workdir：

```bash
HARNESS_WORKDIR=./e2e-counter docker compose run --rm harness \
  "Build a bold counter app" --workdir /app/workdir --playwright-headless
```

前端开发服务器仅发布到宿主的 `127.0.0.1:5173`，因此宿主浏览器可以访问 `http://127.0.0.1:5173`，但 LAN 内任何机器都无法访问。若要换端口，必须同时修改 `--frontend-port` CLI 参数与 `docker-compose.yml` 中的 `ports:`。

## CLI

```bash
uv run python -m src.main "<prompt>" [options]
```

主要选项：

- `--workdir`：生成应用的输出目录
- `--plan-only`：只跑 planner 后停止（与 `--resume` 互斥）
- `--max-rounds`：build/evaluate 循环上限（默认：`MAX_ROUNDS` 环境变量或 `3`）
- `--edit-max-rounds`：显式 Edit 的 build/evaluate 上限（默认：`EDIT_MAX_ROUNDS` 或 `10`；通过即停）
- `--max-budget`：USD 总预算（默认：`MAX_BUDGET_USD` 环境变量或 `150`；80% / 90% 警告，100% 停止）
- `--planner-model`：planner 模型覆盖
- `--generator-model`：generator 模型覆盖
- `--evaluator-model`：evaluator 模型覆盖
- `--evaluator-vision-model`：独立视觉打分器模型覆盖
- `--design-mode`：`text-only` 或 `image-first`
- `--frontend-port`：dev server 端口（默认：`FRONTEND_PORT` 环境变量或 5173）
- `--keep-frontend`：fresh 运行时不擦除 `workdir/frontend/`
- `--task-mode`：`auto`、`generate` 或一等 `edit`
- `--target-route`：Edit 允许的同源页面路径；多页任务可重复指定
- `--input`：本地图片或受限大小的文本/源码输入；多模态任务可重复指定
- `--playwright-headless` / `--no-playwright-headless`：显式开启或关闭 Playwright MCP headless（默认：`PLAYWRIGHT_HEADLESS` 环境变量或 `false`）
- `--resume`：从 `.harness/harness_state.json` 恢复
  `resume` 仅兼容同一版本 harness 写出的 `.harness/` 状态目录；恢复旧目录前需要先删除旧 `.harness/`。

## 输出布局

以 `--workdir ./e2e-test-1` 为例，harness 会写入：

- `./e2e-test-1/frontend/`：生成的前端应用
- `./e2e-test-1/.harness/spec.md`：planner 产品规格
- `./e2e-test-1/.harness/design_tokens.json`：planner 视觉契约
- `./e2e-test-1/.harness/feature_list.json`：planner 特性目录及 Sprint 分配
- `./e2e-test-1/.harness/sprint_plan.json`：含 deliverables 与 exit criteria 的有序 Sprint 计划
- `./e2e-test-1/.harness/ui_verification_plan.json`：每个 Sprint 的浏览器验证检查项
- `./e2e-test-1/.harness/design/design_brief.json`：在启用 `image-first` 时供 generator 消费的设计阶段 brief
- `./e2e-test-1/.harness/design/layout_contract.json`：overlay 与响应式布局契约
- `./e2e-test-1/.harness/design/asset_manifest.json`：生成或手工提供的设计资产及实现说明
- `./e2e-test-1/.harness/design/approved_concept.png`：可选的概念图参考
- `./e2e-test-1/.harness/design/background_ui.png`：可选的无文字背景资产，供最终前端使用
- `./e2e-test-1/.harness/accepted_sprints.json`：已接受的 Sprint 与当前目标
- `./e2e-test-1/.harness/progress.md`：planner 与 generator 共同写入的只追加进度日志
- `./e2e-test-1/.harness/build_log.md`：generator 自评
- `./e2e-test-1/.harness/feedback_round_N.md`：evaluator 反馈
- `./e2e-test-1/.harness/grade_round_N.json`：evaluator 评分（功能 + 外观合并）
- `./e2e-test-1/.harness/visual_manifest_round_N.json`：视觉打分器使用的截图清单
- `./e2e-test-1/.harness/visual_round_N_*.png`：视觉打分器使用的截图
- `./e2e-test-1/.harness/harness_state.json`：恢复检查点
- `./e2e-test-1/.harness/edit_task_contract.json`：显式 Edit baseline 与路由上界
- `./e2e-test-1/.harness/edit_card.json`：单 Sprint 增量指令、需求关系、影响标签、目标、冲突与视觉策略
- `./e2e-test-1/.harness/regression_selection_round_N.json`：受影响历史检查、受保护路由 sentinel 与选择原因
- `./e2e-test-1/.harness/repair_packet_round_N.json`：精确失败证据与下一次 Repair 的动态范围
- `./e2e-test-1/.harness/task_inputs.json`：输入类型、哈希和暂存路径清单
- `./e2e-test-1/.harness/inputs/`：按内容寻址的用户任务输入副本
- `./e2e-test-1/.harness/logs/frontend_round_N.log`：前端运行时日志
- `./e2e-test-1/.harness/traces/*.jsonl`:每次 SDK 调用的 trace

## 评估模型

evaluator 以 Sprint 为粒度审阅运行中的前端。任何语义模型或视觉模型调用之前，
harness 自己先取得确定性的浏览器证据。

它沿四个维度评分：

- `design_quality`
- `functionality`
- `originality`
- `craft`

每一轮按证据类型路由：

1. harness-owned Playwright 执行交互以及 DOM/text/property/attribute/ARIA/focus/storage/console/URL assertions、语义 fragment guard 和被选中的历史 tape；
2. 一旦确定性检查失败，直接生成零模型成本的结构化 grade 与 Repair packet，不再调用付费语义 judge；
3. 确定性门禁通过后，语义 evaluator 才补充产品评分；只有 Edit card 明确要求视觉、conditional check 属于视觉类别，或任务带图片输入时，才截图并调用视觉 scorer。

harness 把适用证据写入 `grade_round_N.json`。通过立即结束；失败则在 Edit 上限内进入下一次有界 Repair。

## 调试

运行失败时优先查看以下文件：

前端运行时日志：

```bash
sed -n '1,220p' ./e2e-test-1/.harness/logs/frontend_round_1.log
```

Planner trace：

```bash
sed -n '1,220p' ./e2e-test-1/.harness/traces/planner.jsonl
```

Generator trace：

```bash
sed -n '1,260p' ./e2e-test-1/.harness/traces/generator_round_1.jsonl
```

Evaluator trace：

```bash
sed -n '1,260p' ./e2e-test-1/.harness/traces/evaluator_round_1.jsonl
```

视觉采集 trace：

```bash
sed -n '1,160p' ./e2e-test-1/.harness/traces/visual_capture_round_1.jsonl
```

trace 中的有用信号：

- `run_start`：agent 调用参数
- `permission_check`：来自 `can_use_tool` 回调的工具放行/拒绝决策（仅对**不在** `--allowedTools` 中的工具触发）
- `sdk_message`：流式 SDK 事件（在 `ToolUseBlock` 中查找 `name=Bash`，可看到 agent 实际执行的命令）
- `sdk_stderr`：Claude Code CLI 的 stderr
- `repair_block` / `repair_block_exhausted`：repair 模式下 Stop hook 的活动（阻塞原因、剩余次数、预算耗尽）
- `run_complete`：最终结果与成本

自动付费视觉重试默认关闭（`EVALUATOR_VISION_MAX_RETRIES=0`）。只有操作者明确授权并设置正数时，重试才会发生；其记录位于 harness logger，因为视觉阶段走纯 HTTP 而非 SDK。

## 架构说明

当前 harness 使用：

- `src/agents/sdk_runner.py`：Claude Agent SDK 集成、工具门控、trace 写入
- `src/agents/planner.py`：planning bundle 生成与 schema 校验
- `src/agents/design_stage.py`：image-first 设计契约生成与回退选择
- `src/agents/image_generation.py`：可选设计图生成所用的 HTTP 客户端
- `src/agents/generator.py`：以 Sprint 为粒度的前端生成与 repair
- `src/agents/evaluator.py`：基于 Playwright 的功能性评估
- `src/agents/visual_capture.py`：基于 Playwright 的截图采集
- `src/agents/vision_scorer.py`：基于纯 HTTP 的独立视觉打分（Anthropic 或 OpenAI 兼容）
- `src/agents/visual_review.py`：把视觉打分结果合并回轮级评分
- `src/orchestration/harness.py`：Sprint 循环、检查点、预算
- `src/orchestration/edit_task_contract.py`：一等 Edit、干净 baseline 与路由契约
- `src/orchestration/task_inputs.py`：受限文本/图片暂存与 provider-native 图片消息
- `src/orchestration/runtime.py`：前端 dev server 进程管理
- `src/orchestration/file_comm.py`：智能体之间共享的 `.harness/` 文件总线
- `src/orchestration/cost_tracker.py`：按阶段记账与预算上限
- `scripts/run_batch.py`：通用并发、可续跑任务调度器
- `scripts/recover_accepted_tapes.py`：仅凭既有通过证据恢复缺失 tape，并在最终状态真实重放
- `scripts/export_run_folders.py`：严格 trajectory 记录的人工可读视图
- `scripts/validate_webcompass_edit_case.py`：零 LLM 的真实多页 Edit → 失败候选 → 证据驱动 Repair 验证

## 安全模型

**`sdk_runner.py` 中的进程内工具门控并非沙箱。** generator 拥有对 `node`、`python`、`python3`、`npm`、`npx`、`pnpm`、`yarn`、`uv`、`vite`、`tsc`、`pytest`、`uvicorn` 的 Bash 访问，其中任何一个都足以以启动 harness 的用户身份执行任意代码——只需先用 `Write` 落盘脚本，再请求执行即可。`_validate_bash_command` 中的 token 级检查是*针对意外的纵深防御*，并非约束原语。

`sdk_runner.py` 在 Claude Agent SDK 自身的 `can_use_tool` 之上**确实**强制：

- **PreToolUse Bash hook**：每次 `Bash` 调用在 CLI 执行*之前*都会过 `_validate_bash_command`（evaluator 走 `_validate_bash_command_readonly`）。CLI 会自动放行 `--allowedTools` 中列出的工具，且永远不会就这些工具询问 `can_use_tool`，因此校验必须挂在 PreToolUse 上才能在 generator 的 Bash 调用上真正生效。
- Bash 命令 token 拒绝 shell 控制操作符（`&&`、`||`、`|`、`;`、`>`、`<`、`$(`、反引号、**裸 `&` 后台 fork**、换行）、绝对路径、`..` 与 `~` 简写。
- Bash 仅限于硬编码可执行文件白名单；`git` 仅允许 `status`、`diff`、`log`、`show`、`add`、`commit`、`rev-parse`、`branch`、`ls-files`、`stash`——禁用 `push`、`clone`、`fetch`、`remote`、`config`、`submodule`，且子命令前不允许任何 flag。
- **evaluator** 在更严格的 `read_only` 配置下运行 Bash：白名单更小（无 `cp`/`mv`/`touch`/`mkdir`/`sed`），`python`/`python3`/`node` 拒绝 `-c`/`-e`/`--eval`/`-i` 以阻断内联代码执行；`git` 仅允许只读子命令；`npm`/`pnpm`/`yarn`/`npx` 仅接受 `list`/`view`/`info`/`outdated`/`ls`（无 `install`/`build`/`test`/`run`）。
- 传给 `Read` / `Write` / `Edit` / `MultiEdit` / `Glob` / `Grep` / `LS` 的文件路径必须解析在 `workdir` 内（无 `..`、无绝对路径、无 `~` 简写）。
- `find` 拒绝 `-exec`、`-execdir`、`-delete`、`-fprint*`、`-ok`、`-okdir`、`-print0`、`-fls`。
- Playwright MCP 浏览器仅允许导航到 `http(s)://{127.0.0.1, localhost, ::1}` 的指定前端端口 —— `file://`、云元数据 IP、其他 localhost 端口都会被拒绝。
- 独立视觉打分器仅接受 `<workdir>/.harness/` 下、后缀为 `.png` 的截图路径。
- 前端 dev server 启动时会先净化环境：任何名字含 `KEY` / `TOKEN` / `SECRET` / `PASSWORD` / `PASSPHRASE` / `CREDENTIAL`，或以 `ANTHROPIC_` / `OPENAI_` / `AWS_` / `AZURE_` / `GOOGLE_` / `GH_` / `GITHUB_` 开头的变量在 `Popen` 前都会被丢掉，避免 Vite 插件或 generator 写出的配置把 API key 内联进 bundle。

### 部署建议

把 harness 当作其他能跑代码的 agent 一样对待：不要在保存凭证、不愿被改动的源码、或与不可泄露的机密同机的 workload 中运行。受支持的隔离部署形态是上文「在 Docker 中运行」描述的容器 —— 它在进程内工具门控之上叠加了 OS 级沙箱（只读 rootfs、cap_drop=ALL、no-new-privileges、pids / memory / cpu 限额、loopback-only 端口绑定）。在裸开发机上运行仅适合快速迭代，但**没有任何进程内检查能在 prompt 注入面前可信** —— 容器才是约束边界。

## 测试

跑测试套件：

```bash
uv run pytest tests -q
```

测试覆盖 harness 控制流、SDK 集成、运行时行为、评分逻辑，以及本地 E2E 中发现的回归用例。

可额外运行一个不调用 LLM 的真实 WebCompass 对齐多页样本：

```bash
uv run python scripts/validate_webcompass_edit_case.py
```

脚本会物化真实 source，证明功能原先不存在，让 4 个真实 GT patch 逐个通过最小路径授权，保存第一次失败 Edit 的浏览器证据，零成本生成 Repair packet，只做一次有界 Repair，再验证 DOM、ARIA、按钮 property、sessionStorage 与两个非目标路由 sentinel。每次运行都在 `logs/edit_first_20260828/` 下新建追加式目录。

## 许可

[MIT License](LICENSE).
