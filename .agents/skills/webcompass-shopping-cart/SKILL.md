---
name: webcompass-shopping-cart
description: Reuse the tested Shopping Cart reference component inside an already running project. Adapt existing product data, buttons, mount location and styles without rewriting the cart implementation.
---

## 参考代码

- [references/cart.js](references/cart.js)：完整购物车组件，提供 `mountShoppingCart(options)`。已实现状态管理、加购合并、数量编辑、行小计、总额、非法输入恢复、DOM 更新和卸载。
- [references/cart.css](references/cart.css)：仅作用于 `.wc-cart__*` 的组件样式，继承当前页面字体和颜色。

直接复制并引用这些文件，不让模型重新输出一份实现。当前项目已经能运行，沿用它的目录、构建方式和模块规范。最终代码只依赖项目内文件。

组件核心只覆盖加购、数量和金额；不擅自添加删除、结算或持久化。参考代码的正确性由开发侧状态测试和真实浏览器测试维护；当前 Edit 的既有 Harness 检查照常执行。

## 找到当前项目的接入位置

定位真实商品数据、商品列表/卡片、现有加购按钮，以及指令要求的购物车区域。使用稳定商品 ID、完整名称和真实价格；不要另造数据，也不要误读截断的商品标题。

优先使用已有数据对象和交互入口。已有按钮若绑定同一业务的旧监听器，应替换旧处理，确保一次点击只有一个加购入口；preventDefault 不会移除旧 JavaScript 监听器。挂载点只占用当前指令允许修改的区域。若已有购物车，不叠加第二套状态：只有指令需要替换该区域时才导入已有数量并接入；否则局部复用其需要的参考逻辑。

## 最小适配方法

1. 把参考 JS/CSS 放入当前项目适合的组件目录，按现有方式引用。普通 HTML 可使用 script/link；模块项目只调整导出方式和生命周期接线，不重写核心逻辑，不重建项目环境。
2. 将现有商品映射成 `{id, label, unitPriceMinor}`，金额转为整数最小货币单位。只写数据映射，不再实现加购、数量计算或渲染逻辑。
3. 在目标区域提供一个空挂载节点，传入现有列表和按钮的对应关系：

```javascript
const basket = mountShoppingCart({
  items,                 // [{id: string, label: string, unitPriceMinor: integer}]
  container,             // 目标区域内的空 HTMLElement
  addRoot,               // 现有商品列表 HTMLElement
  addSelector,           // 列表内现有加购按钮 selector
  getItemId,             // button => 对应商品的真实 ID
  formatMoney,           // 整数最小货币单位 => 当前页面的金额文本
  initialQuantities,     // 可选：已有 [{id, quantity}]，默认 []
  classes,               // 可选：组件部位对应的现有样式类，默认 {}
  labels                 // 可选：heading / quantity / total / empty 文案
});
```

4. `classes` 可映射 `root`、`heading`、`lines`、`line`、`item`、`quantity-label`、`quantity`、`subtotal`、`summary`、`total`、`empty`。优先通过这些参数适配页面风格。卸载或切换该视图时调用 `basket.destroy()`，避免重复事件绑定。

公开方法：`add(id)`、`setQuantity(id, quantity)`、`snapshot()`、`destroy()`。`snapshot()` 返回数据副本，数量只能通过公开方法更新。重复挂载、未知商品和非法数量会报错。

仅当当前 Edit 确有不同的必要行为时，修改对应的最小代码段；不要把参考组件重新实现一遍。引用文件不代表适配已完成，仍须接入真实数据、实际按钮及页面入口。

## 技术栈选择与直接复用

- 原生 HTML/CSS/JS：保留已有 script/link，直接复用本目录经典 JS 与 CSS。
- React / TypeScript / Vite：[Component.jsx](references/Component.jsx) + [cart.mjs](references/cart.mjs) + 同目录 CSS；在现有组件中引入，不改构建配置。TypeScript 项目沿用已有 JS 互操作规范。
- Vue 3 / Vite：[Component.vue](references/Component.vue) + 同一 ESM 核心及 CSS；沿用现有 SFC、路由和服务。

三个入口共享同一功能核心；React/Vue 入口负责生命周期和销毁。`configure(root)` 返回上文入口参数，普通 mount 组件使用 `{container: root, ...现有数据与回调}`。用稳定的 configure（React useCallback），数据变化优先调用核心公开更新方法；只有确需重置状态时才更换 configure 或 key。不要在组件 render 中执行挂载。

绑定现有 DOM 的 bind 类、购物车按钮和向导步骤，在 configure 中传入已挂载的真实 refs；外部宿主节点始终由原框架拥有。**拖放会移动节点：只能绑定组件独占的列表 DOM；React/Vue 正在协调的列表必须改用宿主 state 更新排序，不能直接移动框架管理的节点。** 需要适配这类受控列表时只替换拖放的落点更新，不重写其他行为。

通过 Harness 的 `copy_from` 复制选定文件，响应中只写 source/path 与必要的 line_edits；不要把整份参考代码再生成一遍。保留引用文件的相对关系，最终产物不能依赖 `.harness` 或 Skill 路径。

## 最小修改约束

保留 source 的路由、数据身份、现有业务入口、非目标区域、共享状态和设计语言。仅在本 subtask 的目标 anchor 内增加必要 state/event/component，以及对应 import、样式和服务接线；已有同类 state 必须复用。禁止重建项目、替换技术栈、升级依赖、全文件覆盖、无关重构和添加未要求的功能。参考实现只覆盖核心逻辑，当前指令的额外要求仍须局部补齐。

## 轻量验收规则

[acceptance.json](references/acceptance.json) 定义本类因果流程。按真实源码填入 selector、输入和预期值，在修改前冻结为 Harness 唯一 browser_check。实现后复用 Playwright 执行相同检查并记录具体失败；通过才产生下一 accepted 状态，失败按既有流程修复再复测。

Add an existing product, assert its identity and initial total; add again or change quantity, assert the exact recalculated row subtotal and aggregate. Test remove/persistence only when requested.

元素存在只是入口检查；必须验证操作造成的真实状态变化，并检查 console/page errors。仅无法机械验证的显式视觉要求保留局部评审，不重新评审整个页面。

## 宿主接入与持续改进

先从现有源码定位真实入口、数据源、目标 DOM 容器和事件；保留宿主容器结构与已有状态，不以字段名、DOM 层级或 HTML 序列化字符串做硬编码验收。挂载时传入现有 refs/state 与回调，隐藏状态沿用宿主的可见性/路由机制；组件销毁时调用 `destroy()` 并移除监听，避免全局快捷键冲突、重复挂载和 stale 状态。示例：

```javascript
const instance = mount({ container: existingRegion, initialValue: existingState.value, onChange: value => updateExistingState(value) });
view.addEventListener('beforeunload', () => instance.destroy(), { once: true });
```

验收采用一条最短因果路径，先建立前置状态，再执行核心操作并断言内容、状态和提交结果；必要时只补一个失败/恢复分支。失败反馈必须包含失败动作、预期/实际、源码位置和直接浏览器证据，并区分已确认根因与推测。Harness 会把可复用的宿主接入或参考实现问题写成待验证 Skill 改进；只有同一真实样本和受影响回归行为均通过后才启用新版本，运行中的任务继续使用原版本。不得写入样本专属答案或删除有效检查。
