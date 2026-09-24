function mountRealtimeDashboard({
  container,
  metrics,
  fetchSample,
  intervalMs = 3000,
  historyLimit = 30,
}) {
  if (
    intervalMs < 50 ||
    !Number.isInteger(historyLimit) ||
    historyLimit < 2 ||
    !metrics.length ||
    new Set(metrics.map((m) => m.key)).size !== metrics.length
  )
    throw new Error("Invalid dashboard configuration");
  const root = document.createElement("section");
  root.className = "wc-dashboard";
  const status = document.createElement("p");
  status.setAttribute("role", "status");
  const statusMessage = document.createElement("span");
  status.append(statusMessage);
  root.append(status);
  const series = new Map(),
    views = new Map();
  let closed = false,
    pending = false,
    timer,
    controller,
    lastSample = null;
  for (const metric of metrics) {
    const panel = document.createElement("section"),
      title = document.createElement("h3"),
      value = document.createElement("output");
    title.textContent = metric.label;
    value.dataset.metric = metric.key;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 200 60");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", `${metric.label} history`);
    const line = document.createElementNS(svg.namespaceURI, "polyline");
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "currentColor");
    line.setAttribute("stroke-width", "2");
    svg.append(line);
    panel.append(title, value, svg);
    root.append(panel);
    series.set(metric.key, []);
    views.set(metric.key, { value, line });
  }
  function update(sample) {
    if (closed) return;
    if (metrics.some((m) => !Number.isFinite(sample[m.key])))
      throw new TypeError("Metric values must be finite numbers");
    lastSample = Object.fromEntries(metrics.map((m) => [m.key, sample[m.key]]));
    for (const metric of metrics) {
      const values = series.get(metric.key);
      values.push(sample[metric.key]);
      if (values.length > historyLimit) values.shift();
      const { value, line } = views.get(metric.key);
      value.textContent = metric.format
        ? metric.format(sample[metric.key])
        : String(sample[metric.key]);
      const min = Math.min(...values),
        span = Math.max(...values) - min || 1;
      line.setAttribute(
        "points",
        values
          .map(
            (v, i) =>
              `${(i * 200) / Math.max(1, values.length - 1)},${55 - ((v - min) * 50) / span}`,
          )
          .join(" "),
      );
    }
    statusMessage.textContent = `Updated ${new Date().toISOString()}`;
  }
  async function refresh() {
    if (closed || pending || !fetchSample) return;
    pending = true;
    controller = new AbortController();
    statusMessage.textContent = "Updating…";
    try {
      const sample = await fetchSample({ signal: controller.signal });
      if (!closed) update(sample);
    } catch (error) {
      if (!closed) statusMessage.textContent = `Update failed: ${error.message}`;
    } finally {
      pending = false;
      if (!closed) timer = setTimeout(refresh, intervalMs);
    }
  }
  container.append(root);
  if (fetchSample) refresh();
  else statusMessage.textContent = "Waiting for data";
  return {
    update,
    snapshot: () => ({
      sample: lastSample && { ...lastSample },
      series: Object.fromEntries([...series].map(([k, v]) => [k, [...v]])),
    }),
    destroy() {
      closed = true;
      clearTimeout(timer);
      controller?.abort();
      root.remove();
    },
  };
}

export { mountRealtimeDashboard };
