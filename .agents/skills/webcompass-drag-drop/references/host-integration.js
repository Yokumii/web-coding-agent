// Host-first integration and representative composition for WebCompass Edit.
function wireDragDrop(host) {
  return bindDragDrop({lists: host.lists, itemSelector: host.itemSelector || '[data-item-id]', getId: host.getId || (node => node.dataset.itemId), handleSelector: host.handleSelector, onChange: order => host.persistOrder(order), onInvalidDrop: item => host.showInvalidDrop?.(item)});
}
