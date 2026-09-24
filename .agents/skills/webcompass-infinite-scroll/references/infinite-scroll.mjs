function mountInfiniteScroll({
  container,
  loadPage,
  getId,
  renderItem,
  root = null,
  firstPage = 1,
  initialLoad = false,
  endText = 'End of content',
  renderLoading = null,
}) {
  let nextPage = firstPage,
    loading = false,
    hasMore = true,
    closed = false,
    controller;
  const ids = new Set(),
    items = [],
    owned = [];
  const sentinel = document.createElement("div"),
    status = document.createElement("p"),
    retry = document.createElement("button");
  status.setAttribute("role", "status");
  retry.type = "button";
  retry.textContent = "Retry";
  retry.hidden = true;
  sentinel.className = "wc-scroll-sentinel";
  sentinel.append(status, retry);
  container.append(sentinel);
  async function load() {
    if (closed || loading || !hasMore) return;
    loading = true;
    retry.hidden = true;
    status.textContent = "Loading…";
    if (renderLoading) status.replaceChildren(renderLoading());
    controller = new AbortController();
    try {
      const result = await loadPage(nextPage, { signal: controller.signal });
      if (closed) return;
      if (!Array.isArray(result.items) || typeof result.hasMore !== "boolean")
        throw new Error("Page must return items and hasMore");
      const fresh = [],
        batchIds = new Set(ids);
      for (const item of result.items) {
        const id = getId(item);
        if (id == null) throw new Error("Missing item ID");
        if (!batchIds.has(id)) {
          batchIds.add(id);
          fresh.push(item);
        }
      }
      if (!fresh.length && result.hasMore) throw new Error("Page made no progress");
      const nodes = fresh.map((item) => renderItem(item));
      if (nodes.some((node) => !(node instanceof Element)) || new Set(nodes).size !== nodes.length)
        throw new Error("renderItem must return a fresh element per item");
      nodes.forEach((node, i) => {
        ids.add(getId(fresh[i]));
        items.push(fresh[i]);
        owned.push(node);
        sentinel.before(node);
      });
      nextPage++;
      hasMore = result.hasMore;
      status.textContent = hasMore ? "" : endText;
      if (!hasMore) observer.disconnect();
    } catch (error) {
      if (!closed) {
        status.textContent = `Loading failed: ${error.message}`;
        retry.hidden = false;
      }
    } finally {
      loading = false;
      if (!closed && hasMore && retry.hidden) {
        const box = sentinel.getBoundingClientRect(),
          boundary = root ? root.getBoundingClientRect() : { top: 0, bottom: innerHeight };
        if (box.top < boundary.bottom + 100 && box.bottom > boundary.top - 100)
          queueMicrotask(load);
      }
    }
  }
  const observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting) && retry.hidden) load();
    },
    { root, rootMargin: "100px" },
  );
  retry.onclick = load;
  observer.observe(sentinel);
  if (initialLoad) queueMicrotask(load);
  return {
    load,
    snapshot: () => ({ items: [...items], loading, hasMore, nextPage }),
    destroy() {
      closed = true;
      controller?.abort();
      observer.disconnect();
      sentinel.remove();
      owned.forEach((node) => node.remove());
    },
  };
}

export { mountInfiniteScroll };
