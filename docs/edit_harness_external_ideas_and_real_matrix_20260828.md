# Edit Harness 外部建议整理与真实矩阵（2026-08-28）

## 结论与证据边界

要解决的问题是：让模型在既有前端项目中完成一次明确的增量修改，同时把“该改什么、从哪里开始改、什么时候可以扩大范围、哪些旧能力必须保持”都变成 harness 可执行的约束，并从 accepted Edit 历史派生 Generate 和自然 Repair 数据。

本文有三类证据，不能混用：

1. 用户提供的 ChatGPT 回答是外部调研线索；本文未逐一联网核验其中产品功能、论文版本或市场定位，不把产品宣传当成当前事实。
2. “已实现”只指本仓库当前源码和单元测试。
3. “真实矩阵”指 0805 supplement 中 6 个真实 source/ground-truth Edit 样本在本机 Chromium 和 Git 门禁上的零 LLM 小规模验证；它不是模型端到端能力评测。第六例保留真实四 HTML source 与行为补丁，但用明确标注的受控 target-scoped CSS 修复替代原 GT 的通用 CSS，只用于验证门禁，不能自动作为自然 Edit/Repair 数据出口。

## 用一个例子说清流程

输入是一套已有的多页博物馆管理站：Dashboard 与 Report 各有页面和脚本，共用一个 `store.js`。本轮用户只要求两个页面各增加分页，而且 Staff 页面必须保持不变。

处理过程：

1. Planner 把 `/` 与 `/report.html` 写成同一 Edit 的两个目标路由，并给出真实点击、计数、页码和 Staff sentinel。
2. Harness 为两个目标路由各选一个初始入口，而不是只允许 Dashboard 抢占唯一入口。
3. 页面 HTML/JS 沿已有依赖边逐步开放；共享 Store 只允许增加 `dashboardPage`、`reportPage`、`pageSize` 及相应 setter，禁止删除既有成员或引入与目标无关的名字。
4. Playwright 注入 5 条 program 和 5 条 log 的确定性 localStorage 状态，分别点击两页的 Next，检查 `4 -> 1` 的列表变化和 `Page 1 of 2 -> Page 2 of 2`。
5. Staff 页的既有标题作为非目标 sentinel；Git diff 还必须通过机械检查。

输出是一个通过的五文件 canonical Edit 候选；之后它才能成为下一 accepted Seed，并派生累计需求的 checkpoint Generate。若中途某版本能显示分页但 Next 不可用，则保存该失败版本、浏览器证据和后续恢复，形成同 Sprint 的自然 Repair。

## 用户提供回答中的可借鉴机制

外部回答列举了 Frontman、Kombai、Builder Fusion、stagewise、Onlook、Rivet、Tempo、OpenMagic、eRegion 等系统，以及 WebDesignIter、Web-Bench、Instruct4Edit、MT-Web2Code、WebCompass、SWE-bench Multimodal、GUIRepair、1D-Bench 等研究方向。去掉产品名后，真正值得借鉴的是六个机制：

| 机制 | 对本项目的意义 | 当前处理 |
| --- | --- | --- |
| 运行时元素到源码定位 | 从用户点中的元素、组件、路由和事件处理器缩小源码搜索空间 | 静态 route/selector/import ownership 已有；运行时 source map/event-listener grounding 尚未实现 |
| 组件实例与共享定义区分 | “只改这个实例”和“改整个组件系统”应有不同权限 | 目前以路由本地文件、共享文件 named region、受保护路由区分；React instance-level ownership 尚未实现 |
| 设计系统感知 | 修改应复用既有 token 与组件语言 | 已增加 CSS custom property 清单和 Generator 复用提示；全局 CSS 不因发现 token 而自动放行 |
| selector-aware CSS ownership | 共享样式只能影响目标实例，不能借通用 class 波及受保护页 | 已实现强 ID/data anchor、每个 selector 分支检查、兄弟逃逸/伪类间接锚定拒绝和 protected-route anchor 冲突检查；at-rule 与现代 CSS nesting 修改 v1 继续关闭 |
| 多 HTML 页面所有权 | 多个 HTML 入口需要独立授权和回归保护 | 已按精确 pathname 建立入口与传递依赖；导航链接不算源码依赖，共享 CSS/JS 分成 target-shared、cross-route shared 和 off-target |
| 真实状态与多 viewport 验证 | 不能只看初始桌面页面 | 已支持 storage fixture、reload、viewport/media 和 computed style；状态图覆盖仍有限 |
| Git diff/分支/恢复 | 每轮修改应可追踪、可撤回、可形成 failure→recovery 谱系 | 现有 checkpoint、Git journal、append-only ledger、Repair packet 和 exporter 已覆盖主要链路 |
| 低轮次证据驱动修复 | 用精确失败直接修，不重复全项目探索 | 最多 10 轮但通过即停；确定性失败绕过付费语义 judge，形成有界 Repair packet |

