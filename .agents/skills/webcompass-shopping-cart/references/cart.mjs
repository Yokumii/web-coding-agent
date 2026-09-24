// Standalone component. Host glue supplies data and DOM bindings, not cart logic.
function createCart(items) {
  const catalog = new Map();
  const quantities = new Map();
  for (const item of items) {
    if (
      typeof item.id !== "string" ||
      !item.id ||
      catalog.has(item.id) ||
      typeof item.label !== "string" ||
      !item.label ||
      !Number.isSafeInteger(item.unitPriceMinor) ||
      item.unitPriceMinor < 0
    ) {
      throw new TypeError("Invalid or duplicate cart item");
    }
    catalog.set(
      item.id,
      Object.freeze({
        id: item.id,
        label: item.label,
        unitPriceMinor: item.unitPriceMinor,
      }),
    );
  }
  function setQuantity(id, quantity) {
    if (!catalog.has(id)) throw new RangeError("Unknown cart item");
    if (!Number.isSafeInteger(quantity) || quantity < 1) {
      throw new RangeError("Quantity must be a positive safe integer");
    }
    let total = 0;
    for (const [key, item] of catalog) {
      const count = key === id ? quantity : quantities.get(key) || 0;
      total += item.unitPriceMinor * count;
      if (!Number.isSafeInteger(total)) throw new RangeError("Cart total overflow");
    }
    quantities.set(id, quantity);
  }
  return Object.freeze({
    add(id) {
      setQuantity(id, (quantities.get(id) || 0) + 1);
    },
    setQuantity,
    lines() {
      return Array.from(quantities, ([id, quantity]) => ({
        ...catalog.get(id),
        quantity,
        lineTotalMinor: catalog.get(id).unitPriceMinor * quantity,
      }));
    },
    totalMinor() {
      let total = 0;
      for (const [id, quantity] of quantities) {
        total += catalog.get(id).unitPriceMinor * quantity;
      }
      return total;
    },
  });
}

const shoppingCartMounts = new WeakMap();

function mountShoppingCart({
  items,
  container,
  addRoot,
  addSelector,
  getItemId,
  formatMoney,
  initialQuantities = [],
  classes = {},
  labels = {},
}) {
  if (
    !(container instanceof HTMLElement) ||
    !(addRoot instanceof HTMLElement) ||
    typeof addSelector !== "string" ||
    !addSelector ||
    typeof getItemId !== "function" ||
    typeof formatMoney !== "function"
  ) {
    throw new TypeError("Cart requires host data, DOM bindings and a money formatter");
  }
  if (shoppingCartMounts.has(container)) throw new Error("Cart is already mounted");
  if (container.childNodes.length) throw new Error("Cart mount point must be empty");
  addRoot.querySelector(addSelector); // Validate the selector before changing DOM.
  const cart = createCart(items);
  const importedIds = new Set();
  for (const { id, quantity } of initialQuantities) {
    if (importedIds.has(id)) throw new Error("Duplicate initial cart line");
    cart.setQuantity(id, quantity);
    importedIds.add(id);
  }
  const words = {
    heading: "Basket",
    quantity: "Quantity",
    total: "Total",
    empty: "Your basket is empty.",
    ...labels,
  };
  const doc = container.ownerDocument;
  function element(tag, part, text) {
    const node = doc.createElement(tag);
    node.className = `wc-cart__${part}` + (classes[part] ? ` ${classes[part]}` : "");
    if (text !== undefined) node.textContent = text;
    return node;
  }
  const root = element("section", "root");
  root.setAttribute("aria-label", words.heading);
  const heading = element("h2", "heading", words.heading);
  const empty = element("p", "empty", words.empty);
  const list = element("ul", "lines");
  const summary = element("p", "summary", `${words.total}: `);
  const total = element("output", "total");
  total.setAttribute("data-cart-total", "");
  total.setAttribute("aria-live", "polite");
  summary.append(total);
  root.append(heading, empty, list, summary);
  const rows = new Map();
  let destroyed = false;

  function render() {
    const lines = cart.lines();
    empty.hidden = lines.length !== 0;
    for (const line of lines) {
      let nodes = rows.get(line.id);
      if (!nodes) {
        const row = element("li", "line");
        row.setAttribute("data-cart-id", line.id);
        const label = element("span", "item", line.label);
        const field = element("label", "quantity-label", `${words.quantity} `);
        const input = element("input", "quantity");
        input.type = "number";
        input.min = "1";
        input.step = "1";
        input.setAttribute("aria-label", `${line.label}: ${words.quantity}`);
        const subtotal = element("output", "subtotal");
        subtotal.setAttribute("data-cart-line-total", "");
        field.append(input);
        row.append(label, field, subtotal);
        list.append(row);
        nodes = { row, input, subtotal };
        rows.set(line.id, nodes);
      }
      nodes.input.value = String(line.quantity);
      nodes.subtotal.textContent = formatMoney(line.lineTotalMinor);
    }
    total.textContent = formatMoney(cart.totalMinor());
  }

  function active() {
    if (destroyed) throw new Error("Cart was destroyed");
  }
  const api = Object.freeze({
    add(id) {
      active();
      cart.add(id);
      render();
    },
    setQuantity(id, quantity) {
      active();
      cart.setQuantity(id, quantity);
      render();
    },
    snapshot() {
      active();
      return { lines: cart.lines(), totalMinor: cart.totalMinor() };
    },
    destroy() {
      if (destroyed) return;
      addRoot.removeEventListener("click", addFromHost);
      list.removeEventListener("input", changeQuantity);
      list.removeEventListener("change", changeQuantity);
      root.remove();
      shoppingCartMounts.delete(container);
      destroyed = true;
    },
  });
  function addFromHost(event) {
    const button = event.target.closest(addSelector);
    if (!button || !addRoot.contains(button)) return;
    const id = getItemId(button);
    // Resolve the actual host object before suppressing its original action.
    cart.add(id);
    event.preventDefault();
    render();
  }
  function changeQuantity(event) {
    const input = event.target;
    const row = input.closest("[data-cart-id]");
    if (!row || !list.contains(row)) return;
    const id = row.getAttribute("data-cart-id");
    if (rows.get(id)?.input !== input) return;
    const raw = input.value;
    if (/^[1-9]\d*$/.test(raw)) {
      try {
        cart.setQuantity(id, Number(raw));
        render();
        return;
      } catch (error) {
        if (!(error instanceof RangeError)) throw error;
      }
    }
    // Keep incomplete typing out of state; restore the last valid value on blur.
    if (event.type === "change") render();
  }
  render();
  container.append(root);
  addRoot.addEventListener("click", addFromHost);
  list.addEventListener("input", changeQuantity);
  list.addEventListener("change", changeQuantity);
  shoppingCartMounts.set(container, api);
  return api;
}

export { createCart, mountShoppingCart };
