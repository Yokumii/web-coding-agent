function bindDragDrop({ lists, itemSelector, getId, handleSelector,
  onChange = () => {}, onInvalidDrop = () => {} }) {
  if (
    lists.some(({ element }) =>
      [...element.querySelectorAll(itemSelector)].some((node) => node.parentElement !== element),
    )
  )
    throw new Error("Draggable items must be direct list children");
  let dragged = null;
  const placeholder = document.createElement("div");
  placeholder.className = "wc-drop-placeholder";
  placeholder.setAttribute("aria-hidden", "true");
  const controllers = [],
    saved = new Map();
  const snapshot = () =>
    lists.map(({ id, element }) => ({
      id,
      items: [...element.querySelectorAll(itemSelector)].map(getId),
    }));
  const nodes = lists.flatMap(({ element }) => [...element.querySelectorAll(itemSelector)]);
  if (new Set(nodes.map(getId)).size !== nodes.length)
    throw new Error("Draggable IDs must be globally unique");
  for (const node of nodes) {
    saved.set(node, {
      draggable: node.getAttribute("draggable"),
      tabindex: node.getAttribute("tabindex"),
    });
    node.draggable = true;
    if (handleSelector) for (const handle of node.querySelectorAll(handleSelector)) {
      saved.set(handle, { draggable: handle.getAttribute("draggable") });
      handle.draggable = true;
    }
    if (!node.hasAttribute("tabindex")) node.tabIndex = 0;
  }
  function clear() {
    placeholder.remove();
    if (dragged) dragged.classList.remove("wc-dragging");
    for (const { element } of lists) element.classList.remove("wc-drop-target");
    dragged = null;
  }
  for (const { element } of lists) {
    const controller = new AbortController();
    controllers.push(controller);
    const listen = (event, fn) =>
      element.addEventListener(event, fn, { signal: controller.signal });
    listen("dragstart", (event) => {
      const item = event.target.closest(itemSelector);
      if (!item || !saved.has(item)) return;
      dragged = item;
      placeholder.style.height = `${item.getBoundingClientRect().height}px`;
      item.classList.add("wc-dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", String(getId(item)));
    });
    listen("dragover", (event) => {
      if (!dragged) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      element.classList.add("wc-drop-target");
      const target = event.target.closest(itemSelector);
      if (target && target !== dragged && target.parentElement === element) {
        const rect = target.getBoundingClientRect();
        element.insertBefore(placeholder,
          event.clientY > rect.top + rect.height / 2 ? target.nextSibling : target);
      } else if (!target && event.target !== placeholder) element.append(placeholder);
    });
    listen("dragleave", (event) => {
      if (!element.contains(event.relatedTarget)) element.classList.remove("wc-drop-target");
    });
    listen("drop", (event) => {
      if (!dragged) return;
      event.preventDefault();
      const target = event.target.closest(itemSelector),
        item = dragged;
      if (placeholder.parentElement === element) {
        element.insertBefore(item, placeholder);
      } else if (target && target !== item && element.contains(target)) {
        const rect = target.getBoundingClientRect();
        element.insertBefore(
          item,
          event.clientY > rect.top + rect.height / 2 ? target.nextSibling : target,
        );
      } else if (!target) element.append(item);
      clear();
      item.focus();
      onChange(snapshot());
    });
    listen("dragend", () => {
      const item = dragged;
      clear();
      if (item) onInvalidDrop(item);
    });
    listen("keydown", (event) => {
      const item = event.target.closest(itemSelector);
      if (!item || event.target !== item || !event.altKey ||
          !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) return;
      if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
        const index = lists.findIndex((list) => list.element === element);
        const next = lists[index + (event.key === "ArrowLeft" ? -1 : 1)];
        if (!next) return;
        event.preventDefault();
        event.stopPropagation();
        next.element.append(item);
        item.focus();
        onChange(snapshot());
        return;
      }
      const siblings = [...element.querySelectorAll(itemSelector)],
        index = siblings.indexOf(item);
      const target = siblings[index + (event.key === "ArrowUp" ? -1 : 1)];
      if (!target) return;
      event.preventDefault();
      event.stopPropagation();
      element.insertBefore(item, event.key === "ArrowUp" ? target : target.nextSibling);
      item.focus();
      onChange(snapshot());
    });
  }
  const outside = new AbortController();
  controllers.push(outside);
  document.addEventListener("dragover", (event) => {
    if (!dragged || lists.some(({ element }) => element.contains(event.target))) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "none";
  }, { signal: outside.signal });
  document.addEventListener("drop", (event) => {
    if (!dragged || lists.some(({ element }) => element.contains(event.target))) return;
    event.preventDefault();
    const item = dragged;
    clear();
    onInvalidDrop(item);
  }, { signal: outside.signal });
  return {
    snapshot,
    destroy() {
      clear();
      controllers.forEach((c) => c.abort());
      for (const [node, attributes] of saved)
        for (const [key, value] of Object.entries(attributes))
          value === null ? node.removeAttribute(key) : node.setAttribute(key, value);
    },
  };
}
