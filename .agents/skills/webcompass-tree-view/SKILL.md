---
name: webcompass-tree-view
description: Reuse the Tree View reference implementation for the current structured Tree View subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/tree.css`
- `references/tree.js`
- `references/tree.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Tree View**：真实父子树支持展开/折叠、级联选择/半选、搜索显露祖先路径和懒加载。

公开 Edit 典型：Brand Directory: Gaming > Console > PlayStationLifestyle, parent selection, indeterminate child and search.

覆盖：tree + lazy load；search + ancestor reveal；selection + detail pane。`onSelection` 写回叶子 ID。

## 接入位置

目录/分类树、稳定全局 ID、宿主选择与详情区。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/tree-view/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountTreeView(...)`。

```javascript
const tree = mountTreeView({container, nodes: store.tree, onSelection: ids => store.setSelectedLeafIds(ids), onNodeFocus: node => details.show(node), isDisabled: node => node.locked});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

搜索不能偷偷请求远端分支；父级半选要由真实叶子集合计算；懒加载 ID 必须全局唯一；禁用节点不可被选择。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
