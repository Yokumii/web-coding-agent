---
name: webcompass-realtime-dashboard
description: Reuse the Real-time Dashboard reference implementation for the current structured Real-time Dashboard subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/dashboard.css`
- `references/dashboard.js`
- `references/dashboard.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Real-time Dashboard**：同一采样同时更新指标、趋势图、时间戳和连接状态，失败保留上次有效值。

公开 Edit 典型：Live Impact Metrics: CO2 and energy values with synced sparklines and last-updated time.

覆盖：dashboard + polling/event stream；metric cards + sparkline + status；失败重试由宿主策略决定。

## 接入位置

指标卡 DOM、真实/模拟数据源、状态栏。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/realtime-dashboard/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountRealtimeDashboard(...)`。

```javascript
const dashboard = mountRealtimeDashboard({container: metrics, metrics:[{key:'co2',label:'CO2 Saved',format:formatNumber}], fetchSample:o=>metricsApi.latest(o), intervalMs:3000, historyLimit:30});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

不能只闪烁 Live；数字和图表必须来自同一样本；状态行已有按钮不能被 textContent 覆盖；禁止并发轮询。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
