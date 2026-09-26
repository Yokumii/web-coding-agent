// Host-first integration and representative composition for WebCompass Edit.
function wireRealtimeDashboard(host) {
  const dashboard = mountRealtimeDashboard({container: host.container, metrics: host.metrics, fetchSample: host.fetchSample, intervalMs: host.intervalMs || 3000, historyLimit: host.historyLimit || 30});
  host.stream?.on('sample', sample => dashboard.update(sample));
  return dashboard;
}
