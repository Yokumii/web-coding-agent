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

每个 Edit 是一个连贯的 Sprint，可涉及多个目标页面或文件，但 `target_routes` 是不可扩大的授权上限：

- 非目标路由、文件、DOM/ARIA 片段、共享状态和共享 CSS 默认受保护；
- 多 HTML 页面按 pathname 分别归属；普通导航链接不等于源码依赖；
- 共享源码只开放能机械定位的目标命名区域；共享 CSS 只允许稳定目标锚点下的 selector；
- 新物理 HTML 路径必须由指令明确要求、列入 contract，并有可执行检查；
- 无法解析所有权、范围或稳定 baseline 时停止，不静默扩大修改面。

原生 standalone Edit 默认仍可使用 Planner、历史 accepted checks 和反事实最小性验证。Product Session 的当前策略覆盖如下：

- `minimality_guard_enabled=False`，导出记录 `skipped_by_user_policy`，不得声称已证明补丁最小；
- `edit_originality_required=False`，创新性由整条产品演化承担；
- 不追加历史全站回归，只验证本条 Edit 的连续用户流程和适用缺陷；
- 仍保留 Git 源/目标版本、精确 patch、范围保护、浏览器证据和构建 provenance。

## 4. 浏览器验收与 Repair

每条 Edit 只有一个连续 `browser_check`，与指令在同一次生成调用中产生。检查从目标页开始，仅在指令明确要求时导航、刷新、持久化或操作联动界面。每种状态变化保留一个有区分力的断言，不重复检查标题、容器、占位文字或同一结果的多种表述。

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
