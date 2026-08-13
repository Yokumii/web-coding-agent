# WebCoding Harness 战略调研与改造报告（2026-08-11）

## 结论先行

当前 harness 已从“LLM 生成 + LLM/截图评分”升级为一个以 **edit 为主线的数据证明系统**：

1. 任务目标由 planner 写成可执行 action contract，且每条检查必须以浏览器断言结束；
2. 每个 edit sprint，以及每个可渲染 repair 的失败源，在修改前冻结自己的语义 DOM/ARIA frame，避免把之前已验收的 sprint 或无关失败源区域误判为本轮改动；
3. harness 从 action selector、DOM anchor 和源码依赖计算允许变化的 semantic surface 与 source change cone；
4. OpenAI/Claude 两条工具链在写入前强制 exact patch、路径、文件数与依赖扩展策略，并记录 ledger；
5. 通过普通 evaluator 后，harness 把 Git 变化拆成精确、可重放的 patch atoms；
6. 在隔离项目中逐组、逐个删除 atoms，真实启动 Chromium，重放目标契约和保护契约；
7. 只有源版本不能完成目标、完整目标版本通过、且剩余每个 atom 都经反事实证明不可删除时，才生成 `certified` 证书；
8. 新策略启用后的 edit/repair 没有相应证书，就不能进入数据导出。

这套机制称为 **UI Change Cone + Counterfactual Patch Certificate（UI 变化锥 + 反事实补丁证书）**。它不是像素 mask，也不依赖 agent 自报“我只改了必要部分”。截图仍可用于外观评分，但不承担 edit 保护或最小性证明。

当前判断：**架构方向符合预期，机制已接入主流程并通过真实轨迹校准；但只完成小规模机制验收，尚不能声称大规模数据质量或模型训练收益。**

## 一、问题定义

高质量 WebCoding edit/repair 数据需要同时满足四个条件：

- `Target`: 用户请求的新增或修复行为可在浏览器中观察到；
- `Frame`: 未授权 DOM/ARIA surface 与已验收交互不发生语义回归；
- `Replay`: 从源代码应用精确 patches 后能逐字得到目标代码；
- `Minimality`: 原始 patch 中不存在仍可删除、而 `Target ∧ Frame` 继续成立的 atom。

因此，最小性不是 `changed_lines < N`，而是：

```text
Pass(C') = Target(C') ∧ Frame(C')

Minimal(P) ⇔ Pass(apply(C, P))
             ∧ ¬Pass(apply(C, P \ {p}))  for every p in P
             ∧ ¬Target(C)
```

最后一项 `¬Target(C)` 很关键：如果源版本已经通过目标契约，这条 edit 的测试没有区分力，不能因为目标版本也通过就制造一个正例。

## 二、52 篇论文调研

调研采用“机制 → 本项目落点 → 边界”的方式。WebCoding 论文用于对齐任务分布、运行形态和评测；其他领域用于寻找局部推理、程序约简、修复、测试 oracle、GUI 状态抽象和测试强度校准的方法。以下是 16 + 36 篇，而不是只列标题的书目。

### A. WebCoding / 前端代码生成与评测（16 篇）

