---
name: webcompass-parallax
description: Reuse the Parallax Scrolling reference implementation for the current structured Parallax Scrolling subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/parallax.js`
- `references/parallax.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Parallax Scrolling**：滚动驱动至少两层的可测不同位移，叠加进入视口淡入时仍不破坏宿主 transform。

公开 Edit 典型：Our History: slow abstract background, medium timeline, fast text/images with reveal.

覆盖：parallax + reveal；多层不同 speed + reduced motion；导航离开时 destroy。

## 接入位置

同一区域真实背景/中景/前景层。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/parallax/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`bindParallax(...)`。

```javascript
const parallax = bindParallax({section: history, layers:[{element:bg,speed:.15},{element:timeline,speed:.4},{element:copy,speed:.8}], maxOffset:180});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

不能给所有层同一位移；不要覆盖已有 transform；只在当前 section 写样式；页面滚动与容器滚动要明确。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
