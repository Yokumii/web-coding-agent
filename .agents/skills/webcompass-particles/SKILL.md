---
name: webcompass-particles
description: Reuse the Particle Effects reference implementation for the current structured Particle Effects subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/particles.js`
- `references/particles.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Particle Effects**：粒子随时间漂浮并按邻近、指针和点击改变，离屏暂停且不遮挡业务内容。

公开 Edit 典型：Hero blue/white network: drift, mouse repel, click burst, neighbor links, pause offscreen.

覆盖：particles + hero overlay；pointer interaction + visibility pause；reduced motion 降级。

## 接入位置

Hero 容器尺寸、主色、前景交互层。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/particles/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountParticles(...)`。

```javascript
const particles = mountParticles({container: hero, color: getComputedStyle(hero).getPropertyValue('--accent'), count: 55, linkDistance: 96, interaction: true});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

Canvas 不能承载业务信息或拦截点击；count 与 linkDistance 需受控；不要用固定 DOM 点阵冒充动态系统。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
