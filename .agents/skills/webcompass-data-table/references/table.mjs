function mountDataTable({
  container,
  rows,
  columns,
  getId,
  pageSize = 10,
  selectable = false,
  onSelection = () => {},
}) {
  if (!Number.isInteger(pageSize) || pageSize < 1 || !columns.length)
    throw new TypeError("Invalid table configuration");
  const original = [...container.childNodes];
  if (original.length) throw new Error("Use an empty table mount");
  let data = [],
    page = 0,
    query = "",
    sortKey = null,
    direction = 1;
  const selected = new Set();
  const root = document.createElement("section");
  root.className = "wc-table";
  const search = document.createElement("input");
  search.type = "search";
  search.setAttribute("aria-label", "Filter records");
  const table = document.createElement("table"),
    head = table.createTHead(),
    body = table.createTBody();
  const status = document.createElement("p");
  status.setAttribute("aria-live", "polite");
  const previous = document.createElement("button"),
    next = document.createElement("button");
  previous.type = next.type = "button";
  previous.textContent = "Previous";
  next.textContent = "Next";
  const scroller = document.createElement("div");
  scroller.className = "wc-table__scroll";
  scroller.append(table);
  root.append(search, scroller, status, previous, next);
  container.append(root);
  const headers = new Map(),
    hr = head.insertRow();
  if (selectable) {
    const th = document.createElement("th");
    th.textContent = "Select";
    hr.append(th);
  }
  for (const column of columns) {
    const th = document.createElement("th"),
      button = document.createElement("button");
    th.scope = "col";
    button.type = "button";
    button.textContent = column.label;
    button.onclick = () => {
      direction = sortKey === column.key ? -direction : 1;
      sortKey = column.key;
      page = 0;
      render();
    };
    th.append(button);
    hr.append(th);
    headers.set(column.key, th);
  }
  const text = (row, col) =>
    String(col.format ? col.format(row[col.key], row) : (row[col.key] ?? ""));
  function filtered() {
    const result = data.filter((row) =>
      columns.some((col) => text(row, col).toLocaleLowerCase().includes(query.toLocaleLowerCase())),
    );
    if (sortKey !== null)
      result.sort((a, b) => {
        const av = a[sortKey],
          bv = b[sortKey];
        if (av == null || bv == null) return av == null ? (bv == null ? 0 : 1) : -1;
        return (
          direction *
          (typeof av === "number" && typeof bv === "number"
            ? av - bv
            : String(av).localeCompare(String(bv), undefined, { numeric: true }))
        );
      });
    return result;
  }
  function render() {
    const result = filtered(),
      pages = Math.max(1, Math.ceil(result.length / pageSize));
    page = Math.min(page, pages - 1);
    body.replaceChildren();
    for (const row of result.slice(page * pageSize, (page + 1) * pageSize)) {
      const tr = body.insertRow();
      tr.dataset.rowId = String(getId(row));
      if (selectable) {
        const td = tr.insertCell(),
          box = document.createElement("input");
        box.type = "checkbox";
        box.checked = selected.has(getId(row));
        box.setAttribute("aria-label", `Select ${text(row, columns[0])}`);
        box.onchange = () => {
          box.checked ? selected.add(getId(row)) : selected.delete(getId(row));
          onSelection([...selected]);
        };
        td.append(box);
        td.dataset.label = "Select";
      }
      for (const col of columns) {
        const td = tr.insertCell();
        td.dataset.label = col.label;
        td.textContent = text(row, col);
      }
    }
    if (!result.length) {
      const td = body.insertRow().insertCell();
      td.colSpan = columns.length + Number(selectable);
      td.textContent = "No matching records";
    }
    for (const [key, th] of headers)
      th.setAttribute(
        "aria-sort",
        key === sortKey ? (direction === 1 ? "ascending" : "descending") : "none",
      );
    status.textContent = `${result.length} records · Page ${page + 1} of ${pages}`;
    previous.disabled = page === 0;
    next.disabled = page + 1 >= pages;
  }
  function setRows(value) {
    const ids = value.map(getId);
    if (ids.some((id) => id == null) || new Set(ids).size !== ids.length)
      throw new Error("Rows need unique IDs");
    data = value.map((row) => ({ ...row }));
    const previousSelection = [...selected];
    for (const id of selected) if (!ids.includes(id)) selected.delete(id);
    if (previousSelection.length !== selected.size) onSelection([...selected]);
    render();
  }
  search.oninput = () => {
    query = search.value;
    page = 0;
    render();
  };
  previous.onclick = () => {
    page = Math.max(0, page - 1);
    render();
  };
  next.onclick = () => {
    page++;
    render();
  };
  setRows(rows);
  return {
    setRows,
    snapshot: () => ({ page, query, sortKey, direction, selected: [...selected] }),
    destroy: () => root.remove(),
  };
}

export { mountDataTable };
