---
name: generate-edit-instructions
description: 基于真实网页、产品灵感和已有执行状态规划或生成连续 WebCoding Edit 指令时使用；不负责实现网页或发布数据集。
---

# Generate Edit Instructions

先读取 `AGENTS.md`、[当前构造流程](../../../docs/synthesis/pipeline.md)及相关 `../../../inspiration_library/README.md`。核验当前网页、灵感库和既有执行证据，再生成或修订当前请求范围内的下一步指令。

保持已执行前缀和实际状态；不要把未执行的计划表述为已验证结果。只规划指令时不启动 Harness；实现与浏览器验收使用 `run-webcoding-harness`，受控构造或导出使用 `synthesize-data`。

报告输入来源、生成的指令/结构检查结果、产物位置和未验证边界。

## 连续 Edit 的检查范围（2026-09-13）

检查流程从目标页面开始，只有本条Edit明确要求导航时才测试导航。每个不同的状态变化保留一个有区分力的结果断言；刷新或联动只在指令要求时执行。操作成功已证明控件可用，不再预先断言可见；不逐项检查复制的日期、地点、标签或占位文字，不对同一结果重复检查文字、可见性、数量和存储。

每条 Edit 仅一个连续 browser_check，在生成指令的同一次调用中产生。以实际用户操作验证本条指令明确要求的状态变化、相关功能联动和持久化；只在要求保存时检查刷新/重新打开，只在要求联动时操作相关消费界面。可初始化前置数据，但不能预先写入本次行为的期望结果。删除不证明状态变化的标题、标签、容器和种子文本断言，避免重复检查同一结果；不追加全站检查、历史回归或独立审核模型。Harness 在这条操作流程涉及的实际网页和相关交互状态，检查适用的 WebCompass 11 类缺陷：Occlusion（遮挡）、Crowding（拥挤）、Text Overlap（文字重叠）、Alignment（对齐）、Color Contrast（颜色对比度）、Overflow（溢出）、Sizing Proportion（尺寸比例）、Loss of Interactivity（交互失效）、Semantic Error（HTML语义错误）、Nesting Error（嵌套错误）、Missing Attributes（属性缺失）。复用既有确定性浏览器检测，按目标区域和状态去重；不可到达的状态不编造缺陷。一次检测收集全部可观察的真实问题，保存类别、节点/测量值、截图和失败证据；剔除源码中同样存在的旧问题。多个类别或同类多个问题合并为一次 Repair 调用，随后复跑原功能流程和同一组缺陷检查；仍失败则记录失败，不追加第二次 Repair。自然 Repair 数量由真实失败及修复成功决定。

表单值优先使用 assert_value；assert_text 对 textarea/文本输入读取当前 value，对 select 读取选中项显示标签，对普通文字节点读取 textContent。等待与结果记录使用同一读法，不用HTML默认文字替代空值或错误当前值，避免因合理的控件实现差异制造 Repair。

## 逐步 Generate（2026-09-12）

自主 Session 对每个已验收状态 S1–SN 各合成一次独立完整建站需求，绑定对应完整源码、状态编号、源码哈希和已完成 Edit 前缀。下一步前由数据出口保存 Edit、Generate 和符合证据的自然 Repair；恢复时补缺并去重，终态不额外重复 Generate。训练用 Generate 输入仅为独立需求，不含 Seed 代码或未来计划。连续 Edit 指令仍一次调用生成，Generate 需求合成单独计量。

灵感引用落盘为库内 capability_id。若模型引用已完成 Edit ID，程序沿该步骤已记录的灵感来源展开并保存继承记录；未知或未来引用仍拒绝，不猜测新来源，不额外调用模型改写指令。

assert_text 的 contains 模式按互不包含的最外层匹配区域读值，避免卡片与内部按钮共用属性时对同一内容重复断言；独立匹配区域仍逐个要求满足，exact及计数语义保持原规则。浏览器证据记录策略版本；修正检测器后，恢复仅复测原失败轮现有源码，保留旧grade/证据/状态，不追加生成或Repair。

