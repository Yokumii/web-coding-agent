# Web Coding Agent

[English](README.md) | **简体中文**

本仓库是对 [Anthropic 长时运行 harness 设计工作](https://www.anthropic.com/engineering/harness-design-long-running-apps)中**前端部分**的简易复现。

当前实现刻意保持**仅前端**：

- `planner`：将一段简短提示扩展为雄心勃勃的产品规格与 Sprint 计划
- `generator`：在 `workdir/frontend` 下逐 Sprint 构建浏览器端前端应用，模式可为 `generate` 或 `repair`
- `evaluator`：使用 Playwright MCP 对运行中的前端进行功能性测试
- 独立的视觉打分器（vision scorer）审阅截图并覆盖外观相关评分

当前 harness **不包含后端生成与后端运行时**。

## 状态

已实现的能力：

- 基于 Claude Agent SDK 的执行
- Planner / Generator / Evaluator 三方智能体流水线
- 基于 Sprint 的推进，支持 `generate` / `repair` 两种生成模式
- Sprint 大小硬约束（每个 Sprint ≤5 deliverables 与 ≤5 exit_criteria，由 validator 强制），避免首轮被塞太多任务
- Repair 完成度强制：每个 repair 轮发出结构化目标列表（`repair_targets_round_N.json`），生成器必须先写入对应的 `repair_report_round_N.json` 才允许 Stop，并设有重试上限
- 仅前端运行时管理
- 基于 Playwright MCP 的功能性评估
- 评估器拥有只读 Bash（可 `cat`/`grep`/`python3 -m json.tool` 读取产物，但不能修改源码）
- 独立的截图采集与视觉打分阶段，结果覆盖外观相关评分
- 视觉打分器对瞬时错误（5xx 与连接失败）做指数退避带抖动的重试
- 跨 plan / build / evaluate 各阶段的恢复 / 检查点支持
- 为 planner、generator、evaluator、visual capture 各自记录 JSONL trace
- 前端运行时失败的本地日志
- 按阶段记录成本，并设有总预算硬上限
- WebGen-Bench 集成：端到端的 harness-then-bench 运行器，支持分层抽样、并发 harness worker、resume 与汇总报告

## 环境要求

- Python `>=3.11`
- `uv`
- Node.js + npm
- `.env` 或环境变量中存在 `ANTHROPIC_API_KEY`

Playwright MCP 在评估阶段通过 `npx` 启动，因此机器上必须可用 Node/npm。

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

可选的模型覆盖（`.env`）：

```bash
PLANNER_MODEL=claude-sonnet-4-6
GENERATOR_MODEL=claude-sonnet-4-6
EVALUATOR_MODEL=claude-sonnet-4-6
```

可选的独立视觉打分器配置（`.env`，用于外观审阅；未设置时回退到 `EVALUATOR_MODEL` / `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`）：

```bash
EVALUATOR_VISION_MODEL=claude-sonnet-4-6
EVALUATOR_VISION_API_KEY=...
EVALUATOR_VISION_BASE_URL=...
EVALUATOR_VISION_ENDPOINT_TYPE=anthropic   # 或 "openai" 表示 OpenAI 兼容的 chat completions
EVALUATOR_VISION_MAX_TOKENS=1200
EVALUATOR_VISION_MAX_RETRIES=3             # 瞬时 5xx / URLError 重试次数（默认 3）
EVALUATOR_VISION_RETRY_BASE_DELAY=2.0      # 指数退避基础秒数（默认 2.0）
```

可选的 planner / repair 调优（`.env`）：

```bash
MAX_DELIVERABLES_PER_SPRINT=5      # validator 硬上限；调高可放宽 Sprint 大小
MAX_EXIT_CRITERIA_PER_SPRINT=5     # validator 对 exit_criteria 的硬上限
MAX_REPAIR_BLOCK_ATTEMPTS=3        # 在放行未完成 repair 之前 Stop hook 额外阻塞的次数
```

## 模型配置

三个智能体均支持模型配置。

环境变量：

- `PLANNER_MODEL`
- `GENERATOR_MODEL`
- `EVALUATOR_MODEL`

CLI 覆盖：

- `--planner-model`
- `--generator-model`
- `--evaluator-model`

优先级顺序：

1. CLI 参数
2. 环境变量
3. 内置默认值（`claude-sonnet-4-6`）

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

## 运行 WebGen-Bench

`harness-bench` 在 [WebGen-Bench](https://github.com/mnluzimu/WebGen-Bench) 上端到端运行 harness，并把 UI 功能 + 外观分数汇总到 `summary.{json,md}`。Bench 自身的评估阶段保持不变；harness 的评估器**不**会被替换。每个样本都有独立的子 workdir 与子进程，所以单个样本崩溃不会污染下一个；harness 阶段现在还能并发跑多个样本。

### 在基础依赖之外的额外要求

- `node`、`pm2`（或 `npx`，每个样本都用来启动 dev server）
- macOS 上需要 `lsof`（harness 的端口回收依赖它）
- 在 `awesome-web-bench/webgen-bench/WebGen-Bench/` 下检出 WebGen-Bench fork —— 经过补丁的 [`YzkMing/WebGen-Bench`](https://github.com/YzkMing/WebGen-Bench) fork，已适配 `pm2` 发现并通过环境变量暴露视觉模型
- 一个 **OpenAI 兼容**的 chat completions 端点，用于 bench 的视觉打分（UI 验证 + 外观评级）。如果 `EVALUATOR_VISION_ENDPOINT_TYPE` 不是 `openai` 且未提供 `--vlm-base-url`，启动检查会拒绝运行，避免 webgen 在运行时抛出晦涩错误

### VLM 配置

webgen 读取 `WEBGEN_VLM_API_KEY` / `WEBGEN_VLM_BASE_URL` / `WEBGEN_VLM_MODEL`。`harness-bench` 按以下优先级（高到低）注入：

1. CLI 参数 `--vlm-api-key` / `--vlm-base-url` / `--vlm-model`
2. `EVALUATOR_VISION_API_KEY` / `EVALUATOR_VISION_BASE_URL` / `EVALUATOR_VISION_MODEL`
3. `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`（仅 key/base）与 `EVALUATOR_MODEL`(仅 model)
4. `OPENAI_API_KEY` / `OPENAI_BASE_URL`（仅 key/base）
5. `Qwen2.5-VL-32B-Instruct`（model 默认值）

### 烟雾测试（1 个样本）

```bash
uv run harness-bench webgen \
  --jsonl awesome-web-bench/webgen-bench/WebGen-Bench/data/test.jsonl \
  --runs-dir runs/smoke-1 \
  --limit 1 \
  --vlm-base-url https://your-openai-compatible-endpoint/v1 \
  --vlm-model gpt-4o-mini \
  --harness-args "--max-rounds 1 --max-budget 5 --playwright-headless"
```

`--harness-args` 会原样转发给每个样本的 harness CLI。

### 并发 harness 运行

harness 阶段默认串行（`--concurrency 1`），与旧行为一致。提高 `--concurrency` 可在共享的 WebGen-Bench 评估前并发运行多个 harness 样本。

```bash
uv run harness-bench webgen \
  --jsonl awesome-web-bench/webgen-bench/WebGen-Bench/data/test.jsonl \
  --runs-dir runs/parallel-4 \
  --strata application_type --per-stratum 1 --seed 42 \
  --concurrency 4 \
  --frontend-port-base 5173 \
  --vlm-base-url https://your-openai-compatible-endpoint/v1 \
  --vlm-model gpt-4o-mini \
  --harness-args "--max-rounds 3 --max-budget 30 --playwright-headless"
```

并发模式下的端口分配规则：

- worker 端口取自连续区间 `[--frontend-port-base, --frontend-port-base + --concurrency)`
- `--concurrency 4 --frontend-port-base 5173` 时，worker 使用 `5173`、`5174`、`5175`、`5176`
- 区间内任一端口已被占用时，启动会快速失败；bench runner 不会去杀其他不相关的本地进程
- 并发模式下 `--harness-args` **不能**包含 `--frontend-port` 或 `--workdir`，因为这两个值由 bench runner 按样本自行管理

单 worker 运行仍可用 `--harness-args "--frontend-port 6000"` 覆盖前端端口。

### 分层子集（推荐用于首次正式运行）

```bash
uv run harness-bench webgen \
  --jsonl awesome-web-bench/webgen-bench/WebGen-Bench/data/test.jsonl \
  --runs-dir runs/stratified-1 \
  --strata application_type --per-stratum 1 --seed 42 \
  --vlm-base-url https://your-openai-compatible-endpoint/v1 \
  --vlm-model gpt-4o-mini \
  --harness-args "--max-rounds 3 --max-budget 30 --playwright-headless"
```

`strata` 可取 `application_type`（默认）或 `primary_category`。在 `--per-stratum 1` 时，`application_type` 通常会得到 10–20 个样本，覆盖 bench 的类别分布。

### 其他选样模式

```bash
# 指定样本 ID
uv run harness-bench webgen --jsonl ... --runs-dir runs/r1 --ids 000001,000005,000023

# 取 jsonl 前 N 行（不抽样）
uv run harness-bench webgen --jsonl ... --runs-dir runs/r1 --limit 5

# 跑遍 jsonl 中的全部样本（WebGen-Bench 测试集共 101 条）
uv run harness-bench webgen --jsonl ... --runs-dir runs/r1 --all
```

`--ids` / `--limit` / `--all` / 分层抽样互斥；都没指定时使用分层抽样。

### 恢复 / 重新评估

```bash
# 崩溃或中断后恢复——已完成 harness 的样本会被跳过
uv run harness-bench webgen --jsonl ... --runs-dir runs/stratified-1 --resume

# 不重跑 harness，只重新评估——前提是 runs/<run>/samples/<id>/frontend/ 已存在
uv run harness-bench webgen --jsonl ... --runs-dir runs/stratified-1 --skip-harness
```

如果 `runs/<run>/manifest.json` 不存在（说明 `--runs-dir` 写错），`--resume` 会安全终止。`--skip-harness` 会强制把每个样本的 harness 状态置为 `completed`，直接进入打包 + bench。

`--resume` 还会修复中断的并发运行：处于 `running` 的样本会被重置为 `pending`；任何 `samples/<id>/.harness/harness_state.json` 已显示评估终态完成的样本会被回写为 `completed`，避免被重新派发。

### 输出结构

以 `--runs-dir runs/foo/` 为例：

```
runs/foo/
├── manifest.json              # 状态机 + 每个样本的状态（原子写）
├── sampled.jsonl              # 选中的子集（审计追踪）
├── samples/<sample_id>/
│   ├── frontend/              # harness 生成的应用
│   └── .harness/              # harness 状态（verdict、成本、trace）
├── bench_input/
│   ├── 000001.zip             # 注意：文件名是 jsonl 中 1 起始的行号，而非 sample_id
│   ├── 000001.json            # boltAction chat json（install + start 命令）
│   └── extracted/             # webgen 在此解压，并写入 results/ 与 <id>/shots/
├── logs/
│   ├── harness_<sample_id>.log
│   ├── ui_eval.log
│   └── eval_appearance.log
├── summary.json
└── summary.md
```

为了便于追溯，原始 `sample_id` 与 bench 位置 `app_id` 都被记录在 `bench_input/<app_id>.json._meta` 中。

并发运行时 `logs/harness_<sample_id>.log` 仍按样本切分，因此 worker 的交错输出在事后依然容易检视。

### Verdict 计算方式

聚合器直接读取 webgen 的原始产物 —— `bench_input/extracted/results/task_<idx>_<sub>/interact_messages.json`（UI 判定：`YES`=1、`PARTIAL`=0.5、其他=0）与 `bench_input/extracted/<app_id>/shots/result.json`（外观评分从视觉模型 `model_output` 文本中提取）。它**不**调用 webgen 的 `compute_acc.py` / `compute_grade.py`：那两份脚本硬编码了 101 个样本的假设，对子集会得出错误数字。每个样本的数值、合计与按 harness `last_verdict` 分组的 `by_verdict` 都会落到 `summary.json`，并在 `summary.md` 中以 Markdown 表格呈现。

harness 出错或前端缺失的样本仍会出现在汇总中（`ui_accuracy=0`、`appearance_grade=1`），`harness_verdict` 列反映实际情况，避免静默过滤失败案例。

## CLI

```bash
uv run python -m src.main "<prompt>" [options]
```

主要选项：

- `--workdir`：生成应用的输出目录
- `--plan-only`：只跑 planner 后停止（与 `--resume` 互斥）
- `--max-rounds`：build/evaluate 循环上限
- `--max-budget`：USD 总预算（80% / 90% 警告，100% 停止）
- `--planner-model`：planner 模型覆盖
- `--generator-model`：generator 模型覆盖
- `--evaluator-model`：evaluator 模型覆盖
- `--evaluator-vision-model`：独立视觉打分器模型覆盖
- `--frontend-port`：dev server 端口（默认：`FRONTEND_PORT` 环境变量或 5173）
- `--keep-frontend`：fresh 运行时不擦除 `workdir/frontend/`
- `--playwright-headless`：以 headless 模式运行 Playwright MCP
- `--resume`：从 `.harness/harness_state.json` 恢复

## 输出布局

以 `--workdir ./e2e-test-1` 为例，harness 会写入：

- `./e2e-test-1/frontend/`：生成的前端应用
- `./e2e-test-1/.harness/spec.md`：planner 产品规格
- `./e2e-test-1/.harness/design_tokens.json`：planner 视觉契约
- `./e2e-test-1/.harness/feature_list.json`：planner 特性目录及 Sprint 分配
- `./e2e-test-1/.harness/sprint_plan.json`：含 deliverables 与 exit criteria 的有序 Sprint 计划
- `./e2e-test-1/.harness/ui_verification_plan.json`：每个 Sprint 的浏览器验证检查项
- `./e2e-test-1/.harness/accepted_sprints.json`：已接受的 Sprint 与当前目标
- `./e2e-test-1/.harness/progress.md`：planner 与 generator 共同写入的只追加进度日志
- `./e2e-test-1/.harness/build_log.md`：generator 自评
- `./e2e-test-1/.harness/feedback_round_N.md`：evaluator 反馈
- `./e2e-test-1/.harness/grade_round_N.json`：evaluator 评分（功能 + 外观合并）
- `./e2e-test-1/.harness/repair_targets_round_N.json`：基于上一轮失败检查 / 退出条件 / bug 生成的结构化 repair 列表（仅在第 N 轮处于 repair 模式时存在）
- `./e2e-test-1/.harness/repair_report_round_N.json`：generator 按 target 的完成报告；Stop hook 在该文件存在且全部 target `addressed=true` 之前阻塞 agent 的 stop（或直至重试达到 `MAX_REPAIR_BLOCK_ATTEMPTS`）
- `./e2e-test-1/.harness/repair_incomplete_round_N.json`：当重试预算耗尽时写入，列出未处理的 target id，便于下一轮 evaluator 暴露
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

每一轮包含三个组件：

1. **功能性 evaluator**（Claude Agent SDK + Playwright MCP）：执行 Sprint 的 UI 验证检查、校验退出条件、检视源码，写入反馈与结构化评分。
2. **视觉采集 agent**（Claude Agent SDK + Playwright MCP）：与功能性 evaluator 并发运行，把当前轮的截图与 manifest 保存到 `.harness/visual_round_N_*.png`。
3. **视觉打分器**：直接把截图发到视觉端点（默认 Anthropic Messages API；当 `EVALUATOR_VISION_ENDPOINT_TYPE=openai` 时使用 OpenAI 兼容的 chat completions），并覆盖功能性 evaluator 写下的外观占位值。

随后 harness 把外观结果合并入 `grade_round_N.json`，重新计算 verdict，并决定继续 repair 当前 Sprint、推进到下一个 Sprint，还是结束运行。

### Repair 完成度协议

当 harness 开启一轮 repair 时，会从上一轮的 `grade_round_{N-1}.json` 派生一份结构化 **target 列表**——所有失败的 UI 检查、所有失败的退出条件、所有 critical/major bug、所有自由文本的 `repair_instruction`——写入 `.harness/repair_targets_round_N.json`。每个 target 带有稳定的 `id`、`summary`，以及从 evaluator 笔记中抽取的 `file_hints`（如 `frontend/src/components/PriceChart.jsx`）。

generator 的 repair prompt 强制其写出 `.harness/repair_report_round_N.json`，每个 target 一条记录，包含 `addressed=true|false`、`files_modified`、简短 `notes`/`reason`。Stop hook 在 agent 试图结束本轮时同时读取这两份文件：

- 报告缺失 → 阻塞，要求 agent 写入
- 任一 target 缺失或 `addressed=false` → 阻塞，并把未处理 id 列入反馈
- 触发 `MAX_REPAIR_BLOCK_ATTEMPTS` 次阻塞后 → 写入 `.harness/repair_incomplete_round_N.json` 并放行 stop，让 harness 进入下一轮（同样的项会再次出现在新的评分中并再次触发 repair），而非无限循环。

这堵住了一个失败模式：generator 每轮静默漏掉 1–2 个较难的 repair 项（CSS 级联顺序、WebGL 渲染配置等），让相同的 `partial`/`fail` 项在多轮中反复出现。

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
- `src/agents/generator.py`：以 Sprint 为粒度的前端生成与 repair
- `src/agents/evaluator.py`：基于 Playwright 的功能性评估
- `src/agents/visual_capture.py`：基于 Playwright 的截图采集
- `src/agents/vision_scorer.py`：基于纯 HTTP 的独立视觉打分（Anthropic 或 OpenAI 兼容）
- `src/agents/visual_review.py`：把视觉打分结果合并回轮级评分
- `src/orchestration/harness.py`：Sprint 循环、检查点、预算
- `src/orchestration/runtime.py`：前端 dev server 进程管理
- `src/orchestration/file_comm.py`：智能体之间共享的 `.harness/` 文件总线
- `src/orchestration/cost_tracker.py`：按阶段记账与预算上限
- `src/bench/`：WebGen-Bench 集成 —— `sampler` / `manifest` / `harness_runner` / `packager` / `bench_runner` / `aggregator` / `cli`。`harness-bench` CLI 通过 `pyproject.toml` 中的 `[project.scripts]` 注册。

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
