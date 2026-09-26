---
name: webcompass-skeleton-loading
description: Reuse the Skeleton Loading reference implementation for the current structured Skeleton Loading subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/skeleton.css`
- `references/skeleton.js`
- `references/skeleton.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Skeleton Loading**：占位结构与最终内容对应，加载期间保持布局，完成后有序替换并支持失败重试。

公开 Edit 典型：News slider: title/date/image skeleton with shimmer, then fade to real content.

覆盖：skeleton + async load；retry + error；skeleton + infinite scroll 组合时每页复用同一结构。

## 接入位置

空内容挂载区、最终布局 renderer、真实 load service。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/skeleton-loading/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountSkeletonLoading(...)`。

```javascript
const loading = mountSkeletonLoading({container:news, skeleton:newsSkeleton.content.firstElementChild.cloneNode(true), load:o=>newsApi.latest(o), renderContent:data=>renderNews(data)});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

spinner/灰块不等于骨架；占位尺寸要匹配最终 DOM；完成后不能残留占位占空间；不能内置固定假数据。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
