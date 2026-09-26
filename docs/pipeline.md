# Harness 当前方案

## 目标

从已有可运行网页出发，通过连续 Edit 增加产品能力，并从同一条 accepted 状态链派生 Generate、Edit 和自然 Repair 数据。

## 整体流程

```text
accepted S(n)
  -> 确定一条 Edit
  -> 选择 Skill 和宿主接入位置
  -> 实现并运行页面
  -> 验收；真实失败才进入 Repair
  -> accepted S(n+1)
  -> 继续下一条 Edit 或导出数据
```

Edit 可以来自已有指令，也可以根据产品方向、当前网页和灵感库逐步规划。每一步只从上一 accepted 状态继续，并保留已有功能、业务数据和整体风格。一次 Edit 是一个连贯能力，可涉及多个文件或页面。

## 当前执行路径

- **标准 Harness**：Planner 生成原子目标和检查，Generator/Repair 修改网页，Harness 启动页面并保存运行、截图和补丁证据。
- **快速 Edit GT**：`run_g2_compound_edit.py` / `run_g2_compound_batch.py` 顺序消费 4–12 条指令。显式传入 `--reverse-validate-root` 时复用 `reverse/validate` 的检查与修复；未传入时使用 Harness 自身浏览器检查。
- **运行边界**：每个完整 case 默认 40 分钟硬上限；模型生成、检查设计和 Repair 共享该总预算，不再设置独立轮次上限。单 case 失败保留证据，批量继续处理其他 case。

Harness 会生成一份轻量宿主接入契约，明确目标路由、挂载位置、现有数据/状态来源、Skill API 和必须保留的能力。推荐范围只给模型排序相关文件和依赖，不是读写权限；模型可根据真实源码或运行证据扩大必要范围，Harness 仍拒绝明显破坏性改写。

## Skill 的作用

16 类 Edit Skill 提供可复用组件、宿主接入代码和类别特有风险。Harness 选择当前任务需要的 Skill，将组件接入网页已有的数据、界面和交互，再补齐具体指令要求。

通用组件负责可复用逻辑，网页自身负责业务状态和呈现方式。反复出现的共性问题用于改进 Skill，单个网页的问题在当前任务中解决。

所有 Edit Skill 共用 `.agents/skills/_shared/edit-integration.md` 的接入约束；各 Skill 只维护自身 API、特有风险、短示例和 `references/host-integration.js`。组件必须复用宿主数据和状态，不能另造一套业务数据源或只留下未挂载文件。

每条通过浏览器检查的 Edit 只写入轻量 RSI 候选。整批结束后最多处理两个不同 Skill；只有去业务化、无依赖、通过行为测试且未重复的纯函数才写入对应 `references/learned.json`。每个 Skill 最多保留三个 helper，并记录版本和内容哈希；后续 Edit 会自动加载这些函数。组件核心和接入文档的扩展仍需真实样本与组件回归后更新。

## 验收与修复

验收关注三件事：新功能是否命中指令（ITG）、原有功能和内容是否保留（FTI）、新增部分是否符合原网页风格（STC）。Harness 同时检查页面能否启动、是否空白、是否存在致命脚本错误或明显破坏。

页面能够打开只证明基本运行正常。筛选、展开、删除等交互仍需要浏览器操作结果；静态截图不能单独否定交互或数据流。基础设施错误与网页缺陷分开记录。

只有可复现的网页问题才生成 Repair，并在当前结果上局部修复后重新验收。通过后保存 checkpoint；失败时保留当前结果、问题和调用用量，不把 candidate 标成 accepted。

## 数据产出

- **Generate**：从 accepted checkpoint 的累计需求派生完整项目需求与代码；终态产生唯一 complete Generate。
- **Edit**：记录相邻 accepted 状态、用户增量指令和可回放精确 patch；原子 Edit 含 1 项，compound Edit 含 4–12 项。
- **Repair**：只记录有效验收实际发现的失败，以及同一 Edit 内从失败状态到修复成功状态的变化；不注错、不预选类别。

文本与图像数据共享同一真实网页状态，图片与代码保持一致。正式导出消费 `dataset_index.json` 指向的不可变记录，并保留 lineage、源码哈希、页面清单、检查证据和精确 patch。

## 当前状态

宿主接入契约、16 类 Skill 的共享接入约束与宿主代码、推荐范围、轻量 ITG/FTI/STC 验收、共享 reverse 验证分支和受限 Skill RSI 已进入实现。代码测试只能证明机制可运行；新模型或新批量配置仍先跑一个真实 case，首次结果交由人工检查后再扩大。
