---
name: webcompass-file-upload
description: Reuse the File Upload with Progress reference implementation for the current structured File Upload with Progress subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/upload.css`
- `references/upload.js`
- `references/upload.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**File Upload with Progress**：选择/拖入文件后独立排队、校验、进度、完成/失败/取消/重试，并把结果交给宿主。

公开 Edit 典型：Quick Apply: PDF/DOCX drop zone, per-file progress, queue, cancel/remove and retry.

覆盖：upload + progress + cancel；preview + remove；多文件 queue + concurrency。`onUpdate` 只更新宿主显示。

## 接入位置

已有 input/drop 区、上传端点、类型/大小策略、结果列表。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/file-upload/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountFileUpload(...)`。

```javascript
const uploads = mountFileUpload({container, upload: createXHRUpload('/api/files'), accept: '.pdf,.docx', maxBytes: 10_000_000, concurrency: 2, preview: true, removable: true, onUpdate: job => store.uploads.replace(job.file.name, job)});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

前端完成不等于服务器收到；每个文件必须有独立 AbortSignal；取消要停止传输并释放预览 URL；不要按文件名造失败。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
