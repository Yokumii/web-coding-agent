# harness 目录规则与文档索引

## 职责

`harness/` 是独立的正向 agentic producer：planner → 可选 design → generator → Playwright evaluator，负责从真实 Seed 演化 accepted 状态链并导出 Generate/Edit/Repair 轨迹。它不负责逆向路线抓取、训练或正式发布。

## 权威文档

- `README.md` / `README.zh-CN.md`：安装、CLI、配置、运行模式、输出布局和安全模型。
- `docs/pipeline.md`：当前 Product Session 与六类数据导出语义，是数据生产的首要依据。
- `.agents/skills/`：按任务读取对应 skill；`runs/`、`logs/` 是运行证据，不是全局规则。

## 核心语义

- Edit 是相邻 accepted checkpoint 的真实增量转换；下一步只能从当前 accepted 状态继续。
- Generate 是 accepted 状态派生的完整项目需求/代码，不向模型泄露 Seed 或未来计划。
- Repair 只来自浏览器验收实际复现的失败，以及同一 Sprint 内随后通过的修复；不注错、不为配额制造故障。
- candidate、失败轮、局部 smoke 和后台任务不能标为 accepted 或训练数据。
- 每个 Sprint 是一个连贯用户能力，可涉及多个页面/文件；`target_routes`、文件范围和 DOM/ARIA fragment 是不可扩大授权。

## 验收与导出

- 快速 Edit GT 的共享验证路径由 `--reverse-validate-root` 显式启用，调用 `reverse/validate` 检查和修复；未指定时仍保留 Harness 浏览器检查分支。各入口、参数优先级和验证范围见 `docs/pipeline.md`。每个完整 case 只设 40 分钟硬上限，快速路径的步骤和 LLM Repair 不另设轮次上限。
- 记录源码哈希、精确 patch、页面清单、图片角色/状态映射、浏览器证据和 lineage；不要以截图或单测代替真实验收。
- 六类导出通过 `scripts/export_trajectory_dataset.py` / `scripts/export_session_six_tasks.py`，消费 `dataset_index.json` 指向的不可变分片，不 glob 历史文件。
- Edit/Repair 窗口按 4–12 项保存；Repair 问题按独立问题计数；Image Repair 图片顺序为 current 后 target。

## 运行边界

单 Seed 失败保留证据并继续其他样本；鉴权/额度、系统性故障、数据损坏、资源危险或用户取消才可停止整批。凭据只从环境注入；批量任务必须有硬超时、心跳、费用/token 记录和子进程清理。修改流程后必须做真实模型 + 浏览器小样本验证。
