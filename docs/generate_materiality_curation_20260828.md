# Generate 显著差异筛选与三类数据样例（2026-08-28）

## 要解决的问题

Edit 历史中的每个 accepted checkpoint 都有完整代码，但这不代表每个 checkpoint
都值得再导出为一条 Generate。连续 checkpoint 很容易只差一个小控件；如果全部作为
Generate，会制造大量近重复的“从零需求 → 完整网站”样本，还会把终态 checkpoint 与
`complete_generate` 重复两次。

本次采用两层处理：严格 trajectory exporter 先负责真实性、浏览器证据、patch 回放与
谱系；随后 `scripts/curate_generate_materiality.py` 只做 Generate 物化筛选，不改写原始
记录，也不重新解释失败案例。

## 筛选规则

1. 保留从零轨迹的第一个 accepted Generate，作为初始产品状态。
2. 中间 checkpoint 默认不导出。只有 human 或 semantic reviewer 明确确认它同时相对
   初始 Generate、上一条已选 Generate 都有显著产品差异，并给出非空证据，才可保留。
3. 完整轨迹保留一个 `complete_generate`。若 terminal checkpoint 与它指向同一个
   destination commit，删除 checkpoint 视图，避免逐字节重复。
4. 如果终态没有通过相对初始 Generate 的显著性审查，只保留 `complete_generate`，不再
   同时保留初始 Generate，避免产出一对未经证明有足够差异的近重复样本。
5. 代码行数、文件数和相似度只能作为审查证据，不能自动代替语义判断。规则/阈值不会
   自行把 checkpoint 分类为“显著”。

## 本次真实样例

来源是严格运行
`runs/agentic/generate_edit_repair_v6_recovery_v12_20260821/`。原始 exporter 产出
5 条记录：1 Edit、1 Repair、2 checkpoint Generate、1 complete Generate。

筛选后的审阅 release 位于：

`logs/edit_first_20260828/curated_generate_edit_repair_materiality_v1_20260828/release_v3/`

| 家族 | 数量 | 内容 | 证据边界 |
| --- | ---: | --- | --- |
| Generate | 2 | Sprint 1 初始 Dashboard；Sprint 2 完整 Dashboard + Library | 终态相对初始新增独立 `/library.html`、`library.css`、`library.js`，并新增文本过滤与导入持久化能力 |
| Edit | 1 | accepted Sprint 1 → accepted Sprint 2，增加 Library Catalog & Import | 三个 `create_file` patch 可精确回放；Edit minimality 为 `certified` |
| Repair | 1 | Sprint 2 真实失败 round 5 → accepted round 9 | 缺失/错误 import 与 filter 行为由浏览器复现；只修 `library.js`；Repair minimality 为 `certified` |

被删除的是
`generate_edit_repair_v6_recovery_v12_20260821__checkpoint_generate_s02`，因为它与
`complete_generate` 的 destination commit 都是
`5fa9055df2fa303afc30a761b9ec56624c0b9f5f`。这不是“低质量失败”，而是明确的
Generate 去重。

初始与终态的确定性差异复核为：4 个文件 → 7 个文件，新增三个 route-local 文件；用
native `create_file` patch 重放后与终态逐文件一致。Edit 与 Repair 的 patch 也都从各自
source 精确重放到 destination。

## 与 2026-08-28 最新矩阵的关系

当天最新六例矩阵主要是已有项目上的 Edit admission 验证，不是从零 Generate 轨迹：

- `two-route-shared-state` 是通过的真实 GT Edit 候选，但矩阵没有生成正式 accepted tape
  与 counterfactual certificate，因此暂时只作为验证证据，不混入本次严格数据 release。
- `multi-html-log-scoped-css` 使用明确标注的 controlled CSS overlay，只证明多 HTML 与
  selector-aware guard；不能冒充自然 Edit 或自然 Repair。
- `webcompass_pagination_20260828T120113` 有真实失败与恢复，可作为 natural Repair
  校准证据，但该轻量验证脚本没有物化正式 minimality certificate，所以没有越过严格
  exporter 的准入边界。
- 其余 rejected case 不导出为成功数据。

因此本次“看看数据”优先展示同仓库内已有严格谱系的四条正式候选，同时保留最新矩阵
作为门禁校准证据。两类结果没有混合统计。

## 文件说明

- `strict_records.jsonl`：严格 exporter 的原始 5 条记录，保持不变。
- `generate_materiality_selection.json`：显著性语义审查与结构差异证据。
- `release_v3/records.jsonl`：筛选后的四条完整记录。
- `release_v3/generate.jsonl`：两条 Generate。
- `release_v3/edit.jsonl`：一条 canonical Edit。
- `release_v3/repair.jsonl`：一条 natural Repair。
- `release_v3/manifest.json`：输入 SHA-256、保留/删除 ID、原因与数量。
- `verification_report.json`：Generate 差异、三类数量、patch 回放和证书状态。

本次筛选与回放均为本地确定性处理，没有调用 LLM/vision API，新增 API 成本为 0。
