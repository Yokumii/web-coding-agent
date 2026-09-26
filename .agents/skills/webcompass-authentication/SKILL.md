---
name: webcompass-authentication
description: Reuse the User Authentication reference implementation for the current structured User Authentication subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/authentication.css`
- `references/authentication.js`
- `references/authentication.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**User Authentication**：登录/注册/退出改变会话，并同步 header、受限入口和恢复状态。

公开 Edit 典型：Login/Register modal, simulated or real sign-in, session restore, username and Logout.

覆盖：authentication + async validation；把字段校验绑定在同一 form，onSession 只写宿主 session。

## 接入位置

账号区域、auth service、导航/受限内容。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/authentication/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountAuthentication(...)`。

```javascript
const auth = mountAuthentication({container: accountRegion, authenticate: (credentials, o) => authApi.signIn(credentials, o), restoreSession: o => authApi.currentUser(o), signOut: o => authApi.signOut(o), onSession: user => appStore.setSession(user), allowRegistration: true});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

不能仅改按钮文字；退出必须清理宿主会话；localStorage 只能作为明确模拟，不冒充安全认证。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
