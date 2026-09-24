function mountTreeView({ container, nodes, onSelection = () => {} }) {
  const byId = new Map(),
    leaves = new Map(),
    parents = new Map(),
    expanded = new Set(),
    selected = new Set();
  function index(list, parent = null, ancestors = new Set()) {
    for (const node of list) {
      if (!node.id || byId.has(node.id) || ancestors.has(node))
        throw new Error("Tree needs unique IDs and no cycles");
      byId.set(node.id, node);
      parents.set(node.id, parent);
      if (node.children?.length) {
        index(node.children, node.id, new Set([...ancestors, node]));
        leaves.set(
          node.id,
          node.children.flatMap((child) => leaves.get(child.id)),
        );
      } else leaves.set(node.id, [node.id]);
    }
  }
  index(nodes);
  const root = document.createElement("section"),
    search = document.createElement("input"),
    tree = document.createElement("ul");
  root.className = "wc-tree";
  search.type = "search";
  search.setAttribute("aria-label", "Search tree");
  tree.setAttribute("role", "tree");
  tree.setAttribute("aria-label", "Items");
  root.append(search, tree);
  container.append(root);
  let query = "",
    focused = nodes[0]?.id;
  function relevant(node) {
    return node.label.toLocaleLowerCase().includes(query) || node.children?.some(relevant);
  }
  function choose(id, checked) {
    for (const leaf of leaves.get(id)) checked ? selected.add(leaf) : selected.delete(leaf);
    render();
    onSelection([...selected]);
  }
  function render() {
    const hadFocus = tree.contains(document.activeElement);
    tree.replaceChildren();
    function draw(list, target) {
      for (const node of list) {
        if (query && !relevant(node)) continue;
        const li = document.createElement("li"),
          row = document.createElement("div");
        li.setAttribute("role", "treeitem");
        li.dataset.treeId = node.id;
        li.tabIndex = node.id === focused ? 0 : -1;
        const ids = leaves.get(node.id),
          count = ids.filter((id) => selected.has(id)).length;
        li.setAttribute("aria-checked", count === ids.length ? "true" : count ? "mixed" : "false");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.tabIndex = -1;
        box.checked = count === ids.length;
        box.indeterminate = count > 0 && count < ids.length;
        box.setAttribute("aria-label", `Select ${node.label}`);
        box.onchange = () => {
          focused = node.id;
          choose(node.id, box.checked);
        };
        if (node.children?.length) {
          const open = Boolean(query) || expanded.has(node.id),
            toggle = document.createElement("button");
          toggle.type = "button";
          toggle.tabIndex = -1;
          toggle.textContent = open ? "−" : "+";
          toggle.setAttribute("aria-label", `${open ? "Collapse" : "Expand"} ${node.label}`);
          li.setAttribute("aria-expanded", String(open));
          toggle.onclick = () => {
            focused = node.id;
            expanded.has(node.id) ? expanded.delete(node.id) : expanded.add(node.id);
            render();
          };
          row.append(toggle);
          li.append(row);
          if (open) {
            const group = document.createElement("ul");
            group.setAttribute("role", "group");
            draw(node.children, group);
            li.append(group);
          }
        } else li.append(row);
        const label = document.createElement("span");
        label.textContent = node.label;
        row.append(box, label);
        target.append(li);
        li.addEventListener("focus", () => {
          focused = node.id;
          for (const el of tree.querySelectorAll("[role=treeitem]"))
            el.tabIndex = el === li ? 0 : -1;
        });
        li.addEventListener("keydown", (event) => {
          if (event.target !== li) return;
          const visible = [...tree.querySelectorAll("[role=treeitem]")],
            i = visible.indexOf(li);
          let destination;
          if (event.key === "ArrowDown") destination = visible[i + 1];
          else if (event.key === "ArrowUp") destination = visible[i - 1];
          else if (event.key === "Home") destination = visible[0];
          else if (event.key === "End") destination = visible.at(-1);
          else if (event.key === " ") {
            choose(node.id, count !== ids.length);
          } else if (event.key === "ArrowRight" && node.children?.length) {
            if (!expanded.has(node.id)) {
              expanded.add(node.id);
              render();
            } else destination = visible[i + 1];
          } else if (event.key === "ArrowLeft") {
            if (expanded.has(node.id)) {
              expanded.delete(node.id);
              render();
            } else destination = visible.find((el) => el.dataset.treeId === parents.get(node.id));
          } else return;
          event.preventDefault();
          event.stopPropagation();
          destination?.focus();
        });
      }
    }
    draw(nodes, tree);
    const entries = [...tree.querySelectorAll("[role=treeitem]")];
    if (!entries.some((el) => el.tabIndex === 0) && entries.length) {
      entries[0].tabIndex = 0;
      focused = entries[0].dataset.treeId;
    }
    if (hadFocus) entries.find((el) => el.dataset.treeId === focused)?.focus();
  }
  search.oninput = () => {
    query = search.value.trim().toLocaleLowerCase();
    render();
  };
  render();
  return {
    snapshot: () => ({ selected: [...selected], expanded: [...expanded], query }),
    destroy: () => root.remove(),
  };
}

export { mountTreeView };
