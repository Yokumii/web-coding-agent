---
name: webcompass-drag-drop
description: Reuse the Drag & Drop Interface reference implementation for the current structured Drag & Drop Interface subtask. Adapt the existing project's data, DOM and lifecycle without rewriting the component.
---

## 参考代码

- [references/drag-drop.css](references/drag-drop.css)
- [references/drag-drop.js](references/drag-drop.js)

直接复制并引用参考文件，不让模型重写一份实现。沿用当前项目的目录、模块和样式规范；代码不依赖 Skill 目录、Harness 或外部包。组件正确性由开发侧测试维护，当前 Edit 仍使用既有 Harness 检查。

## 接入位置

已有可移动项目、排序列表和合法分组；项目必须是对应列表的直接子元素，且具有全局唯一 ID。

只改当前指令允许的区域，不造独立 demo，不以类别名为由添加无关功能。若现有事件已承担同一业务，应替换相应旧处理，避免重复更新。

## 最小适配

将 `references/drag-drop.js` 与同目录 CSS（如有）直接复制到当前项目的组件目录。普通 HTML 使用现有 script/link 加载方式；ES Module 项目只给入口函数增加 export 并按现有方式 import。在宿主区域挂载后调用，视图卸载时调用 destroy；样式通过组件作用域内的颜色、字体、间距覆盖适配。不要重写状态或核心算法。

入口：`bindDragDrop({ ... })`。

参数：`lists: [{id, element}], itemSelector, getId, handleSelector?, onChange, onInvalidDrop?`。

有嵌套拖动按钮时传入 `handleSelector`，核心会接管并在销毁时恢复 draggable。需要无效投放提示时通过 `onInvalidDrop(item)` 更新宿主状态提示，不重写拖放监听。列表标题放在项目列表容器外；列表本身仅含可移动项目。空列表必须有可见投放高度，长面板应在自身内部滚动，避免被现有固定工具栏遮挡。

传入真实列表元素与项目 selector，直接移动已有 DOM。onChange 收到各列表的有序 ID，映射回现有数据；只有指令要求持久化时才在该回调保存，初始化由宿主恢复顺序。

保留已有项目 DOM、ID 和关联事件；不要复制一批新卡片替换现有数据。onChange 更新顺序时继续使用原项目的真实 metadata/progress，统计只选择项目节点。

包含真实 HTML 拖放、与原卡片等高的插入占位、目标高亮、Alt+上下方向键排序及 Alt+左右方向键跨列表移动；不是触屏拖动组件。嵌套列表需先映射到直接父容器，不应把子项目移出宿主包装。destroy() 恢复 draggable/tabindex。

宿主适配示例（变量映射到当前源码已有对象）：

```javascript
const component = bindDragDrop({
  lists: existingColumns.map(column => ({id: column.id, element: column.element})),
  itemSelector: "[data-item-id]",
  getId: item => item.dataset.itemId,
  onChange: order => updateExistingBoardOrder(order)
});
```

销毁该区域时调用返回对象的 `destroy()`。需要持久化、接口模拟或额外操作时，只接入当前指令明确要求的部分；额外需求未实现时不能把整条 Edit 标成完成。

## 技术栈选择与直接复用

- 原生 HTML/CSS/JS：保留已有 script/link，直接复用本目录经典 JS 与 CSS。
- React / TypeScript / Vite：[Component.jsx](references/Component.jsx) + [drag-drop.mjs](references/drag-drop.mjs) + 同目录 CSS；在现有组件中引入，不改构建配置。TypeScript 项目沿用已有 JS 互操作规范。
- Vue 3 / Vite：[Component.vue](references/Component.vue) + 同一 ESM 核心及 CSS；沿用现有 SFC、路由和服务。

三个入口共享同一功能核心；React/Vue 入口负责生命周期和销毁。`configure(root)` 返回上文入口参数，普通 mount 组件使用 `{container: root, ...现有数据与回调}`。用稳定的 configure（React useCallback），数据变化优先调用核心公开更新方法；只有确需重置状态时才更换 configure 或 key。不要在组件 render 中执行挂载。

绑定现有 DOM 的 bind 类、购物车按钮和向导步骤，在 configure 中传入已挂载的真实 refs；外部宿主节点始终由原框架拥有。**拖放会移动节点：只能绑定组件独占的列表 DOM；React/Vue 正在协调的列表必须改用宿主 state 更新排序，不能直接移动框架管理的节点。** 需要适配这类受控列表时只替换拖放的落点更新，不重写其他行为。

通过 Harness 的 `copy_from` 复制选定文件，响应中只写 source/path 与必要的 line_edits；不要把整份参考代码再生成一遍。保留引用文件的相对关系，最终产物不能依赖 `.harness` 或 Skill 路径。

## 最小修改约束

保留 source 的路由、数据身份、现有业务入口、非目标区域、共享状态和设计语言。仅在本 subtask 的目标 anchor 内增加必要 state/event/component，以及对应 import、样式和服务接线；已有同类 state 必须复用。禁止重建项目、替换技术栈、升级依赖、全文件覆盖、无关重构和添加未要求的功能。参考实现只覆盖核心逻辑，当前指令的额外要求仍须局部补齐。

## 轻量验收规则

[acceptance.json](references/acceptance.json) 定义本类因果流程。按真实源码填入 selector、输入和预期值，在修改前冻结为 Harness 唯一 browser_check。实现后复用 Playwright 执行相同检查并记录具体失败；通过才产生下一 accepted 状态，失败按既有流程修复再复测。

Drag one existing item to another position/list; assert new DOM order and destination, then exercise keyboard alternative. Reload and assert order only when persistence is required.

元素存在只是入口检查；必须验证操作造成的真实状态变化，并检查 console/page errors。仅无法机械验证的显式视觉要求保留局部评审，不重新评审整个页面。

## 宿主接入与持续改进

先从现有源码定位真实入口、数据源、目标 DOM 容器和事件；保留宿主容器结构与已有状态，不以字段名、DOM 层级或 HTML 序列化字符串做硬编码验收。挂载时传入现有 refs/state 与回调，隐藏状态沿用宿主的可见性/路由机制；组件销毁时调用 `destroy()` 并移除监听，避免全局快捷键冲突、重复挂载和 stale 状态。示例：

```javascript
const instance = mount({ container: existingRegion, initialValue: existingState.value, onChange: value => updateExistingState(value) });
view.addEventListener('beforeunload', () => instance.destroy(), { once: true });
```

验收采用一条最短因果路径，先建立前置状态，再执行核心操作并断言内容、状态和提交结果；必要时只补一个失败/恢复分支。失败反馈必须包含失败动作、预期/实际、源码位置和直接浏览器证据，并区分已确认根因与推测。Harness 会把可复用的宿主接入或参考实现问题写成待验证 Skill 改进；只有同一真实样本和受影响回归行为均通过后才启用新版本，运行中的任务继续使用原版本。不得写入样本专属答案或删除有效检查。
