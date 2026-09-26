// Host-first integration and representative composition for WebCompass Edit.
function wireTreeView(host) {
  return mountTreeView({container: host.container, nodes: host.state.tree, onSelection: ids => host.setSelectedLeafIds(ids), onNodeFocus: node => host.showDetails?.(node), isDisabled: host.isDisabled});
}
