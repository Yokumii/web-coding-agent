function mountTreeView({container, nodes, onSelection = () => {}, onNodeFocus = () => {},
  isDisabled = node => Boolean(node.disabled)}) {
  const byId = new Map(), parents = new Map(), expanded = new Set(), selected = new Set();
  const loaded = new Map(), loading = new Set(), loadErrors = new Map();
  function index(list, parent = null) {
    if (!Array.isArray(list)) throw new TypeError("Tree children must be an array");
    const records = [], localIds = new Set();
    function collect(children, parentId, ancestors) {
      for (const node of children) {
        if (typeof node.id !== "string" || !node.id || byId.has(node.id) || localIds.has(node.id) || ancestors.has(node))
          throw new Error("Tree needs unique string IDs and no cycles");
        localIds.add(node.id); records.push([node, parentId]);
        if (Array.isArray(node.children)) collect(node.children, node.id, new Set([...ancestors, node]));
      }
    }
    collect(list, parent, new Set());
    for (const [node, parentId] of records) { byId.set(node.id, node); parents.set(node.id, parentId); }
  }
  index(nodes);
  const childrenOf = node => Array.isArray(node.children) ? node.children : (loaded.get(node.id) || []);
  const isBranch = node => childrenOf(node).length > 0 || typeof node.loadChildren === "function";
  function leafIds(node) {
    const children = childrenOf(node);
    if (!children.length) return isBranch(node) ? [] : (isDisabled(node) ? [] : [node.id]);
    return children.flatMap(leafIds);
  }
  const root = document.createElement("section"), search = document.createElement("input");
  const report = document.createElement("p"), tree = document.createElement("ul");
  root.className = "wc-tree"; search.type = "search"; search.setAttribute("aria-label", "Search tree");
  report.setAttribute("role", "status"); tree.setAttribute("role", "tree"); tree.setAttribute("aria-label", "Items");
  root.append(search, report, tree); container.append(root);
  let query = "", focused = nodes[0]?.id;
  function relevant(node) {
    return node.label.toLocaleLowerCase().includes(query) || childrenOf(node).some(relevant);
  }
  function choose(node, checked) {
    for (const id of leafIds(node)) checked ? selected.add(id) : selected.delete(id);
    render(); onSelection([...selected]);
  }
  async function open(node) {
    if (loading.has(node.id)) return;
    if (typeof node.loadChildren === "function" && !loaded.has(node.id)) {
      loading.add(node.id); loadErrors.delete(node.id); render();
      try {
        const children = await node.loadChildren();
        index(children, node.id); loaded.set(node.id, children);
      } catch (error) {
        loadErrors.set(node.id, error instanceof Error ? error.message : String(error));
      } finally {
        loading.delete(node.id);
      }
    }
    if (!loadErrors.has(node.id)) expanded.add(node.id);
    render();
  }
  function render() {
    const hadFocus = tree.contains(document.activeElement); tree.replaceChildren(); let matches = 0;
    function draw(list, target) {
      for (const node of list) {
        if (query && !relevant(node)) continue;
        if (query && node.label.toLocaleLowerCase().includes(query)) matches++;
        const item = document.createElement("li"), row = document.createElement("div");
        item.setAttribute("role", "treeitem"); item.dataset.treeId = node.id;
        item.tabIndex = node.id === focused ? 0 : -1;
        const ids = leafIds(node), count = ids.filter(id => selected.has(id)).length;
        item.setAttribute("aria-checked", ids.length && count === ids.length ? "true" : count ? "mixed" : "false");
        const box = document.createElement("input"); box.type = "checkbox"; box.tabIndex = -1;
        box.checked = ids.length > 0 && count === ids.length; box.indeterminate = count > 0 && count < ids.length;
        box.disabled = isDisabled(node) || (!ids.length && isBranch(node));
        box.setAttribute("aria-label", `Select ${node.label}`);
        box.onchange = () => { focused = node.id; choose(node, box.checked); };
        const branch = isBranch(node), shown = Boolean(query) || expanded.has(node.id);
        if (branch) {
          const toggle = document.createElement("button"); toggle.type = "button"; toggle.tabIndex = -1;
          toggle.textContent = loading.has(node.id) ? "…" : (shown ? "−" : "+");
          toggle.disabled = loading.has(node.id);
          toggle.setAttribute("aria-label", `${shown ? "Collapse" : "Expand"} ${node.label}`);
          item.setAttribute("aria-expanded", String(shown));
          toggle.onclick = () => {
            focused = node.id;
            if (shown && !query) { expanded.delete(node.id); render(); } else open(node);
          };
          row.append(toggle);
        }
        const label = document.createElement("span"); label.textContent = node.label; row.append(box, label); item.append(row);
        if (loadErrors.has(node.id)) {
          const error = document.createElement("p"), retry = document.createElement("button");
          error.setAttribute("role", "alert"); error.textContent = loadErrors.get(node.id);
          retry.type = "button"; retry.textContent = "Retry";
          retry.onclick = () => { loaded.delete(node.id); open(node); }; item.append(error, retry);
        } else if (shown && childrenOf(node).length) {
          const group = document.createElement("ul"); group.setAttribute("role", "group");
          draw(childrenOf(node), group); item.append(group);
        } else if (loading.has(node.id)) {
          const pending = document.createElement("p"); pending.setAttribute("role", "status");
          pending.textContent = "Loading…"; item.append(pending);
        }
        target.append(item);
        item.addEventListener("focus", () => {
          focused = node.id; onNodeFocus(node);
          for (const entry of tree.querySelectorAll("[role=treeitem]")) entry.tabIndex = entry === item ? 0 : -1;
        });
        row.addEventListener("click", event => {
          if (!event.target.closest("button,input")) { focused = node.id; onNodeFocus(node); item.focus(); }
        });
        item.addEventListener("keydown", event => {
          if (event.target !== item) return;
          const visible = [...tree.querySelectorAll("[role=treeitem]")], position = visible.indexOf(item);
          let destination;
          if (event.key === "ArrowDown") destination = visible[position + 1];
          else if (event.key === "ArrowUp") destination = visible[position - 1];
          else if (event.key === "Home") destination = visible[0];
          else if (event.key === "End") destination = visible.at(-1);
          else if (event.key === " " || event.key === "Enter") choose(node, count !== ids.length);
          else if (event.key === "ArrowRight" && branch) {
            if (!shown) open(node); else destination = visible[position + 1];
          } else if (event.key === "ArrowLeft") {
            if (expanded.has(node.id)) { expanded.delete(node.id); render(); }
            else destination = visible.find(entry => entry.dataset.treeId === parents.get(node.id));
          } else return;
          event.preventDefault(); event.stopPropagation(); destination?.focus();
        });
      }
    }
    draw(nodes, tree);
    const entries = [...tree.querySelectorAll("[role=treeitem]")];
    if (!entries.some(entry => entry.tabIndex === 0) && entries.length) {
      entries[0].tabIndex = 0; focused = entries[0].dataset.treeId;
    }
    report.textContent = query ? `${matches} matches` : "";
    if (query && !entries.length) tree.append(Object.assign(document.createElement("li"), {textContent:"No matches"}));
    if (hadFocus) entries.find(entry => entry.dataset.treeId === focused)?.focus();
  }
  search.oninput = () => { query = search.value.trim().toLocaleLowerCase(); render(); };
  render();
  return {
    snapshot:() => ({selected:[...selected], expanded:[...expanded], query,
      loaded:[...loaded.keys()], loading:[...loading]}),
    setSelected(ids) {
      selected.clear();
      for (const id of ids) if (byId.has(id) && !isBranch(byId.get(id)) && !isDisabled(byId.get(id))) selected.add(id);
      render(); onSelection([...selected]);
    },
    destroy:() => root.remove(),
  };
}

export { mountTreeView };
