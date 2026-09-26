function mountDataTable({container, rows, columns, getId, pageSize = 10, pageSizes = [],
  selectable = false, bulkActions = [], onSelection = () => {}, onEdit = null,
  onBulkAction = null}) {
  const sizes = [...new Set([pageSize, ...pageSizes])].filter(n => Number.isInteger(n) && n > 0);
  if (!sizes.length || !columns.length) throw new TypeError("Invalid table configuration");
  if (container.childNodes.length) throw new Error("Use an empty table mount");
  let data = [], page = 0, currentPageSize = pageSize, query = "", sortKey = null, direction = 1;
  const selected = new Set(), filters = Object.fromEntries(columns.map(c => [c.key, ""]));
  const root = document.createElement("section"); root.className = "wc-table";
  const controls = document.createElement("div"); controls.className = "wc-table__controls";
  const search = document.createElement("input"); search.type = "search";
  search.setAttribute("aria-label", "Filter records"); controls.append(search);
  if (sizes.length > 1) {
    const label = document.createElement("label"), select = document.createElement("select");
    label.textContent = "Rows per page "; select.setAttribute("aria-label", "Rows per page");
    for (const size of sizes) {
      const option = document.createElement("option"); option.value = option.textContent = String(size);
      option.selected = size === pageSize; select.append(option);
    }
    select.onchange = () => { currentPageSize = Number(select.value); page = 0; render(); };
    label.append(select); controls.append(label);
  }
  const bulk = document.createElement("div"), bulkCount = document.createElement("output");
  bulk.className = "wc-table__bulk"; bulkCount.setAttribute("aria-live", "polite"); bulk.append(bulkCount);
  for (const action of bulkActions) {
    const button = document.createElement("button"); button.type = "button"; button.textContent = action.label;
    button.onclick = () => {
      const ids = [...selected]; if (!ids.length || !onBulkAction) return;
      if (action.confirm && !globalThis.confirm(action.confirm.replace("{count}", ids.length))) return;
      onBulkAction(action.id, ids);
    };
    bulk.append(button);
  }
  controls.append(bulk);
  const table = document.createElement("table"), head = table.createTHead(), body = table.createTBody();
  const headerRow = head.insertRow(), filterRow = head.insertRow(), headers = new Map();
  filterRow.className = "wc-table__filters"; let selectAll = null;
  if (selectable) {
    const th = document.createElement("th"); selectAll = document.createElement("input");
    selectAll.type = "checkbox"; selectAll.setAttribute("aria-label", "Select visible records");
    th.append(selectAll); headerRow.append(th); filterRow.append(document.createElement("th"));
  }
  for (const column of columns) {
    const th = document.createElement("th"), sort = document.createElement("button");
    th.scope = "col"; sort.type = "button"; sort.textContent = column.label;
    sort.onclick = () => { direction = sortKey === column.key ? -direction : 1; sortKey = column.key; render(); };
    th.append(sort); headerRow.append(th); headers.set(column.key, th);
    const cell = document.createElement("th");
    if (["text", "select", "sign"].includes(column.filter)) {
      const input = column.filter === "text" ? document.createElement("input") : document.createElement("select");
      input.setAttribute("aria-label", `Filter ${column.label}`);
      if (column.filter === "text") input.type = "search";
      else for (const value of column.filter === "select" ? ["", ...(column.filterOptions || [])] : ["", "positive", "negative"]) {
        const option = document.createElement("option"); option.value = value;
        option.textContent = value ? value[0].toUpperCase() + value.slice(1) : "All"; input.append(option);
      }
      input.oninput = input.onchange = () => { filters[column.key] = input.value; page = 0; render(); };
      cell.append(input);
    }
    filterRow.append(cell);
  }
  filterRow.hidden = !columns.some(c => c.filter);
  const scroller = document.createElement("div"); scroller.className = "wc-table__scroll"; scroller.append(table);
  const status = document.createElement("p"), nav = document.createElement("nav");
  const previous = document.createElement("button"), pagesList = document.createElement("span"), next = document.createElement("button");
  status.setAttribute("aria-live", "polite"); nav.setAttribute("aria-label", "Table pages");
  previous.type = next.type = "button"; previous.textContent = "Previous"; next.textContent = "Next";
  nav.append(previous, pagesList, next); root.append(controls, scroller, status, nav); container.append(root);
  const text = (row, col) => String(col.format ? col.format(row[col.key], row) : (row[col.key] ?? ""));
  function filtered() {
    const needle = query.toLocaleLowerCase();
    const result = data.filter(row => (!needle || columns.some(c => text(row, c).toLocaleLowerCase().includes(needle))) &&
      columns.every(c => {
        const filter = filters[c.key]; if (!filter) return true;
        if (c.filter === "sign") return filter === "positive" ? Number(row[c.key]) >= 0 : Number(row[c.key]) < 0;
        return text(row, c).toLocaleLowerCase().includes(filter.toLocaleLowerCase());
      }));
    if (sortKey !== null) result.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      if (av == null || bv == null) return av == null ? (bv == null ? 0 : 1) : -1;
      return direction * (typeof av === "number" && typeof bv === "number" ? av - bv :
        String(av).localeCompare(String(bv), undefined, {numeric:true}));
    });
    return result;
  }
  function editCell(cell, row, column) {
    const input = document.createElement("input"), save = document.createElement("button"), cancel = document.createElement("button");
    input.type = "text"; input.value = row[column.key] ?? ""; save.type = cancel.type = "button";
    save.textContent = "Save"; cancel.textContent = "Cancel";
    save.onclick = async () => {
      const value = input.value.trim(); if (!value) return input.focus();
      await onEdit({id:getId(row), key:column.key, value, row:{...row}});
    };
    cancel.onclick = render; cell.replaceChildren(input, save, cancel); input.focus();
  }
  function render() {
    const result = filtered(), pages = Math.max(1, Math.ceil(result.length / currentPageSize));
    page = Math.min(page, pages - 1);
    const visible = result.slice(page * currentPageSize, (page + 1) * currentPageSize); body.replaceChildren();
    for (const row of visible) {
      const tr = body.insertRow(), id = getId(row); tr.dataset.rowId = String(id);
      if (selectable) {
        const cell = tr.insertCell(), box = document.createElement("input"); box.type = "checkbox";
        box.checked = selected.has(id); box.setAttribute("aria-label", `Select ${text(row, columns[0])}`);
        box.onchange = () => { box.checked ? selected.add(id) : selected.delete(id); onSelection([...selected]); render(); };
        cell.append(box); cell.dataset.label = "Select";
      }
      for (const column of columns) {
        const cell = tr.insertCell(), value = document.createElement("span"); cell.dataset.label = column.label;
        value.textContent = text(row, column); cell.append(value);
        if (column.editable && onEdit) {
          const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "Edit";
          edit.setAttribute("aria-label", `Edit ${column.label} for ${text(row, columns[0])}`);
          edit.onclick = () => editCell(cell, row, column); cell.append(edit);
        }
      }
    }
    if (!result.length) {
      const cell = body.insertRow().insertCell(); cell.colSpan = columns.length + Number(selectable);
      cell.textContent = "No matching records";
    }
    for (const [key, th] of headers) th.setAttribute("aria-sort", key === sortKey ?
      (direction === 1 ? "ascending" : "descending") : "none");
    status.textContent = `${result.length} records · Page ${page + 1} of ${pages}`;
    previous.disabled = page === 0; next.disabled = page + 1 >= pages; pagesList.replaceChildren();
    for (let index = 0; index < pages; index++) {
      const button = document.createElement("button"); button.type = "button"; button.textContent = String(index + 1);
      button.setAttribute("aria-current", index === page ? "page" : "false");
      button.onclick = () => { page = index; render(); }; pagesList.append(button);
    }
    if (selectAll) {
      const ids = visible.map(getId), count = ids.filter(id => selected.has(id)).length;
      selectAll.checked = ids.length > 0 && count === ids.length; selectAll.indeterminate = count > 0 && count < ids.length;
    }
    bulk.hidden = !selected.size || !bulkActions.length;
    bulkCount.value = bulkCount.textContent = `${selected.size} selected`;
  }
  function setRows(value) {
    const ids = value.map(getId);
    if (ids.some(id => id == null) || new Set(ids).size !== ids.length) throw new Error("Rows need unique IDs");
    data = value.map(row => ({...row})); const oldSize = selected.size;
    for (const id of selected) if (!ids.includes(id)) selected.delete(id);
    if (oldSize !== selected.size) onSelection([...selected]); render();
  }
  search.oninput = () => { query = search.value; page = 0; render(); };
  previous.onclick = () => { page = Math.max(0, page - 1); render(); };
  next.onclick = () => { page++; render(); };
  if (selectAll) selectAll.onchange = () => {
    for (const row of filtered().slice(page * currentPageSize, (page + 1) * currentPageSize))
      selectAll.checked ? selected.add(getId(row)) : selected.delete(getId(row));
    onSelection([...selected]); render();
  };
  setRows(rows);
  return {setRows, snapshot:() => ({page, pageSize:currentPageSize, query, filters:{...filters},
    sortKey, direction, selected:[...selected]}), destroy:() => root.remove()};
}
