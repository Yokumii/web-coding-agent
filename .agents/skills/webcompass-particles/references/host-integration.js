// Host-first integration and representative composition for WebCompass Edit.
function wireParticles(host) {
  return mountParticles({container: host.container, count: host.count || 45, color: host.color, linkDistance: host.linkDistance || 90, interaction: host.interaction !== false});
}
