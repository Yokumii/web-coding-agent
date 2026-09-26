---
name: webcompass-infinite-scroll
description: Reuse the Infinite Scroll reference implementation for the current structured Infinite Scroll subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/infinite-scroll.js`
- `references/infinite-scroll.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Infinite Scroll**：接近列表末端才请求下一页，追加去重、串行 pending、结束后停止，并可恢复已加载状态。

公开 Edit 典型：Company News: 5 initial cards, append batches near footer, spinner, end at 30, return restores list/scroll.

覆盖：infinite scroll + skeleton loading；append + deduplicate；restore cache + scroll position。已有首批必须从下一页开始。

## 接入位置

列表容器、真实分页 API、稳定 ID、宿主缓存。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/infinite-scroll/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountInfiniteScroll(...)`。

```javascript
const feed = mountInfiniteScroll({container:list, root:scrollPane, loadPage:(page,o)=>newsApi.page(page,o), getId:n=>String(n.id), renderLoading:renderNewsSkeleton, renderItem:renderNewsCard, initialLoad:true});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

不要预载全部数据；没有新记录不能宣称 hasMore；快速滚动只能有一个请求；恢复时同时恢复 items 和 scroll。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