桥接到Harness时，把本条Edit的全部completion_criteria完整合并为一个exit_criteria条目；原列表保留在链元数据中。一个功能Edit仍只绑定一个验收条目和一个连续browser_check，不因旧Planner的10条列表限制中断，也不截断用户要求。

## Edit 生成输入约束（2026-09-12）

Edit指令生成输入包含Seed简短介绍、主任务转变方向、召回的top-k灵感、当前浏览器信息和简短Edit记录。动态字段为seed_introduction/task_transformation/retrieved_inspirations/browser/edit_records，禁止传入整库、当前网页源码、完整执行日志或校验器源码。先按产品方向和实际状态做语义Top-K召回，默认k=3（可配置）；当前使用TokenWave gpt-5.5语义排名，召回与单次Edit生成分别计量。仅传所选卡的行为信息和相关性理由；多页信息来自各HTML页的真实观察，历史每步仅保留edit_id与一句功能摘要summary，直接复用已有capability，不传完整指令、代码或依赖展开，不追加摘要模型调用。代码修改与逐步Generate仍按各自接口消费源码。请求身份绑定输入策略和k，恢复复用同一状态的已有响应。

默认返回Top-3灵感；召回和Edit生成仅使用行为信息。待本条Edit确定inspiration_refs后，在交给Harness时按这些实际引用读取库内source_slices，不把全部召回卡的代码前置输入。相同代码按内容哈希去重；没有代码的引用记录为unavailable，不补造代码。参考片段只供Harness实现模型按需借鉴，不作为目标源码或额外要求，训练Edit指令仍不含参考代码。

新生成的capability用一句简短功能描述，供后续edit_records的summary直接复用。assert_aria中label/live/modal三个标准简写可等价规范为aria-label/aria-live/aria-modal；未知字段仍交由既有校验器拒绝，不猜测语义。

## Seed背景与主任务转变（2026-09-12）

方向选择强调实质的主任务反转与产品跃迁：原主任务→新的用户行动与产出→交互和主界面重组→新产品身份。仅从访客浏览切换成站主管理同一批内容不足以构成跃迁。新任务逐步成为主入口、对象操作和结果页面的主要用途；按本步功能需要调整原任务对应的交互和布局，保留范围外的可用能力与可复用内容。避免“原网站加独立管理工作台”。

每步只完成一个独立有用的用户能力，仅捎带实现该动作必需的控件和状态。共享页面、对象或产品目标不等于同一能力；可独立使用的创建、编辑、排序、审核、发布等动作不能集中打包。第一步和最后一步同样遵守；不拆成空壳或代码施工步骤，也不为补齐目标制造大杂烩。contribution_to_goal说明这一步如何改变原任务下的操作或产出。能力仍按实际状态逐步产生。

browser_check的单键嵌套动作`{动作名:{参数}}`可无损规范为`{action:动作名,...参数}`，继续通过既有Harness协议校验；多动作或内外动作名冲突仍拒绝。格式恢复复用已有模型响应。

每条Edit Prompt显式包含seed_introduction（原Seed的简短产品介绍）和task_transformation（original_primary_task、target_product、target_primary_task、task_reversal）。主任务转变以原Seed的用户任务为起点，说明如何转向已确定的目标用户任务，不能把灵感来源产品当作Seed。自然语言保持简短，不传Seed源码或冗长执行元数据。

新Session在原有方向选择调用中同时生成这些介绍，不新增规划调用；旧Session缺失时只根据原Seed浏览器观察与已选方向补齐一次并保存product_context，保持既定目标不变。每一步直接复用，Edit历史仍是编号与一句功能摘要。Generate与Harness的源码输入沿用各自接口。

## 步数约束与批次失败隔离（2026-09-12）

方向选择接收已均匀抽取并保存的edit_count，要求目标在N个单能力Edit内形成有用的新产品；按步数调整目标范围，能力继续逐步产生。

产品Session批量入口隔离单Seed的内容、格式、局部超时及验收失败，保留成功前缀和失败证据后继续下一Seed；全部派发结束记completed或completed_with_failures并记录成功/失败数。鉴权/额度、API有界重试耗尽、明确运行时或数据完整性故障和用户中断暂停整批。session_failure.json记录当前失败范围与证据，batch_state.json保存各Seed结果；恢复忽略旧尝试的失败标记。

