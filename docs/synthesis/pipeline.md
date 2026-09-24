# WebCoding 数据生产流程

## 1. 目标与主线

Harness 把真实、可运行的 Seed 逐步演化为一条已验收状态链：

```text
Seed S0
-> 选择产品方向与随机步数 N（4-12）
-> 根据当前真实状态检索灵感并只生成下一条 Edit
-> 一次实现 -> 浏览器验收 -> 必要时一次 Repair 与复测
-> accepted S1...SN
-> 导出 Generate / Edit / natural Repair
```

核心语义：

- **Edit** 是相邻 accepted 状态间的真实转换，accepted target 才能成为下一步 Seed。
- **Generate** 是 accepted 状态的独立完整建站需求与完整目标项目，不向模型暴露 Seed 代码或未来计划。
- **Repair** 只来自有效浏览器检查实际复现的失败，以及同一 Edit 内随后通过的修复；不注错、不为配额制造故障。
- candidate、失败轮、局部 smoke 和已启动后台任务都不能称为 accepted 或可训练数据。

## 2. Product Session

Session 初始化时只确定产品方向和均匀随机的 `N in [4,12]`，不预生成完整能力链。每个 accepted 状态之后，才基于实际页面和已完成前缀生成下一步；续跑复用已保存方向、步数和完成响应，不改写历史。

下一条 Edit 的动态输入仅包含：

- Seed 简介与主任务转变；
- 当前页面、控件、导航和状态的浏览器事实；
- 已完成 Edit 的 `edit_id` 与一句能力摘要；
- 按产品方向和当前状态语义召回的 Top-K 灵感，默认 `k=3`。

完整灵感库、当前源码、完整日志、校验器源码和未来步骤不进入指令生成请求。召回和 Edit 生成分别计量；确定 `inspiration_refs` 后，只把对应且真实存在的参考代码片段交给实现模型。

每条 Edit 必须是独立、完整、英文的用户能力，一步完成一个有价值的操作或结果。入口、必要控件、状态、响应式和无障碍随功能一起实现，不拆成施工步骤，也不把多个可独立使用的能力打包。产品需要优先，WebCompass 16 类仅用于自然的设计偏好和事后分类，不设类别配额；不自然匹配时保留 `extension`。

主要状态文件：

- `session.json`：方向、目标步数、已完成前缀、当前状态和执行结果；
- `session_history/`：方向及逐步快照；
- `states/`：状态源码哈希和观察；
- `step_results/`：Harness 单步结果；
- `session_failure.json` / `batch_state.json`：失败范围和批次进度。

## 3. 单步执行与范围保护

`scripts/run_product_session_step.py` 把上游 Edit 和它的唯一 `browser_check` 直接物化为 atomic plan，不再调用 Harness Planner。当前 Product Session 固定使用 TokenWave `gpt-5.5`，一次实现调用，最多一次基于真实失败证据的 Repair 调用，即 `edit_max_rounds=2`。

Edit Skill 位于 `.agents/skills/webcompass-*/`，覆盖 16 类，每类包含参考实现、source anchor 接入规则、最小修改约束和轻量验收规则。参考实现提供原生 JS、React 和 Vue 三种接入方式；框架入口管理同一核心的生命周期，组件独占自己的 DOM 子树。Harness 按当前 subtask 的结构化类型和现有项目依赖只加载一种 Skill/技术栈，不替换项目技术栈。

原子 Editing Agent 使用 `copy_from` 请求直接复用参考文件；Harness 校验当前库的原始字节和 SHA，只允许复制到对应组件目录。原样参考代码不计入模型局部修改行数，适配代码仍受范围、行数和文件数约束。已有组件直接复用并保留局部适配；原生 HTML 的缺失 script/link 在授权页面内确定性补齐并进入完整 patch。原项目路由、非目标区域、共享数据和已有交互保持保护。

0921 输入入口为 `scripts/run_g2_compound_edit.py`：接受单条正式记录的 `instruction.src_code` 与 `instruction.description[]`，只消费 `query_ready`，保留 4–12 条子任务的原文、类型与顺序。先在真实浏览器确认 source 初始入口可见性，再把各 Skill 的因果验收规则映射为冻结的 browser_check；规划最多进行一次带具体错误反馈的修正并缓存原始响应；每步从上一 accepted 状态实现，运行当前与已完成流程，通过后才推进。完整链通过并验证 patch 回放后才输出整条 GT；受限试跑的前缀标为 partial。

