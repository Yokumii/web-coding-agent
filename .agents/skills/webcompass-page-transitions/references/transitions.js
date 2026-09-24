function bindPageTransitions({ views, initial, duration = 250, historyKey = null }) {
  if (!views[initial] || duration < 0) throw new Error("Invalid initial view");
  const stage = views[initial].parentElement;
  if (Object.values(views).some((view) => view.parentElement !== stage))
    throw new Error("Views must be siblings in one stage");
  const stageStyle = { position: stage.style.position, minHeight: stage.style.minHeight };
  if (getComputedStyle(stage).position === "static") stage.style.position = "relative";
  const layout = new Map(
    Object.values(views).map((view) => [
      view,
      { position: view.style.position, inset: view.style.inset, width: view.style.width },
    ]),
  );
  const restoreLayout = () => {
    for (const [view, style] of layout) Object.assign(view.style, style);
    stage.style.minHeight = stageStyle.minHeight;
  };
  const saved = Object.values(views).map((element) => ({
    element,
    hidden: element.hidden,
    inert: element.inert,
  }));
  let active = initial,
    closed = false,
    busy = false,
    animations = [],
    queued = null,
    historyVersion = 0;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  for (const [id, element] of Object.entries(views)) {
    element.hidden = id !== active;
    element.inert = id !== active;
  }
  const ownHistory = () => history.state?.[historyKey];
  if (historyKey) history.replaceState({ ...history.state, [historyKey]: initial }, "");
  async function go(id, { back = false, record = true } = {}) {
    if (closed) return;
    if (!views[id]) throw new Error("Unknown view");
    if (busy) {
      queued = { id, back, record };
      return;
    }
    if (id === active) return;
    const version = historyVersion;
    busy = true;
    const old = views[active],
      next = views[id];
    old.inert = true;
    next.hidden = false;
    next.inert = true;
    stage.style.minHeight =
      Math.max(old.getBoundingClientRect().height, next.getBoundingClientRect().height) + "px";
    for (const view of [old, next])
      Object.assign(view.style, { position: "absolute", inset: "0", width: "100%" });
    const ms = reduced.matches ? 0 : duration;
    animations = [
      old.animate(
        [
          { opacity: 1, transform: "translateX(0)" },
          { opacity: 0, transform: `translateX(${back ? 10 : -10}%)` },
        ],
        { duration: ms, fill: "both" },
      ),
      next.animate(
        [
          { opacity: 0, transform: `translateX(${back ? -10 : 10}%)` },
          { opacity: 1, transform: "translateX(0)" },
        ],
        { duration: ms, fill: "both" },
      ),
    ];
    await Promise.all(animations.map((a) => a.finished.catch(() => {})));
    if (closed) return;
    old.hidden = true;
    next.inert = false;
    animations.forEach((a) => a.cancel());
    animations = [];
    active = id;
    busy = false;
    restoreLayout();
    if (historyKey && record && version === historyVersion)
      history.pushState({ ...history.state, [historyKey]: id }, "");
    next.querySelector("a,button,input,select,textarea,[tabindex]")?.focus();
    if (queued) {
      const request = queued;
      queued = null;
      await go(request.id, request);
    }
  }
  const events = new AbortController();
  if (historyKey)
    window.addEventListener(
      "popstate",
      () => {
        historyVersion++;
        const id = ownHistory();
        if (views[id]) go(id, { back: true, record: false });
      },
      { signal: events.signal },
    );
  return {
    go,
    snapshot: () => ({ active, busy }),
    destroy() {
      closed = true;
      events.abort();
      restoreLayout();
      stage.style.position = stageStyle.position;
      animations.forEach((a) => a.cancel());
      saved.forEach(({ element, hidden, inert }) => {
        element.hidden = hidden;
        element.inert = inert;
      });
    },
  };
}
