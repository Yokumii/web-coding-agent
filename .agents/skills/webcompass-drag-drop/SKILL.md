---
name: webcompass-drag-drop
description: Reuse the Drag & Drop Interface reference implementation for the current structured Drag & Drop Interface subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/drag-drop.css`
- `references/drag-drop.js`
- `references/drag-drop.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Drag & Drop Interface**：真实拖放手势改变排序/分组，显示占位和合法目标反馈，并按要求持久化。

公开 Edit 典型：Brand Portfolio Manager: reorder cards, cross-list moves, highlighted drop zone, refresh persistence.

覆盖：reorder + cross-list move；drag + persisted order；键盘 Alt+方向键作为可访问替代。`onChange` 后写回宿主。

## 接入位置

已有列表容器、卡片 DOM、真实 metadata/order store。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/drag-drop/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`bindDragDrop(...)`。

```javascript
const dnd = bindDragDrop({lists: columns.map(c => ({id:c.id, element:c.el})), itemSelector:'[data-item-id]', getId: el => el.dataset.itemId, onChange: order => boardStore.persistOrder(order)});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

不能只调用最终回调；项目必须是直接子节点；drop 后顺序和父列表都要写回；不要覆盖卡片原事件。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