| # | 论文 | 可借鉴机制 | 在本项目中的落点 | 边界 |
|---:|---|---|---|---|
| 1 | [WebCompass: Benchmarking Web Coding Agents Across the Development Lifecycle](https://arxiv.org/abs/2604.18224) (2026) | generate/edit/repair 全生命周期、跨模态任务、agent-as-judge、可重放 edit | 统一 TaskSpec 与 WebCompass-like 导出；edit 作为主轨迹，repair 保留自然失败来源 | taxonomy 对齐不等于质量对齐；仍需真实浏览器证据 |
| 2 | [WebGen-Bench](https://arxiv.org/abs/2505.03733) (2025) | 多文件项目、自动测试和 agent trajectory | 保留完整 source/destination、工具轨迹、浏览器结果 | 主要面向生成，不能直接证明 edit 非目标区域未变 |
| 3 | [ArtifactsBench](https://arxiv.org/abs/2507.04952) (2025) | 对可交互 artifacts 做视觉与功能综合评估 | 将外观评分和功能契约分离并同时保存 | 综合分数会掩盖局部回归，不能替代 frame contract |
| 4 | [Design2Code](https://arxiv.org/abs/2403.03163) (2024) | 真实网页与细粒度视觉指标 | 作为 image-conditioned/edit 的视觉质量侧证 | 视觉相似不等于交互正确，也不能证明最小修改 |
| 5 | [WebCoderBench](https://arxiv.org/abs/2601.02430) (2026) | 多维、可解释的 WebCoding 指标 | 把证书状态、目标通过、保护通过、patch replay 独立暴露 | 维度多仍可能共享同一个弱 oracle |
| 6 | [FrontendBench](https://arxiv.org/abs/2506.13832) (2025) | Puppeteer/Jest 可执行 prompt-test 对 | planner 产出浏览器 action contract；拒绝仅自然语言验收 | 人工测试仍可能遗漏行为，需 mutation calibration |
| 7 | [DesignBench](https://arxiv.org/abs/2506.06251) (2025) | 多框架 generate/edit/repair | 不迁移 seed 技术栈；patch guard 与框架无关 | 跨框架状态归一化仍不完全 |
| 8 | [Interaction2Code](https://arxiv.org/abs/2411.03292) (2024) | 用交互轨迹描述和评估页面行为 | action tapes 成为任务目标的隐藏 oracle | 录制轨迹必须避免 selector 脆弱性和答案泄漏 |
| 9 | [FronTalk](https://arxiv.org/abs/2601.04203) (2026) | 多轮反馈、历史约束重检、遗忘分析 | repair 后重放当前 sprint 目标和保护 frame | 全历史全量重放成本高，需要分层选择 |
| 10 | [SWE-bench Multimodal](https://arxiv.org/abs/2410.03859) (2024) | 带视觉证据的真实 JS 仓库 issue | 保留失败截图作为 repair 来源，但修复验收用行为+语义 | issue 级修复通常比单页面 edit 更复杂 |
| 11 | [WebApp1K](https://arxiv.org/abs/2408.00019) (2024) | 1,000 个 React user journeys，成功/失败测试 | 每个 sprint 规划一条有状态 user journey | 单框架分布不能覆盖静态 HTML/Vue/Angular 等 |
| 12 | [Web-Bench](https://arxiv.org/abs/2505.07473) (2025) | 同一项目上的顺序依赖任务 | 每 sprint 单独冻结 source frame，避免前序 edit 污染本轮判定 | 顺序轨迹累计后仍需 release 级去重与分布控制 |
| 13 | [IWR-Bench](https://arxiv.org/abs/2509.24709) (2025) | 视频到交互页面、动作级评测 | image/video 输入最终也落到可执行状态转换 | 视频外观对齐成本高，当前证书只覆盖代码行为 |
| 14 | [WebSight](https://arxiv.org/abs/2403.09029) (2024) | 大规模合成 screenshot-HTML 配对、Playwright 渲染 | 适合作为 generate seed/视觉侧数据来源 | 合成分布与真实交互/可维护代码存在差距 |
| 15 | [Web2Code](https://arxiv.org/abs/2406.20098) (2024) | screenshot、指令、HTML、QA 联合数据 | 可扩展为 image-edit 的 source image + action contract | QA/视觉一致仍不足以约束未编辑区域 |
| 16 | [pix2code](https://arxiv.org/abs/1705.07962) (2017) | 早期 GUI image-to-code 序列建模 | 作为视觉生成路线的历史基线 | 主要是静态 DSL/像素目标，不适合承担现代 Web 行为 oracle |

### B. 其他领域但可直接迁移的工作（36 篇）

#### B1. 约简、切片与变化影响（9 篇）

| # | 论文 | 可借鉴机制 | 在本项目中的落点 | 边界 |
|---:|---|---|---|---|
| 17 | [Simplifying and Isolating Failure-Inducing Input](https://www.st.cs.uni-saarland.de/publications/files/zeller-tse-2002.pdf) (Zeller & Hildebrandt, 2002) | `ddmin` 用通过/失败 oracle 约简输入 | 成组删除 patch atoms，再做逐 atom 必要性检查 | oracle 非确定时结果不可靠，因此 infra error 不得算“必要” |
| 18 | [HDD: Hierarchical Delta Debugging](https://doi.org/10.1145/1134285.1134307) (2006) | 按语法树层级约简 | 后续从行 hunk 升级为 HTML/CSS/JS AST 层级 atoms | 不同语言和模板混合需要统一语法层 |
| 19 | [Test-Case Reduction for C Compiler Bugs](https://www.cs.utah.edu/~regehr/papers/pldi11-preprint.pdf) (C-Reduce, 2012) | 多种保持“interestingness”的变换级联 | 先做确定性安全归一化，再跑反事实删除 | 编译器用例与有状态 UI 的 oracle 成本差异很大 |
| 20 | [Perses: Syntax-Guided Program Reduction](https://doi.org/10.1145/3338906.3338926) (2019) | 语法有效的程序约简 | 防止删除 atom 后只因语法损坏而产生廉价“必要性” | 需要对应框架 parser；当前 v1 仍是精确 hunk |
| 21 | [DRReduce](https://arxiv.org/abs/2605.19412) (2026) | 约简时重建依赖，保留可执行性 | HTML 元素、CSS selector、JS handler 作为依赖组 | 重建可能引入新代码，必须保留来源标记 |
| 22 | [Program Slicing](https://doi.org/10.1145/800078.802557) (Weiser, 1981) | 针对某个行为点保留相关语句 | 从 target selector/handler 建立静态 change cone | 动态 JS、反射和框架编译会降低静态精度 |
| 23 | [Dynamic Program Slicing](https://doi.org/10.1016/0020-0190(88)90054-3) (Korel & Laski, 1988) | 对一次具体执行生成更小切片 | 利用真实 action tape 的执行轨迹缩小候选文件/handler | 只覆盖被执行路径，不能保护未触达状态 |
| 24 | [Whole Program Path-Based Dynamic Impact Analysis](https://doi.org/10.1109/ICSE.2003.1201188) (2003) | 根据执行路径估计变化影响 | 用点击/输入路径建立 edit 影响域和回归重放优先级 | 浏览器运行时与源码映射需要 instrumentation |
| 25 | [Differential Symbolic Execution](https://doi.org/10.1145/1453101.1453131) (2008) | 只分析两个程序版本之间的语义差异 | 长期可用于 JS handler 级 source/destination 差分 | 对真实 DOM、异步和第三方 API 的可扩展性有限 |

#### B2. 自动程序修复与 edit 表示（8 篇）

| # | 论文 | 可借鉴机制 | 在本项目中的落点 | 边界 |
|---:|---|---|---|---|
| 26 | [Automatically Finding Patches Using Genetic Programming](https://doi.org/10.1109/ICSE.2009.5070536) (GenProg, 2009) | 测试驱动搜索 patch | repair 必须由真实失败→通过轨迹定义 | 过拟合测试是核心风险 |
| 27 | [SemFix: Program Repair via Semantic Analysis](https://research.ibm.com/publications/semfix-program-repair-via-semantic-analysis) (2013) | 约束求解得到修复表达式 | 用 target/frame 双约束限制 repair 搜索 | UI oracle 很难直接变成可解公式 |
| 28 | [Angelix](https://discovery.ucl.ac.uk/id/eprint/1477702/) (2016) | angelic forest 与符号修复 | 失败 action 指向具体状态/handler，而非整页重写 | 环境交互与异步副作用仍难符号化 |
| 29 | [Automatic Patch Generation by Learning Correct Code](https://people.csail.mit.edu/rinard/paper/prophet-popl16.pdf) (Prophet, 2016) | 从历史正确 patch 学习排序 | 用 certified patches 反哺 patch proposal/ranking | 未认证历史 patch 会把宽泛修改学进去 |
| 30 | [Getafix: Learning to Fix Bugs Automatically](https://arxiv.org/abs/1902.06111) (2019) | 分层聚类 edit patterns | 对 certified repair 聚类，不混入 edit/generate | 模式频率不等于语义正确 |
| 31 | [TBar](https://arxiv.org/abs/1903.08409) (2019) | 模板化、上下文感知 repair | 常见前端缺陷可形成受控 repair operator | 模板只覆盖已知 bug family |
| 32 | [A Syntax-Guided Edit Decoder for Neural Program Repair](https://arxiv.org/abs/2106.08253) (Recoder, 2021) | 生成 edit 而不是整文件，语法引导 | 训练输出保持 exact patches，避免全文件重写 | 需要可靠 AST 与跨文件 edit 表示 |
| 33 | [SequenceR](https://arxiv.org/abs/1901.01808) (2019) | copy mechanism 和序列化单行修复 | 鼓励复制源代码上下文，只生成局部 replace | 单行假设不足以覆盖 HTML/CSS/JS 联动 edit |

#### B3. 软件工程 agent（3 篇）

| # | 论文 | 可借鉴机制 | 在本项目中的落点 | 边界 |
|---:|---|---|---|---|
| 34 | [SWE-agent](https://arxiv.org/abs/2405.15793) (2024) | Agent-Computer Interface 会显著影响结果 | 限制工具、稳定路径、原子提交、持久证据 | 仓库 issue 成功率不能外推到视觉 Web edit |
| 35 | [Agentless](https://arxiv.org/abs/2407.01489) (2024) | localization → repair → validation 的可解释流水线 | 将语义 scope、patch、oracle 分成独立阶段 | 不适合所有需要探索式 UI 设计的任务 |
| 36 | [OpenHands](https://arxiv.org/abs/2407.16741) (2024) | 沙箱、事件流、可扩展 agent 平台 | 每个反事实候选放入隔离运行目录并留日志 | 平台能力本身不保证任务/数据定义正确 |

#### B4. Test oracle、生成与强度校准（7 篇）

| # | 论文 | 可借鉴机制 | 在本项目中的落点 | 边界 |
|---:|---|---|---|---|
| 37 | [Dynamically Discovering Likely Program Invariants](https://homes.cs.washington.edu/~mernst/pubs/invariants-tse2001.pdf) (Daikon, 2001) | 从成功执行中挖掘 likely invariants | 从 accepted action tapes 学习候选 DOM/状态不变量 | “likely” 不能未经 mutation 验证就成为硬门禁 |
| 38 | [Metamorphic Testing: A New Approach for Generating Next Test Cases](https://arxiv.org/abs/2002.12543) (1998/2020 archive) | 在无精确 oracle 时验证输入输出关系 | viewport、输入顺序、刷新/返回后的关系型不变量 | 错误 metamorphic relation 会产生系统性误报 |
| 39 | [Feedback-Directed Random Test Generation](https://doi.org/10.1109/ICSE.2007.37) (Randoop, 2007) | 用执行反馈避免无效序列 | 从 DOM 可操作元素扩展 action tapes | 随机序列不天然代表真实用户任务 |
| 40 | [Whole Test Suite Generation](https://doi.org/10.1109/TSE.2012.14) (EvoSuite, 2013) | 多目标覆盖并在末端压缩 test suite | 兼顾目标行为、保护 surface、动作数和运行成本 | coverage 不是产品正确性的替代品 |
| 41 | [DART: Directed Automated Random Testing](https://doi.org/10.1145/1065010.1065036) (2005) | 随机执行 + 符号约束探索路径 | 对 JS handler 输入边界做定向探索 | 浏览器 API 与事件循环难完整符号化 |
| 42 | [KLEE: Unassisted and Automatic Generation of High-Coverage Tests](https://www.usenix.org/legacy/event/osdi08/tech/full_papers/cadar/cadar.pdf) (2008) | 高覆盖符号执行、错误路径证据 | 借鉴“每个失败带具体路径条件”的证据组织 | 原生 C/LLVM 技术不能直接迁移到 DOM 应用 |
| 43 | [QuickCheck: A Lightweight Tool for Random Testing of Haskell Programs](https://doi.org/10.1145/351240.351266) (2000) | property-based generation 与 shrinking | 为 UI 状态生成输入，并把失败动作序列 shrink 成最短 tape | 属性仍需人工/LLM 正确定义 |

#### B5. Web/GUI 状态、局部不变量与测试框架（7 篇）

| # | 论文 | 可借鉴机制 | 在本项目中的落点 | 边界 |
|---:|---|---|---|---|
| 44 | [Crawling Ajax-Based Web Applications through Dynamic Analysis of User Interface State Changes](https://repository.tudelft.nl/record/uuid%3A50ee5cfe-7f48-4309-b90a-6a9a1fa2326c) (Crawljax, 2012) | 触发 UI 事件并增量推导状态机 | 从 action contracts 扩展 protected state graph | 状态爆炸与近重复页面需要抽象 |
| 45 | [Fragment-Based Test Generation for Web Apps](https://arxiv.org/abs/2110.14043) (FRAGGEN, 2021/2022) | fragment 级状态等价和稳定 regression oracle | semantic surfaces 不做整页像素/HTML diff | fragment 算法需要动态页面归一化 |
| 46 | [Using Tree Kernels to Detect Near-Duplicate States](https://arxiv.org/abs/2108.13322) (2021) | DOM tree kernel 判断近重复状态 | 作为 exact fingerprint 之外的审计/召回层 | 相似阈值不应直接决定 edit 通过 |
| 47 | [A Framework for Automated Testing of JavaScript Web Applications](https://www.franktip.org/pubs/icse2011artemis.pdf) (Artemis, 2011) | feedback-directed JavaScript test generation | 依据运行反馈补充关键 action path | 自动覆盖仍可能缺少产品语义断言 |
| 48 | [GUI Ripping: Reverse Engineering of GUIs for Testing](https://www.cs.umd.edu/~atif/papers/MemonWCRE2003.pdf) (2003) | 从可执行 GUI 反向抽取 event-flow graph | 不依赖源码框架构建 UI 状态模型 | 自动点击有副作用，必须限制安全动作 |
| 49 | [GUITAR: An Innovative Tool for Automated Testing of GUI-Driven Software](https://www.cs.umd.edu/~atif/pubs/NguyenASE2013-abstract.html) (2013) | GUI model、test generation、execution、oracle 一体化 | 对应 planner/runner/frame/export 的分层结构 | 桌面 GUI 经验需适配浏览器异步与响应式布局 |
| 50 | [Invariant-Based Automatic Testing of AJAX User Interfaces](https://repository.tudelft.nl/record/uuid%3A443976bc-9a43-4351-8eef-e7dfcf47bb94) (2009) | DOM invariants + state-flow graph + plugin validator | 当前 DOM/ARIA frame guard 的直接理论支撑 | generic invariants 不能替代任务特定行为 |

#### B6. 局部推理与 mutation calibration（2 篇）

| # | 论文 | 可借鉴机制 | 在本项目中的落点 | 边界 |
|---:|---|---|---|---|
| 51 | [Separation Logic: A Logic for Shared Mutable Data Structures](https://www.cs.cmu.edu/~jcr/seplogic.pdf) (Reynolds, 2002) | frame rule：局部命令应保留不相交状态 | 将“允许变化 surface”视作 change footprint，其余 DOM/ARIA 是 frame | Web DOM 共享事件/全局 CSS 并非真正分离，需要运行时验证 |
| 52 | [Trivial Compiler Equivalence](https://discovery.ucl.ac.uk/id/eprint/1499169/) (2015) | 快速识别等价/重复 mutants | 先用便宜规范化筛掉等价 atoms，再跑昂贵 Chromium；用 mutants 测 oracle 强度 | 编译后二进制等价不能直接用于 HTML/CSS 行为等价 |

## 三、从调研收敛出的架构

```text
accepted source C
   │
   ├── planner target contract T
   ├── per-sprint semantic frame F
   ├── harness-derived DOM footprint S (≤2 source surfaces)
   └── source change cone G (hotspots → recorded dependencies)
   │
tool-gated edit trajectory
   ├── existing source overwrite / unrelated path → denied
   ├── exact local patch → allowed + ledger
   └── recorded import/link edge → dependency widening + ledger
   │
agent candidate C' + exact patches P
   │
   ├── normal evaluator / visual review
   ├── patch replay: apply(C, P) == C'
   └── counterfactual reducer
         ├── empty subset must fail T and pass F
         ├── full P must pass T and F
         ├── ddmin removes groups that still pass T ∧ F
         └── delete each remaining p once
   │
   ├── certified: no removable atom → export eligible
   ├── non_minimal: redundant atoms → repair/reject
   ├── invalid_contract: C already passes T → do not fabricate edit
   └── inconclusive: infra/flaky/too broad → do not treat as product repair
```

### 为什么这是战略级而不是 prompt tweak

- scope 与 source cone 由 harness 计算，OpenAI/Claude 两条工具链都在写入前执行同一门禁；
- agent 无法通过文字承诺绕过在线门禁或最终证书；
- “未改区域”来自运行时语义 frame，不是截图相似度；
- 文件/行预算只负责引导搜索路径；最终“是否还能更少”仍来自反事实重放，而不是阈值；
- 测试本身也被验证：源版本若已通过，任务契约被拒绝；
- edit 与 repair 同时有证书，但 source 定义不同：
  - edit：该 sprint 最初 accepted source → 最终 destination；
  - repair：真实失败 commit → 修复 destination；
- exporter 是最后一道硬门禁；缺证书的新策略运行不会静默混入旧数据。

## 四、当前落地

### 已实现

- `src/orchestration/minimal_path_guidance.py`
  - 从 action selector、DOM anchor、源码命中和 import/link 边生成 harness-owned change cone；
  - 每条 check 的同源 route 先解析为页面入口与传递依赖所有权；静态 HTML、具体的 Next/filesystem route 和显式 React Router literal mapping 可机械识别；`<a href>` 仅作为导航，不再错误合并为源码依赖；
  - 路径分为 target-route local、仅目标路由共享、跨目标/保护路由共享、完全 off-target 四类；只有前两类可进入 change cone，Next layout/global CSS 等隐式 shell 归属于其包裹的全部路由；
  - 根据 typed action/category 对 behavior、markup、style 源码分层排序，只开放一个 initial path；
  - 以 `inspect → exact patch → validation → recorded-neighbor expansion` 状态机逐级开放路径，import/link 可双向遍历但不能跳边；
  - 禁止已有源码整文件覆盖、未规划新文件、非唯一 exact patch、过宽 patch 与 Bash 旁路修改；最后一次修改未成功验证时禁止 commit；
  - OpenAI 原生工具与 Claude SDK pre/post-tool hooks 共用 policy，并以实际工具结果而不是预授权推进状态；
  - append-only ledger 记录 read/applied/validation/deny，live state 记录 phase、unlocked paths 与 next action，dataset exporter 分别保存引导 provenance。
- `src/orchestration/minimal_patch_guard.py`
  - exact atomic patch；
  - ddmin + exhaustive one-deletion；
  - source discriminativity；
  - infra error 不计作必要性；
  - deterministic fingerprint/certificate。
- `src/orchestration/minimality_runtime.py`
  - Git source/destination map；
  - Git archive 隔离候选；
  - 真实静态/Node frontend 启动；
  - Chromium action contract；
  - DOM/ARIA frame；
  - attempt 级持久化与断点续跑。
- `src/orchestration/edit_dom_guard.py`
  - 原 seed baseline 保留；
  - 新增 per-sprint source baseline，repair 重用；
  - 非 forward repair 额外冻结该轮真实 failed-source frame；若源根本无法渲染，则显式退化为 target-only 最小性而不伪造 DOM 证据；
  - 多页 seed 复用同一个真实 Chromium context 逐 route 建立 v3 baseline，root key 带 route 前缀；每个目标 route 最多开放两个 surface，所有 protected route 的 semantic surfaces、ARIA 与 focusability 均保持冻结。
- `src/orchestration/phases.py`
  - build 前记录 source；build 后记录 destination；
  - evaluator 通过后运行证书；
  - `non_minimal` 变成明确 repair signal；
  - `invalid_contract/inconclusive` 作为评测基础设施问题，不伪造代码修复。
- `scripts/export_trajectory_dataset.py`
  - 新 policy 启用后，forward edit 必须有 certified edit certificate；
  - repair 必须有 certified repair certificate；
  - legacy run 不被追溯性破坏，但带显式 provenance。
- `scripts/calibrate_minimality_cases.py` / `run_minimality_calibration.sh`
  - append-only `status` rows；
  - attempt timeout、持久日志、断点续跑；
  - 不覆盖已有实验结果。
- `scripts/calibrate_minimal_path_cases.py` / `run_minimal_path_calibration.sh`
  - 从历史真实 source commit 建立隔离 workspace；
  - 启动真实前端与 Chromium，重采 v2 DOM anchor frame；
  - 按 controller 依赖顺序回放历史真实 patch，每个成功 patch 后重新启动真实页面并采集 semantic DOM validation checkpoint；
  - plan、state、ledger、decision、validation evidence 与 status row 全部持久化且可断点跳过。

### 自动测试

完整 suite：`547 passed, 2 skipped`。两个 skip 是既有条件性测试；另有两条第三方 `aiohttp` deprecation warning。测试覆盖：

- exact patches 的顺序与唯一匹配；
- 冗余 atom 检出；
- target/frame 两种必要性；
- source 已通过时拒绝；
- infra error 不冒充必要性；
- build source map 恢复；
- action contract 必须含断言；
- 静态多 HTML 页、React Router 和 Next layout/global CSS 的 route ownership；
- 真实 Chromium 跨 route action contract 与逐 route semantic-DOM collateral regression。

多页新增验证是确定性真实浏览器 integration fixture，不是 mock browser；它验证
机制而不是声称已有多页训练语料校准。现有两条历史真实 source-commit calibration
仍是单页三文件样本，因此不能被表述成多页 corpus 证据。带参数的 filesystem route
在没有具体 URL/所有权映射时保持关闭，避免把 `/items/:id` 错当可访问页面建立假基线。
- DOM action selector → source hotspot/change cone；
- OpenAI 与 Claude SDK 双运行时 mutation policy；
- whole-file overwrite、越界路径、过宽 patch 和 Bash 旁路拒绝；
- dependency-edge widening ledger；
- exporter policy/certificate gate；
- legacy action-less artifact 的兼容读取。

## 五、真实小规模 case 数据

校准使用既有真实 Qwen 生成轨迹和真实 Chromium；没有 mock LLM、没有 mock 数据，也没有新调用 LLM。结果写入：

`runs/agentic/minimality_calibration/20260811_v1/records.jsonl`

| Case | 类型 | 结果 | 证据 |
|---|---|---|---|
| `air_truthchecked_back_to_top_v1_20260807__round_1_edit` | edit | `certified`, 3/3 atoms 必要 | HTML 按钮、CSS 可见/点击状态、JS scroll/click 行为任删一个均失败 |
| `edit_3662_store_tools_v4_20260807__round_2_repair` | repair | `certified`, 1/1 atom 必要 | 删除窄屏 `flex-direction: column` 修复后，移动端 action contract 失败 |
| `edit_3662_store_tools_v4_20260807__round_2_edit` | edit | `non_minimal`, 7→6 | `p006` 仅为 clear-search button 样式；删除后全部目标/保护契约仍通过，因此阻止导出 |

这组结果说明门禁既不是“全部拒绝”，也不是“全部放过”；它能接受必要的跨 HTML/CSS/JS edit、接受真实单点 repair，并发现旧 evaluator 未发现的冗余修改。

渐进式在线引导另有两条真实 source-commit 校准，写入
`runs/agentic/minimal_path_calibration/20260813_v3/records.jsonl`：

| Case | 渐进路径 | 真实检查点 | 最终反事实结论 |
|---|---|---|---|
| `air_truthchecked_back_to_top...9431392_4837d9a` | `index.html` 单入口；验证后沿真实引用边依次开放 `main.js`、`styles.css` | 3/3 patch 获准且实际成功，3/3 patch 后 Chromium semantic-DOM 验证成功 | `certified` |
| `edit_3662_store_tools...933ff09_b5f1242` | source 尚无目标 selectors，退化到 `index.html` 单入口；再沿 `<script>/<link>` 边开放 JS/CSS | 7/7 patch 获准且实际成功，7/7 patch 后 Chromium semantic-DOM 验证成功 | `non_minimal`，`p006` 为锥内冗余 |

第二条不是失败，而是重要边界：在线 controller 能阻止路径扩散，却不能仅凭
source cone 判断同一路径内某条 CSS 是否必要；因此后置 counterfactual certificate
仍必须保留。引导与证明分别回答“先去哪里改”和“最终还能不能更少”。

## 六、是否已经符合期待

### 已符合

- 保护手段以 DOM/ARIA、可聚焦性、浏览器行为为主，不靠像素 mask；
- edit/repair 在模型运行前和写入时由 harness-owned change cone 引导，不靠 prompt 自律；
- 最终最小性再由反事实执行机械证明，在线引导与后置验收相互独立；
- 产物仍与 WebCompass-like `src_code + instruction + exact patches + destination` 兼容；
- accepted/rejected/inconclusive 都保留，适合后续数据审计；
- 一个旧的“看似合格 edit”被真实证据降级，证明策略有新增判别力。

### 仍未完成，不能夸大

- 只校准 3 条证书记录与 2 条在线引导 replay，不能推断 corpus 通过率或训练收益；
- v1 atom 是 exact diff hunk，不是 HTML/CSS/JS AST/依赖图；
- frame 目前是顶层 semantic surfaces；全局 CSS、storage、network、timer 等跨 surface 副作用还需专门契约；
- action contract 的覆盖强度仍由 planner 初始定义，需要 mutation calibration 量化“能杀死多少真实回归”；
- 一轮多 atoms 会多次启动 Chromium，虽然已 fail-fast 和断点续跑，规模化前仍需两阶段 cheap→expensive 筛选。

因此当前结论应写为：**机制验收通过、规模验收待做、模型效果未验证。**

## 七、下一阶段优先级

1. **Mutation-calibrated contracts**：对 protected/target surface 注入 8–12 类受控 mutants，要求契约杀死率达到用户确认的阈值；不把 mutants 混入自然 repair。
2. **AST + dependency atoms**：HTML node、CSS rule、JS statement/handler 分层 HDD；将 selector/handler/element 依赖成组，减少“语法坏了所以必要”的廉价证据。
3. **Accepted action-tape bank**：每个已验收 sprint 保存 3–8 条高价值路径，后续 edit 只重放受 impact cone 影响的路径。
4. **State/storage/network frame**：把 localStorage key、route、network request signature、console/runtime error 纳入非像素保护。
5. **分层成本控制**：静态 replay、语法/依赖、DOM contract、最后才 Chromium；缓存相同 subset 的 oracle 结果。
6. **50–100 条真实 pilot**：按 task type、seed 框架、patch atoms 分层统计 certified/non-minimal/invalid/inconclusive，用户确认阈值后再扩到 300+。

## 八、数据与安全说明

- 本次没有使用或保存用户提供的 Kimi key；仓库、命令和日志中均不应出现该 key。
- 真实校准复用了已有 Qwen 轨迹，新增判断全部来自本地真实 Chromium。
- `records.jsonl` 逐条 append，包含 `status`；attempt 和日志持久化，可断点续跑。
- accepted、rejected 与 infrastructure/inconclusive 必须分开统计，不能用代码单测替代模型/数据质量验收。
