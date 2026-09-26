// Host-first integration and representative composition for WebCompass Edit.
function wirePageTransitions(host) {
  // views are existing sibling route/overlay nodes; do not create duplicate placeholder views.
  const transition = bindPageTransitions({views: host.views, initial: host.initial, duration: host.duration || 250, historyKey: host.historyKey || null});
  host.links.forEach(link => link.addEventListener('click', event => { event.preventDefault(); transition.go(link.dataset.view); }));
  host.closeButton?.addEventListener('click', () => transition.go(host.initial, {back:true}));
  return transition;
}