编辑响应使用约束式 JSON Schema：参考文件 copy_from，现有源码精确文本替换。格式或语法拒绝后完整回滚，最多一次带具体错误的修正；修复提示只使用上一轮仍复现的运行错误，并保留浏览器操作失败的具体原因。已接入组件以项目当前源码为补丁依据。默认最多两轮；`--debug-max-rounds` 可显式扩展至八轮，仅用于调试。

每步保留模型原始响应和包含全部新增组件源码的 `ground_truth.patch`；成功前缀也输出可回放的 `patches.json`，其 `scope=accepted_prefix` 与完整链的 `scope=full` 明确区分。

```bash
python scripts/run_g2_compound_edit.py --case /path/to/0921-row.json \
  --output /path/to/run --provider-profile experimental-luna --max-steps 1
```

`experimental-luna` 从本机受限配置文件读取供应商与密钥，默认 `gpt-5.6-luna`、流式 Responses、单请求超时 600 秒、关闭响应存储。已复现的上游 `stream_read_error` 最多尝试 3 次，退避 5/10 秒；保留中断输出，未返回的用量标记为 unavailable。`--max-steps 1` 用于首次小样本；完整执行时省略。该入口默认启用 Edit Skills，其他入口由 `EDIT_SKILLS_ENABLED` 控制。轻量检查复用既有 Playwright typed actions，覆盖实际状态变化与 console/page errors；可机械判定时直接验收，不能机械判定的显式视觉要求才使用局部语义评审。

指定指令的快速 GT 使用同一入口的 `--fast-gt`：预加载源码、完整 `SKILL.md` 和核心实现，生成局部 patch，并检查核心是否实际接入。`--provider-profile qwen --model qwen3.7-max` 使用 Qwen 流式调用。每条 `--subtask-timeout` 最多 180 秒，生成、局部修正和浏览器复测共享预算。宽松检查只验证主要可见修改和交互，不比较精确条数或提示文案；React 属性警告仅记录，真实运行异常仍失败。检查错误由模型根据指令及页面证据自动纠正。

`--unattended` 使用全新目录、不加载人工检查；浏览器失败证据直接交给统一修复调用，同一步内最多三轮局部修复并回归旧功能，不另设辅助诊断调用。失败保留产物及成功前缀，不跳过失败步骤生成后续 GT。单任务 `--resume` 扣除失败步此前用时，不能借重启重置预算。

批量入口 `run_g2_compound_batch.py --fast-gt` 固定输入、代码和 Skill 快照，每个 task 独立目录及端口，自动启动并清理预览。独立守护进程按子任务期限终止卡住的进程组，记录心跳；失去批量主进程或取消时清理子进程。单 task 失败继续其他 task，鉴权/额度阻塞停止派发。相同目录重启复用终态结果，不自动重付费重跑失败项。金额未返回时记录未知，同时汇总 token；整批不设累计时限。

```bash
python scripts/run_g2_compound_batch.py --plan /path/to/plan.json --output /path/to/batch \
  --fast-gt --provider-profile qwen --model qwen3.7-max --workers 1 --limit 20 \
  --dependencies /path/to/compatible/node_modules --base-port 19000 --subtask-timeout 180
```

plan 的 `jobs` 包含唯一 `instance_id`、case 文件路径与 `task_count`。依赖目录须预先安装并适配母本；启动前校验真实小样本和目标机资源。

每个 Edit 是一个连贯的 Sprint，可涉及多个目标页面或文件，但 `target_routes` 是不可扩大的授权上限：

- 非目标路由、文件、DOM/ARIA 片段、共享状态和共享 CSS 默认受保护；
- 多 HTML 页面按 pathname 分别归属；普通导航链接不等于源码依赖；
- 共享源码只开放能机械定位的目标命名区域；共享 CSS 只允许稳定目标锚点下的 selector；
- 新物理 HTML 路径必须由指令明确要求、列入 contract，并有可执行检查；
- 无法解析所有权、范围或稳定 baseline 时停止，不静默扩大修改面。

原生 standalone Edit 可以使用 Planner、历史 accepted checks 和反事实最小性验证；0921 G2 量产模式采用轻量路径，关闭这些额外门禁。量产模式的当前策略如下：

