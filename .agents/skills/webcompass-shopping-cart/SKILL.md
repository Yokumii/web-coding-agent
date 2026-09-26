---
name: webcompass-shopping-cart
description: Reuse the tested Shopping Cart reference component inside an already running project. Adapt existing product data, buttons, mount location and styles without rewriting the cart implementation.
---

## 参考代码

- `references/Component.jsx`
- `references/Component.vue`
- `references/cart.css`
- `references/cart.js`
- `references/cart.mjs`
- `references/host-integration.js`：宿主写回和组合接法，可直接复制后按项目模块规范改 import。

## Benchmark 对齐与能力覆盖

**Shopping Cart**：加入、合并数量、编辑/删除、汇总和按要求持久化必须来自同一购物车状态。

公开 Edit 典型：Document Request Basket: add PDFs, sidebar lines/thumbnails, remove, total size, preserve across Contact navigation.

覆盖：add + quantity + remove + total；cart + local persistence；跨页恢复。公开方法操作同一状态。

## 接入位置

真实商品/文档数据、加购按钮、basket 区域、宿主持久化。先定位现有数据、DOM 和生命周期，再把 reference 复制到 `frontend/components/edit-skills/shopping-cart/`；挂载点按 API 要求保持为空，已有节点和事件不重建。

## API / 数据流

入口：`mountShoppingCart(...)`。

```javascript
const basket = mountShoppingCart({items: docs.map(d=>({id:d.id,label:d.title,unitPriceMinor:d.sizeBytes})), container:basketRegion, addRoot:docList, addSelector:'[data-add]', getItemId:b=>b.dataset.add, formatMoney:bytes=>formatBytes(bytes), initialQuantities:store.loadBasket()});
```

输入由宿主提供；组件只维护必要 UI 状态。所有编辑、删除、批量、排序、选择、认证或上传结果都通过回调写回宿主，再由宿主把最新数据传回 `setRows` / `push` / `update` 等公开入口。调用 `destroy()` 解除监听、请求、定时器和动画。

## 特有风险

金额/大小必须用真实整数值；徽标、列表、汇总不能各算一份；不要只存数量；未知 ID 与非法数量应报错。

## 最小正确示例

上面的接入代码已经体现真实数据、关键回调和生命周期。组合任务优先沿用 `references/host-integration.js`，只改配置、数据映射和宿主回调；不要重新实现核心状态机。
