function bindParallax({ section, layers, maxOffset = 200 }) {
  if (
    layers.length < 2 ||
    new Set(layers.map((l) => l.speed)).size < 2 ||
    layers.some((l) => !Number.isFinite(l.speed))
  )
    throw new Error("Parallax requires distinct finite layer speeds");
  const saved = layers.map(({ element }) => ({
    transform: element.style.transform,
    willChange: element.style.willChange,
  }));
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  let frame = 0,
    closed = false,
    visible = true;
  function draw() {
    frame = 0;
    if (closed) return;
    const offset = Math.max(-maxOffset, Math.min(maxOffset, -section.getBoundingClientRect().top));
    layers.forEach(({ element, speed }, i) => {
      element.style.transform = reduced.matches
        ? saved[i].transform
        : `${saved[i].transform} translate3d(0,${offset * speed}px,0)`;
    });
  }
  function schedule() {
    if (!closed && visible && !frame) frame = requestAnimationFrame(draw);
  }
  const observer = new IntersectionObserver((entries) => {
    visible = entries[0].isIntersecting;
    if (visible) schedule();
  });
  observer.observe(section);
  const events = new AbortController();
  window.addEventListener("scroll", schedule, { passive: true, signal: events.signal });
  window.addEventListener("resize", schedule, { signal: events.signal });
  reduced.addEventListener("change", schedule, { signal: events.signal });
  layers.forEach(({ element }) => (element.style.willChange = "transform"));
  schedule();
  return {
    refresh: schedule,
    destroy() {
      closed = true;
      cancelAnimationFrame(frame);
      events.abort();
      observer.disconnect();
      layers.forEach(({ element }, i) => {
        element.style.transform = saved[i].transform;
        element.style.willChange = saved[i].willChange;
      });
    },
  };
}

export { bindParallax };