target_routes和browser_check若只放错到顶层，可等价移回edit；内外字段冲突或未知字段仍按原规则拒绝，保存规范化证据并复用模型响应。Harness解释器保留虚拟环境入口的绝对路径，不解析Python符号链接到基础解释器。

## 六类自动导出与子任务数量（2026-09-13）

每个已验收状态导出 Text/Image Generate；每次成功 Edit 导出 Text/Image 原子 Edit；同一轨迹额外导出所有长度为4–12的连续 Edit 窗口。N步链的组合数为 sum(N-k+1, k=4..N)，8步为15条组合，12步为45条组合；原子记录全部保留，组合输入按时间顺序列出原指令，源码取窗口起点，答案为起点到终点的可回放净patch。窗口内允许前序能力依赖，新增文件使用官方空search格式。

自然Repair保持同一故障版本→一次Repair→复测通过。问题数按独立问题计，不按类别去重，不把同一问题的多状态检测重复计数；不同控件的属性缺失分别计项，共享根因的布局症状由同一次Repair响应分组，绑定具体失败位置。同一故障修好的全部问题完整导出，1–3项或超过12项保留在补充池；不跨版本拼故障、不为配额注错。训练Repair公开输入为官方11类公共定义＋N＋故障源码，具体故障说明、位置和标签仅保留在内部元数据及Harness修复输入。

Image Generate仅输入目标图和页面/状态说明；Image Edit仅增加source图；Image Repair顺序为全部current图→全部target图。图片必须来自相应Git版本，使用整页截图覆盖项目所有HTML页及本条已有流程涉及的状态。属性/语义修复没有可见变化时保留Text Repair，并在image_skips记录原因；不制造视觉差异。图与源码通过哈希、页面清单和交互映射绑定，原始资源随版本保存。图片路径相对六类出口根目录解析。

出口是dataset/six_tasks/dataset_index.json；仅消费该索引指向的六类不可变JSONL分片，不glob目录中的历史分片。每行含messages、images/input_images、response及隐藏metadata，Edit/Repair答案为官方XML search_replace，Generate答案为完整项目Markdown。图片/资源按内容版本缓存，恢复校验哈希后复用，最后原子更新索引。

按官方600条Edit/Repair的实际4–12项频数保存sampling索引；单页/多页各占官方任务的50%。保留全量样本，并为数量对齐部分计算权重；批入口汇总所有Session后重新计算全局权重和缺档。数量对齐与16/11类标签对齐分别标记，扩展Edit类别保持原标签。原子补充池与对齐部分的训练混合比例由消费侧配置，当前不丢数据、不强制抽样。由同一原始源码派生的样本共用lineage_group，训练/验证按组划分。官方频数和源文件SHA在Harness的src/orchestration/webcompass_subtask_distribution.json。

## 宿主接入与持续改进

先从现有源码定位真实入口、数据源、目标 DOM 容器和事件；保留宿主容器结构与已有状态，不以字段名、DOM 层级或 HTML 序列化字符串做硬编码验收。挂载时传入现有 refs/state 与回调，隐藏状态沿用宿主的可见性/路由机制；组件销毁时调用 `destroy()` 并移除监听，避免全局快捷键冲突、重复挂载和 stale 状态。示例：

```javascript
const instance = mount({ container: existingRegion, initialValue: existingState.value, onChange: value => updateExistingState(value) });
view.addEventListener('beforeunload', () => instance.destroy(), { once: true });
```

验收采用一条最短因果路径，先建立前置状态，再执行核心操作并断言内容、状态和提交结果；必要时只补一个失败/恢复分支。失败反馈必须包含失败动作、预期/实际、源码位置和直接浏览器证据，并区分已确认根因与推测。Harness 会把可复用的宿主接入或参考实现问题写成待验证 Skill 改进；只有同一真实样本和受影响回归行为均通过后才启用新版本，运行中的任务继续使用原版本。不得写入样本专属答案或删除有效检查。