- `minimality_guard_enabled=False`，导出记录 `skipped_by_user_policy`，不得声称已证明补丁最小；
- `edit_originality_required=False`，创新性由整条产品演化承担；
- 不回放历史 accepted checks，只验证本条 Edit 的一条连续用户流程；
- 不运行隐藏 oracle、11 类缺陷全面检查、独立 evaluator/judge、视觉一致性审核或反事实最小性证明；明显启动失败、核心控件不可操作和当前 browser_check 失败仍会触发修复；
- 每个 subtask 一次实现，最多两次带具体证据的 Repair；仍失败就保存现场、结束当前 case 并继续其他 case。第二次 Repair 仍只接收当前源码和最新失败证据，不回放整段历史上下文；
- Skill 版本在批次内固定；重复问题集中修订，不在每个 case 后自动升级 Skill；
- 仍保留 Git 源/目标版本、精确 patch、范围保护、浏览器证据和构建 provenance。

## 4. 浏览器验收与 Repair

每条 Edit 只有一个连续 `browser_check`，与指令在同一次生成调用中产生。轻量量产检查从目标页开始，只验证当前功能的最短核心路径和关键结果；仅在指令明确要求时导航、刷新、持久化或操作联动界面。每种状态变化保留一个有区分力的断言，不重复检查标题、容器、占位文字或同一结果的多种表述。次要视觉差异、非阻断缺陷和未覆盖的边界只记日志，不阻断量产。

检查必须通过真实用户操作证明明确要求的状态变化：

- 表单值优先用 `assert_value`；`assert_text` 对输入控件读取当前 value，对 select 读取选中标签，对普通节点读取 `textContent`；
- fixture 只能建立前置状态，不能直接写入本次行为的期望结果；
- 新功能 selector 可以是目标态条件，但 action 字段必须通过 Harness 的同一校验器；
- `browser_check.route` 是流程起点，`target_routes` 才是修改范围；
- 完成条件完整合并为一个验收条目，不因旧列表上限截断。

在同一流程涉及的目标区域和交互状态中，Harness 使用确定性浏览器检查覆盖适用的 11 类 WebCompass 缺陷：Occlusion、Crowding、Text Overlap、Alignment、Color Contrast、Overflow、Sizing Proportion、Loss of Interactivity、Semantic Error、Nesting Error、Missing Attributes。检查在实现前冻结，使用 DOM、几何、hit testing、computed style、语义、属性和可操作性证据；源码中已存在的相同问题不算本次 Edit 缺陷。

一次验收收集全部可观察失败，再合并为一次 Repair：

```text
一次实现 -> 功能流与适用缺陷检查
  -> 通过：accepted
  -> 失败：保存失败版本和证据 -> 一次 Repair -> 重跑同一组检查
       -> 通过：accepted，并可形成 natural Repair
       -> 失败：停止当前 Edit，保留失败证据
```

测试无效、导航/运行基础设施错误和产品实现失败必须分开记录。修正检测器后，只复测同一轮现有源码，不追加生成或 Repair。Product Session 不增加独立 judge，也不把常规功能或风格差异升级成 Repair。

## 5. 数据导出

入口：

```bash
uv run python scripts/export_trajectory_dataset.py \
  --session /path/to/session.json \
  --output-jsonl /path/to/dataset/records.jsonl \
  --six-output-dir /path/to/dataset/six_tasks
```

出口索引为 `six_tasks/dataset_index.json`；消费方只读取索引指向的不可变分片，不 glob 历史文件。重复导出按 Session 对账并保持幂等。

六类输出规则：

- Text/Image Generate：按 `generation_policy` 导出每个 accepted 状态，或只导出完整终态；需求必须绑定目标源码哈希和已完成前缀；
- Text/Image Edit：保留每个成功原子 Edit，并导出同一轨迹内所有长度 4-12 的连续窗口；窗口答案是起点到终点可回放的净 patch；
- Text/Image Repair：只导出真实失败版本到同 Sprint accepted 版本；无可见差异的语义/属性修复只保留 Text Repair，并记录 image skip 原因。

图片必须来自对应 Git 版本，覆盖项目 HTML 页面和该条流程涉及的状态，并与源码哈希、页面清单及交互映射绑定。Image Generate 只给目标图，Image Edit 给 source 图，Image Repair 按 current 图后 target 图排列。

Repair 问题数按独立问题计数，不按类别去重，也不跨版本拼接。训练侧 Repair 输入公开官方 11 类定义、问题数和故障源码；具体诊断、位置与标签保留在内部元数据和 Harness Repair 输入中。

