---
name: webcompass-rich-text-editor
description: Reuse the Rich Text Editor reference implementation for the current structured Rich Text Editor subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/editor.css`
- `references/editor.js`
- `references/editor.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Rich Text Editor**：真实选区/块级语义格式、链接/图片插入、粘贴清理和可保存 HTML 同步。

公开 Edit 典型：Content Composer: sticky toolbar, bold/italic/underline/strike, H1-H3, lists, quote, link dialog, image URL preview and hidden input.

覆盖：editor + form save；selection formatting + link/image insertion；preview + restore draft。`onChange` 写回宿主。

## 接入位置

现有正文 HTML、form hidden input、保存/预览入口。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/rich-text-editor/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountRichTextEditor(...)`。

```javascript
const editor = mountRichTextEditor({container: composer, initialHTML: doc.body, output: bodyInput, onChange: html => store.updateDraft({body: html})}); save.onclick = () => api.save({ ...store.current(), body: editor.getHTML() });
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

按钮 active 不等于正文改变；操作不能丢选区；保存从宿主上下文读取元数据；只允许安全 URL/HTML。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
