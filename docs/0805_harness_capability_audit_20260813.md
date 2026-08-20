# 0805 数据与 Harness 能力审计（2026-08-13）

## 审计对象与证据边界

本次通过只读 SSH 直接统计物理机目录：

`/data1/xieqianqian/webcoding/WebCoding_Data/releases/webcoding_sft_v2_20260805_compatible`

目录总量约 8.4G。统计直接读取 gzip JSONL、`dataset_index.json` 和首条记录的
嵌套 schema，没有改写或同步原始结果。多页/多文件与 browser-action 新增测试使用
真实 Chromium，但尚未用真实 LLM 重新构造 0805 全类型样本。因此本报告区分：

- 已确认的 0805 数据事实；
- 已实现的 harness 机械能力；
- 尚未完成的真实模型质量对齐。

## 六类交付规模

| 任务 | 样本 | 图片 | 主要监督结构 |
|---|---:|---:|---|
| text-generate | 9,094 | 0 | 完整代码文件数组 |
| image-generate | 5,094 | 5,094 | 目标截图 + 完整代码 |
| text-edit | 3,000 | 0 | 源项目 + 1–7 类 Edit + exact patches |
| image-edit | 3,000 | 3,000 | edit-before 截图 + 源项目 + patches |
| text-repair | 3,332 | 0 | 真实缺陷项目 + 1–7 类 defect + repair patches |
| image-repair | 3,000 | 6,000 | defective/clean 成对截图 + repair patches |

Edit 和 Repair 的目标是 patch 数组，不能假设只有一个 patch。Generate 完整代码和
Edit patch 数均可跨多个文件。

## 复杂度与分布

### 项目结构

| 任务 | 单页 | 多页 | `file_manifest` 文件数 | response/patch 数 |
|---|---:|---:|---:|---:|
| text-generate | 9,058 | 36 | 2–42 | 1–42 |
| text-edit | 2,991 | 9 | 3–9 | 1–31 |
| text-repair | 3,322 | 10 | 3–8 | 1–11 |

多页占比不高，但不能被单页默认逻辑吞掉。多文件则是主流：0805 Edit 的 changed-file
参考分布中，2,900/3,000 条修改三个文件。

### Edit 类型

0805 text-edit 有 40 个原子类型；每条组合 1–7 类，而且七个 task-count bucket
近似等量（分别 429/429/428/428/428/429/429）。类型包括：

- 复杂输入：Rich Text Editor、Date/Color Picker、File Upload with Progress；
- 状态与数据：Shopping Cart、Undo Redo、User Authentication、Data Table；
- 高级鼠标/键盘：Drag & Drop、Context Menu、Tooltip、Keyboard Shortcuts；
- 异步：Async Form Validation、Autocomplete、Infinite Scroll、Lazy Loading、Skeleton；
- 视觉/媒体：Parallax、Particle Effects、Dark Mode、Print Stylesheet、Transitions；
- 常见组件：Accordion、Modal、Tabs、Carousel、Tree、Toast、Lightbox 等。

`WEBCOMPASS_EDIT_TYPES` 已与这 40 类逐项一致。

### Repair 类型

0805 text-repair 有 11 个原子 defect family：Alignment、Color Contrast、Crowding、
Loss of Interactivity、Missing Attributes、Nesting Error、Occlusion、Overflow、
Semantic Error、Sizing Proportion、Text Overlap。每条同样组合 1–7 类。

### 构造上下文与成本参考

0805 metadata 记录的 prompt token：

| 任务 | 均值 | 最大值 |
|---|---:|---:|
| text-generate | 6,999.8 | 38,757 |
| text-edit | 12,089.7 | 38,757 |
| text-repair | 12,018.9 | 31,707 |

Edit/Repair 首条记录显示 construction model 为 `qwen3.7-max`；这是历史数据事实，
不是本轮重新调用模型的结果。harness 的低成本目标应通过工具读取、机械 action 和
失败后局部 refinement 降低无效 token，而不是删减必须的源文件或验收证据。

## 本轮发现并修复的能力缺口

原 harness 虽然已保存完整 40 类 taxonomy，但 planner/browser contract 实际只有
viewport、click、fill、select、key、scroll 和任意 `evaluate`。这导致高级类型必须
让 planner 手写 JavaScript；不支持的 action 还可能在执行期被误记为页面失败。

本轮新增共享的 typed action contract：

- `hover`；
- `click(button=right|middle|left)`；
- `drag_and_drop`；
- `set_input_files`（1–3 个有大小上限的内存 fixture，禁止读取任意本地路径）；
- `wait_for`（稳定 selector、明确 state、最长 5 秒）；
- `emulate_media`（print/screen 与 color scheme）。

Planner 与 Chromium executor 现在共用同一验证器。未知 action、越界等待、危险上传
文件名、多余字段等会被标记为 invalid test contract，不再伪造成产品 repair。
`WEBCOMPASS_EDIT_ACTION_PROFILES` 为 40 类逐项声明最小 typed browser vocabulary，
自动测试要求所有 profile 都只能引用真实实现的 action。

## 与“完全相同质量”仍有距离的部分

1. **尚无 40 类真实模型逐类成功率。** Typed action coverage 证明“能表达和执行”，
   不证明模型已经能为每类生成高质量页面与可靠 contract。
2. **超大项目仍需 manifest-first 工具读取。** 本轮已移除 AIR seed loader 的静默
   截断：默认完整接收最多 48 文件/140K 字符，覆盖 0805 观测到的 42 文件上限；超过
   预算会 fail closed 并要求转入工具读取 harness，绝不把部分上下文标成
   `all_files_included=true`。下一步仍需让超预算项目自动进入 manifest-first 路径。
3. **Generate 母本 gate 尚未完成全链验收。** 0805 可作格式/分布参考，但既有审计已
   发现大量外部脚本、CSS 和字体；不能把历史外链缺陷继承为质量标准。
4. **Image 三任务尚需严格物化协议。** 要稳定生成 task-local 图片路径、1920 viewport
   证据以及 Repair defective/clean 配对，并验证 JSONL 引用零缺失。
5. **Repair 必须保留真实失败 lineage。** 不能把规则标签或 clean 页面人工改坏后直接
   当成自然 repair；源失败、修复行为和截图/DOM 证据都要持久化。
6. **分布结论仍需真实 pilot。** 先按 40 Edit + 11 Repair 类型选择有决策价值的小样本，
   记录 success/error/timeout、token、浏览器证据和 rejection；达到 100 条后才可报告
   分布距离，300+ 正式批量前需用户确认配比与成本。

## 下一实施顺序

1. 为超过 48 文件/140K 字符的项目增加 manifest-first/tool-read 自动路由；
2. 选择 Tooltip、Drag & Drop、File Upload、Print、Async 五个不同工具族，各跑一个
   真实 LLM + Chromium 最小样本；
3. 将成功/拒绝轨迹导出为 v2 text/image Edit，并与 0805 schema 和 patch 分布核对；
4. 再覆盖其余 35 Edit 类型和 11 Repair 类型；
5. 最后才进入 100 条分层 pilot 和用户批准后的 300+ 构造。

0805 本身只能作为结构和历史质量下限参考。最终目标应是兼容其类型与监督密度，
同时通过当前更严格的母本、语义保护、真实行为和最小性门禁。