连续 Edit 窗口按官方 4-12 项分布保存 sampling 权重；单页/多页、16 类 Edit 与 11 类 Repair 的对齐状态分别记录。保留全量数据，不为对齐强制丢样本。同一原始源码派生记录共享 `lineage_group`，训练/验证必须按组划分。

跨 Session 汇总：

```bash
uv run python scripts/export_trajectory_dataset.py \
  --merge-six-outputs /path/to/session-a/six_tasks /path/to/session-b/six_tasks \
  --output-jsonl /path/to/batch/dataset_index.json
```

## 6. 运行与恢复边界

- 单 Seed 内容、格式、局部超时或验收失败只影响该 Seed；保留成功前缀和失败证据，继续其他样本。
- 鉴权/额度、系统性实现故障、数据损坏风险、资源危险和用户取消可停止整批。
- 传输和导航临时错误只按调用方既有上限有界重试；不得把语义失败伪装成网络重试，也不得重复支付已完成阶段。
- 恢复必须校验 Session 状态、Edit 元数据、源码哈希和请求身份；输入或策略变化使用新的运行身份。
- 每个工作单元保留硬超时、资源保护、费用/token 记录、心跳和子进程清理；凭据仅从环境注入。

## 7. 代码入口

- `scripts/run_product_session_step.py`：执行一个外部规划的 Product Session Edit；
- `scripts/run_batch.py`：通用 Harness 批量入口；
- `scripts/export_trajectory_dataset.py`：轨迹与 Session 导出；
- `scripts/export_session_six_tasks.py`：六类分片与批量索引；
- `src/orchestration/harness.py`：主状态机；
- `src/orchestration/edit_*`、`browser_evidence.py`、`hidden_oracle_checks.py`：范围、检查与证据；
- `src/orchestration/webcompass_protocol.py`：官方协议转换；
- `src/orchestration/webcompass_subtask_distribution.json`：官方分布及来源哈希。

Product Session 的方向选择和逐步规划位于相邻 `inspiration_library/` 模块；Harness 只消费已结构化的当前 Edit。上传和正式发布不属于本流程，必须另行授权。

## 8. Harness 与 Edit Skill 的实现架构

### 8.1 分层职责

```text
输入记录 / Product Session
        |
        v
Edit 规划层
  - 当前 Seed 浏览器观察
  - 子任务与 Skill 类型
  - source anchors / target routes
  - 一条连续 browser_check
        |
        v
Skill 复用层
  - references/Component.{jsx,vue}
  - references/*.js|*.mjs|*.css
  - SKILL.md 接入规则
  - acceptance.json 因果验收规则
        |
        v
实现层
  - copy_from 复用参考实现
  - 精确局部 patch 接入现有入口、数据和容器
  - 范围、文件、行数和源码哈希保护
        |
        v
执行验收层
  - 启动/构建检查
  - Playwright typed browser flow
  - DOM/ARIA/几何/样式/交互缺陷检查
  - 语义评审仅处理无法机械判定的显式视觉要求
        |
        +--> accepted target --> 下一 Edit 的 Seed
        |
        +--> 真实失败证据 --> Repair --> 同一 browser_check 复验
        |
        +--> Skill / Harness 反馈 --> 独立候选版本验证后启用
```

Harness 负责状态机、模型调用、补丁事务、浏览器执行、证据和导出；Skill 负责某类组件的参考实现、宿主接线约束、生命周期要求和最短因果检查。Skill 不拥有宿主业务状态，组件只能通过 `container`、已有数据源、回调和公开 API 接入。

### 8.2 单 Edit 状态机

每个 Edit 从一个已确认的 `S(n)` 开始，成功后才产生 `S(n+1)`：

```text
source observation
  -> frozen plan / browser_check
  -> implementation candidate
  -> startup + build
  -> shortest causal browser flow
  -> applicable defect checks
      pass  -> accepted state / replayable patch
      fail  -> evidence + repair packet
             -> one bounded Repair
             -> same flow and regression checks
                 pass -> accepted / natural Repair
                 fail -> incomplete, retain artifacts
```

`candidate`、单轮失败、局部 smoke、只启动未验收的进程都不是 GT。只有 browser flow、适用缺陷检查、范围保护和 patch replay 均通过，才允许作为下一步 Seed 或导出答案。

