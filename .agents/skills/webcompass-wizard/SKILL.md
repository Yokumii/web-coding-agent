---
name: webcompass-wizard
description: Reuse the Multi-step Wizard reference implementation for the current structured Multi-step Wizard subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/wizard.js`
- `references/wizard.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Multi-step Wizard**：分步表单验证前进条件，返回保留输入，最终摘要读取同一 form 状态并提交。

公开 Edit 典型：Freelance Talent Application: personal, experience/portfolio, legal, validation and final summary.

覆盖：wizard + async validation；back + persistent fields；final summary + submit。组件接管 form submit。

## 接入位置

同一 form 的已有 step 元素、字段、提交服务。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/wizard/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountWizard(...)`。

```javascript
const wizard = mountWizard({form, steps:[...document.querySelectorAll('[data-step]')].map((element,i)=>({element,label:labels[i]})), onComplete:data=>applicationApi.submit(data)});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

Next 必须验证当前步；Back 不能重建丢值；同名多值保留数组；摘要不能读取过时副本；避免重复 submit listener。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
