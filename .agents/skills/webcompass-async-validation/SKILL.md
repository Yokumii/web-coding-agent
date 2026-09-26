---
name: webcompass-async-validation
description: Reuse the Async Form Validation reference implementation for the current structured Async Form Validation subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/validation.js`
- `references/validation.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Async Form Validation**：字段停输后异步检查可用性；pending、旧响应作废、同步约束与提交资格一致。

公开 Edit 典型：Company URL/domain availability check with debounce, success/error feedback, and submit gating.

覆盖：filter/validation + submit gating；多个字段分别绑定同一 form，canSubmit 连接宿主其它业务条件。

## 接入位置

输入字段、已有 form validity、真实校验服务。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/async-validation/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`bindAsyncValidation(...)`。

```javascript
const validation = bindAsyncValidation({form, input: urlInput, submitButton, check: (value, ctx) => api.checkDomain(value, ctx), debounceMs: 500, canSubmit: () => draft.isReady});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

不要把固定延迟成功当服务结果；旧请求不得覆盖新值；异步成功不能绕过其它 required 字段。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
