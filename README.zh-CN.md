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

## 在 Monorepo 中的职责

在 `WebCoding_Data` 内，本子项目是**正向 agentic 数据 producer**。源码、测试、
prompt 和 exporter 由父仓库统一版本控制，运行产物则与源码分离：

- `../runs/agentic/`：任务 workdir、checkpoint、trace、截图和导出轨迹
- `../logs/agentic/`：launcher、API probe 与 seed 同步的持久化日志

同级 `construct/` 保持为逆向/受控 producer。两条路线共享发布级审计与 schema，
但必须保留不同的 provenance 标签。

## 状态

已实现的能力：

- 基于 Claude Agent SDK 的执行
- Planner / 可选 Design Stage / Generator / Evaluator 流水线
- 基于 Sprint 的推进，支持 `generate` / `repair` 两种生成模式
- Sprint 大小硬约束（每个 Sprint ≤5 deliverables 与 ≤5 exit_criteria，由 validator 强制），避免首轮被塞太多任务
- 仅前端运行时管理
- 可选的 image-first 设计阶段，可写出 `design_brief.json`、`layout_contract.json` 与 `asset_manifest.json`
- 可选的设计图生成能力，可产出 `approved_concept.png` 与 `background_ui.png`，图像资产缺失时自动回退为仅文本设计契约
- 基于 Playwright MCP 的功能性评估
- 评估器拥有只读 Bash（可 `cat`/`grep`/`python3 -m json.tool` 读取产物，但不能修改源码）
- evaluator 侧截图采集加独立视觉打分阶段，结果覆盖外观相关评分
- 视觉打分器对瞬时错误（5xx 与连接失败）做指数退避带抖动的重试
- 跨 plan / build / evaluate 各阶段的恢复 / 检查点支持
- 为基于 SDK 的 agent 调用记录 JSONL trace
- 为基于 SDK 的 agent 调用生成 Claude HTTP trace 配套文件：`*.http.jsonl` 保持为源 trace，旁边生成 `*.http.html` 供浏览器查看
- 前端运行时失败的本地日志
- 按阶段记录成本，并设有总预算硬上限
- forward edit 的 DOM 契约保护：编辑前对已验收 seed 建立语义 DOM/ARIA surface 基线；除显式声明的最多两个 root 外，其他 surface 不得变化。该检查独立于截图/像素评分。

### Forward edit 回归保护

由 `scripts/prepare_forward_edit_seed.py` 创建的 workdir 含有已验证的
`seed_manifest.json`。首次 edit build 前，harness 会启动该 seed 并写入
`.harness/edit_dom_baseline.json`：其中是 landmark、role、`data-testid` root 与
语义控件的 DOM/ARIA 指纹，也包含可聚焦控件是否确实能获得键盘焦点；不是截图。随后 generator 必须写入
`.harness/edit_scope_round_N.json`，例如：

```json
{"allowed_root_keys":["main:unnamed"],"allow_new_roots":false}
```

契约最多允许两个已命名 baseline surface 内发生变化；其他 surface 被删除或语义变化，
或未授权新增 surface，都会作为 regression 使该轮失败，并写入
`grade_round_N.json::edit_guard`。该门禁约束 edit 的边界；它不能替代正常 browser
evaluator 对需求是否真正实现的验证。

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
EVALUATOR_VISION_MAX_RETRIES=3             # 瞬时 5xx / URLError 重试次数（默认 3）
EVALUATOR_VISION_RETRY_BASE_DELAY=2.0      # 指数退避基础秒数（默认 2.0）
```

可选的运行时 / planner 调优（`.env`）：

```bash
MAX_DELIVERABLES_PER_SPRINT=5      # validator 硬上限；调高可放宽 Sprint 大小
MAX_EXIT_CRITERIA_PER_SPRINT=5     # validator 对 exit_criteria 的硬上限
MAX_BUDGET_USD=150
MAX_ROUNDS=3
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
- `FRONTEND_PORT` ↔ `--frontend-port`
- `DESIGN_MODE` ↔ `--design-mode`
- `PLANNER_SCOPE_MODE` ↔ `--planner-scope-mode`
- `PLAYWRIGHT_HEADLESS` ↔ `--playwright-headless` / `--no-playwright-headless`

内置默认值：

- 模型：`claude-sonnet-4-6`
- 最大预算：`150`
- 最大轮次：`3`
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
- `--max-budget`：USD 总预算（默认：`MAX_BUDGET_USD` 环境变量或 `150`；80% / 90% 警告，100% 停止）
- `--planner-model`：planner 模型覆盖
- `--generator-model`：generator 模型覆盖
- `--evaluator-model`：evaluator 模型覆盖
- `--evaluator-vision-model`：独立视觉打分器模型覆盖
- `--design-mode`：`text-only` 或 `image-first`
- `--frontend-port`：dev server 端口（默认：`FRONTEND_PORT` 环境变量或 5173）
- `--keep-frontend`：fresh 运行时不擦除 `workdir/frontend/`
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
- `./e2e-test-1/.harness/logs/frontend_round_N.log`：前端运行时日志
- `./e2e-test-1/.harness/traces/*.jsonl`:每次 SDK 调用的 trace

## 评估模型

evaluator 以 Sprint 为粒度对运行中的前端做审阅，外观阶段被拆成独立的视觉打分流程。

它沿四个维度评分：

- `design_quality`
- `functionality`
- `originality`
- `craft`

每一轮包含两个组件：

1. **功能性 evaluator**（Claude Agent SDK + Playwright MCP）：执行 Sprint 的 UI 验证检查、校验退出条件、检视源码，写入反馈与结构化评分；同一次运行中还会补充截图，并把清单写入 `.harness/visual_manifest_round_N.json`。
2. **视觉打分器**：直接把截图发到视觉端点（默认 Anthropic Messages API；当 `EVALUATOR_VISION_ENDPOINT_TYPE=openai` 时使用 OpenAI 兼容的 chat completions），并覆盖功能性 evaluator 写下的外观占位值。

随后 harness 把外观结果合并入 `grade_round_N.json`，重新计算 verdict，并决定继续 repair 当前 Sprint、推进到下一个 Sprint，还是结束运行。

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

视觉打分器的瞬时重试（5xx / 连接失败）通过 harness logger 记录，而不是各 agent 的 trace —— 因为视觉阶段走纯 HTTP 而非 SDK。请在 harness 控制台输出中查找 `vision scorer attempt N/M failed; retrying in ...`。

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
- `src/orchestration/runtime.py`：前端 dev server 进程管理
- `src/orchestration/file_comm.py`：智能体之间共享的 `.harness/` 文件总线
- `src/orchestration/cost_tracker.py`：按阶段记账与预算上限

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

## 许可

[MIT License](LICENSE).
