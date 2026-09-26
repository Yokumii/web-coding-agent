// Host-first integration and representative composition for WebCompass Edit.
function wireParallax(host) {
  return bindParallax({section: host.section, layers: host.layers, maxOffset: host.maxOffset || 200});
}
