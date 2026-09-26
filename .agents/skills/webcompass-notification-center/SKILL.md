---
name: webcompass-notification-center
description: Reuse the Notification Center reference implementation for the current structured Notification Center subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/notifications.css`
- `references/notifications.js`
- `references/notifications.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Notification Center**：可管理通知集合：未读计数、单条/全部已读、删除、分类/日期分组和新事件到达。

公开 Edit 典型：Executive Dashboard bell with category/severity/date groups, Mark all read and periodic new events.

覆盖：notification + realtime event；category/date grouping + mark all read；action + delete。push 进真实事件分支，onChange 写回宿主。

## 接入位置

事件总线、通知入口、HUD badge、业务 action。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/notification-center/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountNotificationCenter(...)`。

```javascript
const center = mountNotificationCenter({container: panel, notifications: store.notifications, allowDelete: true, onAction: n => router.open(n.target), onChange: (entries, unread) => store.saveNotifications(entries, unread)}); eventBus.on('notification', n => center.push(n));
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

badge 必须从同一 entries 计算；事件重试保持 ID；打开面板不能凭空派发通知；hidden 样式不能被布局规则覆盖。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
