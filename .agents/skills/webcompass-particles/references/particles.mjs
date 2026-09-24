function mountParticles({
  container,
  count = 45,
  color = "#5279c7",
  linkDistance = 90,
  interaction = true,
}) {
  if (!Number.isInteger(count) || count < 1 || count > 300)
    throw new Error("Particle count must be 1..300");
  const canvas = document.createElement("canvas");
  canvas.className = "wc-particles";
  canvas.setAttribute("aria-hidden", "true");
  const originalPosition = container.style.position;
  if (getComputedStyle(container).position === "static") container.style.position = "relative";
  canvas.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none";
  container.append(canvas);
  const ctx = canvas.getContext("2d");
  let width = 1,
    height = 1,
    frame = 0,
    last = 0,
    visible = true,
    closed = false;
  const reduced = matchMedia("(prefers-reduced-motion: reduce)"),
    pointer = { x: -10000, y: -10000 };
  const particles = Array.from({ length: count }, () => ({
    x: Math.random(),
    y: Math.random(),
    vx: (Math.random() - 0.5) * 0.06,
    vy: (Math.random() - 0.5) * 0.06,
  }));
  function paint(time) {
    frame = 0;
    if (closed) return;
    const delta = Math.min((time - last) / 1000 || 0, 0.05);
    last = time;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = color;
    ctx.strokeStyle = color;
    for (const p of particles) {
      if (!reduced.matches) {
        const dx = p.x * width - pointer.x,
          dy = p.y * height - pointer.y,
          d = Math.hypot(dx, dy);
        if (interaction && d > 0 && d < 80) {
          p.vx += (dx / d) * 0.08 * delta;
          p.vy += (dy / d) * 0.08 * delta;
        }
        p.vx = Math.max(-0.2, Math.min(0.2, p.vx));
        p.vy = Math.max(-0.2, Math.min(0.2, p.vy));
        p.x = (p.x + p.vx * delta + 1) % 1;
        p.y = (p.y + p.vy * delta + 1) % 1;
      }
      ctx.beginPath();
      ctx.arc(p.x * width, p.y * height, 2, 0, Math.PI * 2);
      ctx.fill();
    }
    for (let i = 0; i < count; i++)
      for (let j = i + 1; j < count; j++) {
        const a = particles[i],
          b = particles[j],
          d = Math.hypot((a.x - b.x) * width, (a.y - b.y) * height);
        if (d < linkDistance) {
          ctx.globalAlpha = (1 - d / linkDistance) * 0.5;
          ctx.beginPath();
          ctx.moveTo(a.x * width, a.y * height);
          ctx.lineTo(b.x * width, b.y * height);
          ctx.stroke();
        }
      }
    ctx.globalAlpha = 1;
    if (visible && !document.hidden && !reduced.matches) frame = requestAnimationFrame(paint);
  }
  function start() {
    if (!closed && !frame && visible && !document.hidden) {
      last = performance.now();
      frame = requestAnimationFrame(paint);
    }
  }
  function resize() {
    const r = container.getBoundingClientRect(),
      dpr = Math.min(devicePixelRatio || 1, 2);
    width = Math.max(1, r.width);
    height = Math.max(1, r.height);
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    start();
  }
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(container);
  const observer = new IntersectionObserver((entries) => {
    visible = entries[0].isIntersecting;
    if (!visible) {
      cancelAnimationFrame(frame);
      frame = 0;
    } else start();
  });
  observer.observe(container);
  const events = new AbortController();
  const listen = (target, type, fn) => target.addEventListener(type, fn, { signal: events.signal });
  listen(container, "pointermove", (event) => {
    const r = canvas.getBoundingClientRect();
    pointer.x = event.clientX - r.left;
    pointer.y = event.clientY - r.top;
  });
  listen(container, "pointerleave", () => {
    pointer.x = pointer.y = -10000;
  });
  listen(container, "click", (event) => {
    if (!interaction || reduced.matches) return;
    const r = canvas.getBoundingClientRect();
    for (const p of particles) {
      const dx = p.x * width - (event.clientX - r.left),
        dy = p.y * height - (event.clientY - r.top),
        d = Math.hypot(dx, dy) || 1;
      p.vx += (dx / d) * 0.1;
      p.vy += (dy / d) * 0.1;
    }
  });
  listen(document, "visibilitychange", () => {
    cancelAnimationFrame(frame);
    frame = 0;
    start();
  });
  listen(reduced, "change", () => {
    cancelAnimationFrame(frame);
    frame = 0;
    start();
  });
  resize();
  return {
    destroy() {
      closed = true;
      cancelAnimationFrame(frame);
      events.abort();
      observer.disconnect();
      resizeObserver.disconnect();
      canvas.remove();
      container.style.position = originalPosition;
    },
  };
}

export { mountParticles };
