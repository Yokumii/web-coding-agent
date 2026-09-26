---
name: webcompass-page-transitions
description: Reuse the Page Transitions reference implementation for the current structured Page Transitions subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/learned.json`
- `references/transitions.js`
- `references/transitions.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Page Transitions**：同父视图之间有可观察的退出/进入中间态，最终焦点、交互和 history 保持一致。

公开 Edit 典型：Brands grid to detail: home fades/shrinks, detail slides in; Back and browser history reverse it.

覆盖：transition + route history；forward/back + focus restore；转场中禁用旧视图交互。已有路由只调用 `go`。

## 接入位置

现有 view 元素、导航、路由/history。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/page-transitions/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`bindPageTransitions(...)`。

```javascript
const transition = bindPageTransitions({views:{home, detail}, initial:'home', duration:280, historyKey:'brands'}); links.forEach(a => a.onclick = e => {e.preventDefault(); transition.go(a.dataset.view)});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

不要 display:none 瞬切；不可交互状态要覆盖旧视图；historyKey 只能有一套；视图必须是兄弟节点。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
