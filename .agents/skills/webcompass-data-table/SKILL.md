---
name: webcompass-data-table
description: Reuse the Data Table reference implementation for the current structured Data Table subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/learned.json`
- `references/table.css`
- `references/table.js`
- `references/table.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Data Table**：真实同构行集合上的筛选、表头排序、分页、选择/批量和窄屏等价展示。

公开 Edit 典型：Portfolio Performance table: company/sector/year/status, name filter, sortable headers, client pagination and mobile cards.

覆盖：filter + sort + pagination；selection + bulk action；edit + pagination。所有操作共用 `rows`，回调写回后 `setRows`。

## 接入位置

已有 rows、稳定 ID、列定义、宿主 store。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/data-table/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountDataTable(...)`。

```javascript
const table = mountDataTable({container, rows: store.records, columns, getId: r => String(r.id), pageSize: 10, selectable: true, bulkActions: [{id:'archive',label:'Archive'}], onEdit: change => store.update(change.id, change.key, change.value).then(() => table.setRows(store.records)), onBulkAction: (id, ids) => store.bulk(id, ids)});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

排序必须使用真实值而非格式化文本；过滤后重算页数；批量 ID 只来自当前选择；编辑后保留稳定 ID。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