## 本轮落实的改动

| 原问题 | 本轮实现 | 保护边界 |
| --- | --- | --- |
| hash-router 页面无法进入路由 ownership | 识别字面 `registerRoute('/report', renderReport)`，内部规范化为 `/#/report`；Edit contract 与浏览器只接受有界同源 hash | 动态拼接、查询串、外站和 `..` 仍拒绝 |
| 浏览器只能依赖 seed 自带数据 | 新增 `set_storage_value`，支持 bounded local/sessionStorage JSON 或字符串状态，随后可 reload | 单值上限 32 KiB，不执行模型 JavaScript |
| DOM 出现不代表必要样式生效 | 新增 `assert_computed_style`，只开放 display、visibility、opacity、position、overflow、pointer-events、z-index 等少量属性 | 它不是完整视觉审美 oracle；视觉任务仍需要条件式截图/vision |
| Generator 不知道项目已有 token | 最小路径计划记录 custom property、定义文件与使用次数；prompt 要求先复用 | 不自动开放全局样式文件 |
| 多路由 Edit 只给一个初始路径 | 改为每个目标路由一个排名最高入口 | 仅适用于同一产品语义的连贯多路由 Edit；独立需求仍应拆分 |
| 共享 Store 被整文件关闭 | 对可机械定位的 State/Store object/class 增加 `additive_target_members` | 只能增加目标命名成员；删除/替换已有标识符或无关新成员失败 |
| 默认 3 文件不足以覆盖合理的两页功能 | 默认 touched-file ceiling 调整为 6 | 仍受 route ownership、dependency edge、exact patch 和逐次 validation 约束，不是 6 文件白名单 |
| 候选代码质量失败被误记成 harness error | 矩阵把 `git diff --check` 等候选失败归为 `rejected` | 真正异常继续记为 `error` |
| 共享 CSS 只能整文件关闭 | 新增 `target_scoped_css` guarded region；每次 patch 都在 before/after 两侧重新解析完整顶层规则 | 所有 selector 分支必须含目标 ID 或 `data-testid`/`data-page`；通用、混合、相邻兄弟、伪类间接锚点和 at-rule 修改拒绝 |
| 多 HTML 只被笼统称作“多页” | 每个 HTML pathname 分别拥有自己的入口、脚本与样式传递闭包；每个目标页得到一个最短初始入口 | 非目标 HTML 保持 protected；导航 href 不会错误开放另一页源码 |

## 真实样本矩阵

数据源：`WebCoding_Data/output/0805_supplement_release_cache/text-edit.jsonl.gz`。最终统一矩阵证据目录：`logs/edit_matrix_20260828/real_edit_matrix_20260828T115939/`；增加 CSS nesting 拒绝后，最终 CSS 成对复核目录为 `logs/edit_matrix_20260828/real_edit_matrix_20260828T120530/`；自然 Repair 复核：`logs/edit_first_20260828/webcompass_pagination_20260828T120113/`。更早的同日矩阵属于问题定位过程，不作为下表的最终结果。

