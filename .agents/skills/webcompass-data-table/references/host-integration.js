// Host-first integration and representative composition for WebCompass Edit.
function wireDataTable(host) {
  // host.state.records is the existing route/store collection, never a copied fixture.
  let table;
  table = mountDataTable({container: host.container, rows: host.state.records, columns: host.columns, getId: host.getId || (row => String(row.id)), pageSize: host.pageSize || 10, pageSizes: host.pageSizes, selectable: !!host.selectable, bulkActions: host.bulkActions || [], onSelection: ids => host.setSelection?.(ids), onEdit: change => Promise.resolve(host.updateRow(change)).then(() => table.setRows(host.state.records)), onBulkAction: (action, ids) => Promise.resolve(host.bulkAction(action, ids)).then(() => table.setRows(host.state.records))});
  return table;
}