0921 的 G2 入口是 `scripts/run_g2_compound_edit.py`：读取 `instruction.src_code` 和 `instruction.description[]`，按原顺序执行 4–12 个子任务；每个子任务一个 Sprint，默认两轮（实现 + 一次 Repair）。首次运行用 `--max-steps 1` 做真实小样本，确认后才扩大范围。当前 NjuLink smoke 使用 `--provider-profile experimental-luna`、`gpt-5.6-luna`，与 Product Session 文档中固定的 TokenWave `gpt-5.5` 路线分开记录。

### 8.3 Skill 的接入契约

每个 `webcompass-*` Skill 至少包含：

- `SKILL.md`：寻找现有入口、数据源、DOM 容器和事件；保留宿主结构；隐藏状态、快捷键、受控列表和销毁规则；最小适配示例。
- `references/Component.jsx`、`Component.vue`：框架生命周期薄入口，共用同一核心逻辑。
- `references/*.js` 或 `*.mjs`：框架无关的组件能力；组件只拥有自己挂载的 DOM 子树。
- `references/acceptance.json`：一条最短核心行为路径，必要时一个失败/恢复分支。

实现模型优先用 `copy_from` 复制参考文件，再用精确局部 patch 完成宿主适配。必须复用已有数据和状态，避免另造 demo 数据；React/Vue 组件在卸载时调用 `destroy()`，取消请求、定时器、动画和事件监听。参考组件更新状态时只能更新自己的动态节点，不能用 `container.innerHTML` 或整体 `status.textContent` 删除宿主控制节点。

### 8.4 灵活验收原则

验收检查行为，不检查某个框架必须采用的 DOM 层级或 HTML 序列化。规划器应从真实源码推导入口和状态节点；`data-testid` 是稳定行为锚点，不是唯一实现格式。Harness 对 `assert_visible`/`wait_for` 允许有限的等价节点映射，例如指标的 value/trend/status 后缀；点击、填写、导航、暂停、提交等用户操作仍必须命中真实可操作控件。

每条流程只保留最短可区分路径：先建立前置条件，执行一次核心操作，断言实际状态/内容/提交变化，必要时补一个关键失败或恢复。异步行为等待可观察状态，不使用固定延时推断结果；动态值先捕获真实值，再等待它发生真实变化。重复的容器、标题、占位文案和无关视觉差异不进入 Repair。

### 8.5 失败归属与 Skill 持续改进

每次失败都保存动作、预期、实际、源码路径、截图、DOM/可见性诊断和 console/page error。Harness 按证据路由：

| 证据 | 修复层 |
|---|---|
| 当前宿主入口、字段映射、路由或业务状态错误 | 当前项目候选 |
| 多个项目都会遇到的容器、隐藏、快捷键或生命周期接入错误 | 对应 Skill 的 `SKILL.md` 和示例 |
| 参考组件自身状态、拖放、进度、格式保留或销毁错误 | Skill reference implementation |
| selector 映射、浏览器操作、schema、超时或收尾错误 | Harness |

反馈先写入 `.harness/skill_feedback_round_N.json`，不直接修改当前启用版本。`skill_versions.py` 为候选版本建立独立目录，记录证据、同一样本验证、受影响回归和启用状态；只有真实触发样本与回归均通过才切换默认版本。运行中的任务固定使用创建时的 Skill 版本，保留 rollback 信息，禁止把样本专属字符串写进 Skill 或删除有效检查来制造通过。

### 8.6 关键产物与状态边界

单步目录至少保留：

- `.harness/source_observation.json`、`edit_context_round_N.json`：源项目入口和状态事实；
- `.harness/frozen_compound_plan.json`、`ui_verification_plan.json`：冻结的子任务、范围和 browser flow；
- `.harness/atomic_edit_plan.json`、`edit_scope_round_N.json`：允许修改的文件/路由/锚点；
- `response_stream.jsonl`、`traces/`：模型原始响应和调用用量；
- `frontend/`、`ground_truth.patch`、`patches.json`：候选源码和可回放 patch；
- `browser_evidence_round_N.json`、截图、`feedback_round_N.md`：真实验收证据；
- `repair_packet_round_N.json`、`skill_feedback_round_N.json`：受影响修复和可复用反馈；
- `grade_round_N.json`、`harness_state.json`：评分和 checkpoint。

`accepted` 是状态资格，不等同于 HTTP 200、页面能打开、模型返回 JSON 或静态检查通过。0921 当前仍把只有 Edit 指令的记录保留为 `query_only_generation_in_progress`；只有每个子任务完成真实浏览器验收并生成可回放 patch，才回填 `response`/GT 并更新 release manifest。