| 真实 case | 结果 | 发现的问题 | 应采取的处理 |
| --- | --- | --- | --- |
| shared-file discovery | GT 候选拒收；单独 Repair 流程通过 | GT 每页 6 条，seed 恰有 6 条，Next 仍 disabled；另有新增行尾空格 | 失败证据进入 Repair；`itemsPerPage: 6 -> 3` 后真实浏览器恢复。不能把原 GT 当 accepted Edit |
| inline summary | 目标与保护检查通过，候选拒收 | `git diff --check` 发现新增行尾空格 | 作为机械质量失败，不记 harness error，不进入正式出口 |
| My Tickets | 目标与保护检查通过，`non_minimal` | 6 个 atom 中 `p005` 是与分页无关的 cancel toast；移除后目标/保护仍通过 | 反事实证书拒收原候选，并给出精确冗余 atom |
| Dashboard + Report | accepted | 原 harness 只开放一个路由入口并卡住第二页和共享 store；修复后 8/8 patches 获准，5 次 Git validation 通过，3 个 browser checks 通过 | 证明多路由初始入口和共享状态定向增量策略在该真实样本上有效 |
| 四 HTML Log | accepted（受控门禁样本） | `index.html`、`dispatch.html`、`log.html`、`roster.html` 共享样式；target-root 的 `#log-pagination ...` 规则获准，首页 sentinel 与分页行为均通过 | 证明多 HTML ownership 与 selector-aware 共享 CSS 可以同时工作；受控 CSS overlay 单独标记，不能冒充自然 GT |
| hash-router Report | 候选拒收 | 行为 DOM 可通过，但 GT 想向全局 CSS 加通用 `.page-btn` 等规则；该文件属于所有页面，且新增行有空格。computed style 复核显示目标视觉属性未生效 | selector-aware guard 仍拒绝通用 selector；只有改成 `#report-pagination .page-btn` 等 target-rooted rule 才可能进入浏览器验收 |

主矩阵最终为 `ok=2, rejected=4, error=0`，LLM 调用与费用均为 0。这个比例不是模型成功率；矩阵没有调用模型，其中新增 accepted 项是受控门禁案例。结果说明 admission gate 能同时放行目标锚定的共享样式，并挡住错误行为、机械瑕疵、通用全局样式越界和无关功能。它尚不能证明模型在 10 轮内的真实 Edit 产率。

测试证据：selector/multi-page/generator/planner 定向测试 124/124 通过；仓库全套为 707 passed、2 skipped。两条 skipped 和 aiohttp deprecation warning 未被本轮变更转化为功能结论。

## 结合外部建议后，下一步最值得做什么

1. **selector-aware CSS v2。** 当前顶层规则保护已实现；下一步需要用成熟 CSS selector AST 覆盖 `@media`/container/layer 嵌套，并把 declaration token 偏离作为独立警告或门禁，仍保留 fail-closed 回归对照。
2. **运行时元素到源码 ownership。** 在 development build 中记录 DOM 节点对应 React/Vue component、source map、event listener 和加载模块；把静态候选与一次真实点击 trace 取交集。该层只缩小搜索顺序，不能单独授权修改。
3. **组件实例级保护。** 当同一个 React component 在多个路由复用时，保存 `route + component + stable props/data key`。只改一个实例时优先改调用点/配置；只有任务明确要求全局变化时才开放组件定义。
4. **状态图而非单状态。** 对 accepted action tape 保存最小状态夹具、动作和后置断言；选择目标状态邻居与一个受保护状态邻居。用 property-based 生成后再 shrink，避免无限枚举。
5. **反事实搜索按语法层级分组。** 当前 exact hunk atoms 已能发现 toast；下一步按 HTML element、CSS rule、JS function/handler 分组删除，减少因语法损坏造成的“伪必要”。
6. **用数据指标证明创新，而不是用工具名。** 至少比较同等 LLM 预算下：普通 agent prompt、当前静态 ownership、增加 runtime grounding 三组的 accepted Edit 率、无关修改率、平均轮次、浏览器成本、Repair 自然度和下游训练收益。

## 暂不采纳的做法

- 不把截图或像素 mask 作为非目标保护的主 oracle；DOM/ARIA/action/state/source ownership 更能说明交互和语义，截图只补视觉任务。
- 不因某产品宣称“可在现有项目里编辑”就直接集成；先核验其可用接口、源码定位证据和导出边界。
- 不把全局组件或全局 CSS 一次性放开；先证明本轮目标实例/selector 的 ownership。
- 不为凑 Repair 数据注入假 bug，也不恢复启发式 SFT 分类器作为正式出口。
- 不把 10 轮当配额。一次 accepted Edit 可在第 1 轮结束；后续轮次必须由同 Sprint 的精确失败证据驱动。

## 成本解释

本轮验证使用真实本地数据、Git 和 Chromium，LLM/vision 调用均为 0，因此 API 成本为 0。主矩阵中 My Tickets 的反事实证书执行了 16 个隔离浏览器候选，这是本轮最主要的时间成本。正式生产的主要付费来源仍是 Planner/Generator 和必要时的语义/视觉评分；storage、DOM、hash、computed style、Git、route ownership 和多数回归 tape 都是先行的本地确定性门禁，应在付费调用前尽量淘汰不合格候选。
