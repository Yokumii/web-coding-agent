// Host-first integration and representative composition for WebCompass Edit.
function wireInfiniteScroll(host) {
  // loadPage slices the host's current filtered collection; it must not fabricate later pages.
  const feed = mountInfiniteScroll({container: host.container, root: host.root, loadPage: (page, o) => host.loadPage(host.state.filteredItems, page, o), getId: host.getId || (item => String(item.id)), renderItem: item => host.renderExistingItem(item), renderLoading: host.renderLoading, initialLoad: host.initialLoad !== false, endText: host.endText || 'End of content'});
  host.onBeforeNavigate?.(() => host.saveFeedState({...feed.snapshot(), scrollTop: host.root?.scrollTop || scrollY}));
  return feed;
}
