// Host-first integration and representative composition for WebCompass Edit.
function wireSkeletonLoading(host) {
  return mountSkeletonLoading({container: host.container, skeleton: host.skeleton, load: host.load, renderContent: host.renderContent, duration: host.duration || 180});
}
