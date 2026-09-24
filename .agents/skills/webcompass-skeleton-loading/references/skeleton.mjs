function mountSkeletonLoading({ container, skeleton, load, renderContent, duration = 180 }) {
  if (container.childNodes.length) throw new Error("Use an empty loading mount");
  const placeholder = skeleton.cloneNode(true),
    error = document.createElement("p"),
    retry = document.createElement("button");
  placeholder.classList.add("wc-skeleton");
  placeholder.setAttribute("aria-hidden", "true");
  error.setAttribute("role", "alert");
  retry.type = "button";
  retry.textContent = "Retry";
  retry.hidden = true;
  const oldBusy = container.getAttribute("aria-busy");
  let closed = false,
    version = 0,
    controller,
    content,
    animation,
    pending = false;
  container.append(placeholder, error, retry);
  async function run() {
    if (closed || pending) return;
    pending = true;
    const current = ++version;
    controller = new AbortController();
    placeholder.hidden = false;
    error.textContent = "";
    retry.hidden = true;
    container.setAttribute("aria-busy", "true");
    try {
      const data = await load({ signal: controller.signal });
      if (closed || current !== version) return;
      const next = renderContent(data);
      if (!(next instanceof Element)) throw new Error("renderContent must return an Element");
      content?.remove();
      content = next;
      placeholder.replaceWith(content);
      container.setAttribute("aria-busy", "false");
      animation = content.animate([{ opacity: 0 }, { opacity: 1 }], {
        duration: matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : duration,
      });
    } catch (cause) {
      if (!closed && current === version) {
        placeholder.hidden = true;
        container.setAttribute("aria-busy", "false");
        error.textContent = `Could not load: ${cause.message}`;
        retry.hidden = false;
      }
    } finally {
      if (current === version) pending = false;
    }
  }
  retry.onclick = run;
  run();
  return {
    destroy() {
      closed = true;
      version++;
      controller?.abort();
      animation?.cancel();
      placeholder.remove();
      content?.remove();
      error.remove();
      retry.remove();
      oldBusy === null
        ? container.removeAttribute("aria-busy")
        : container.setAttribute("aria-busy", oldBusy);
    },
  };
}

export { mountSkeletonLoading };
