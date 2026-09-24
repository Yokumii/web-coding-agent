function createXHRUpload(url) {
  return (file, { signal, onProgress }) =>
    new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      let settled = false;
      const abort = () => xhr.abort();
      function finish(error, result) {
        if (settled) return;
        settled = true;
        signal.removeEventListener("abort", abort);
        error ? reject(error) : resolve(result);
      }
      xhr.open("POST", url);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded / e.total);
      };
      xhr.onload = () =>
        xhr.status >= 200 && xhr.status < 300
          ? finish(null, xhr.responseText)
          : finish(new Error(`HTTP ${xhr.status}`));
      xhr.onerror = () => finish(new Error("Network error"));
      xhr.onabort = () =>
        finish(new DOMException("Upload cancelled", "AbortError"));
      if (signal.aborted) {
        finish(new DOMException("Upload cancelled", "AbortError"));
        return;
      }
      signal.addEventListener("abort", abort, { once: true });
      const data = new FormData();
      data.append("file", file);
      xhr.send(data);
    });
}
// Opt in only when the requested feature explicitly uses simulated transport.
// A JSON document with {"simulateFailure": true} fails once; retry then succeeds.
function createSimulatedUpload({ durationMs = 1600, tickMs = 80 } = {}) {
  if (!(durationMs > 0) || !(tickMs > 0)) throw new Error("Use positive upload timing");
  const failed = new WeakSet();
  return (file, { signal, onProgress }) => new Promise((resolve, reject) => {
    let timer, settled = false;
    const abort = () => finish(new DOMException("Upload cancelled", "AbortError"));
    function finish(error) {
      if (settled) return;
      settled = true;
      clearInterval(timer);
      signal.removeEventListener("abort", abort);
      error ? reject(error) : resolve({ simulated: true, name: file.name });
    }
    if (signal.aborted) { abort(); return; }
    signal.addEventListener("abort", abort, { once: true });
    (async () => {
      let failOnce = false;
      if (file.type === "application/json" || /\.json$/i.test(file.name)) {
        try { failOnce = JSON.parse(await file.text()).simulateFailure === true; }
        catch { /* Ordinary non-JSON documents upload normally. */ }
      }
      if (settled) return;
      const start = performance.now();
      onProgress(0);
      timer = setInterval(() => {
        const fraction = Math.min(1, (performance.now() - start) / durationMs);
        onProgress(fraction);
        if (failOnce && !failed.has(file) && fraction >= .6) {
          failed.add(file);
          finish(new Error("Simulated temporary upload failure; retry is available."));
        } else if (fraction >= 1) finish();
      }, tickMs);
    })().catch(finish);
  });
}

function mountFileUpload({
  container,
  upload,
  accept = "",
  maxBytes = Infinity,
  concurrency = 2,
  deduplicate = false,
  removable = false,
  preview = false,
  onCreate = () => {},
  onUpdate = () => {},
}) {
  if (
    typeof upload !== "function" ||
    !Number.isInteger(concurrency) ||
    concurrency < 1
  )
    throw new Error("Supply an upload transport and positive concurrency");
  const root = document.createElement("section");
  root.className = "wc-upload";
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = true;
  input.accept = accept;
  input.setAttribute("aria-label", "Choose files");
  const drop = document.createElement("div");
  drop.textContent = "Drop files here";
  drop.className = "wc-upload__drop";
  drop.tabIndex = 0;
  drop.setAttribute("role", "button");
  drop.setAttribute("aria-label", "Drop files or browse");
  drop.onclick = () => input.click();
  drop.onkeydown = (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      input.click();
    }
  };
  const list = document.createElement("ul"),
    error = document.createElement("p");
  error.setAttribute("role", "alert");
  root.append(input, drop, error, list);
  container.append(root);
  const jobs = [];
  let active = 0,
    closed = false;
  function draw(job) {
    job.row.dataset.state = job.state;
    job.progress.value = job.fraction;
    job.progress.setAttribute(
      "aria-valuenow",
      String(Math.round(job.fraction * 100)),
    );
    job.progress.setAttribute("aria-valuemin", "0");
    job.progress.setAttribute("aria-valuemax", "100");
    job.status.textContent =
      job.state === "failed"
        ? `Failed: ${job.errorMessage || "Upload failed"}`
        : job.state === "complete"
          ? "completed"
          : job.state;
    job.percentage.textContent = `${Math.round(job.fraction * 100)}%`;
    job.cancel.disabled = !["queued", "uploading"].includes(job.state);
    job.retry.hidden = job.state !== "failed";
    job.remove.disabled = job.state === "uploading";
    onUpdate(job);
  }
  function accepts(file) {
    return (
      !accept ||
      accept.split(",").some((raw) => {
        const rule = raw.trim().toLowerCase();
        return rule.startsWith(".")
          ? file.name.toLowerCase().endsWith(rule)
          : rule.endsWith("/*")
            ? file.type.toLowerCase().startsWith(rule.slice(0, -1))
            : file.type.toLowerCase() === rule;
      })
    );
  }
  function pump() {
    if (closed) return;
    for (const job of jobs) {
      if (active >= concurrency) break;
      if (job.state !== "queued") continue;
      active++;
      job.state = "uploading";
      job.controller = new AbortController();
      draw(job);
      Promise.resolve()
        .then(() =>
          upload(job.file, {
            signal: job.controller.signal,
            onProgress(value) {
              if (
                !closed &&
                job.state === "uploading" &&
                Number.isFinite(value)
              ) {
                job.fraction = Math.max(
                  job.fraction,
                  Math.min(0.99, Math.max(0, value)),
                );
                draw(job);
              }
            },
          }),
        )
        .then((result) => {
          if (!closed && job.state === "uploading") {
            job.result = result;
            job.fraction = 1;
            job.state = "complete";
            draw(job);
          }
        })
        .catch((cause) => {
          if (!closed && job.state === "uploading") {
            job.state = "failed";
            job.errorMessage = cause.message;
            draw(job);
          }
        })
        .finally(() => {
          active--;
          pump();
        });
    }
  }
  function add(files) {
    error.textContent = "";
    delete error.dataset.errorKind;
    for (const file of files) {
      if (file.size > maxBytes) {
        error.dataset.errorKind = "size";
        error.textContent = `Rejected ${file.name}: file exceeds ${maxBytes} bytes`;
        continue;
      }
      if (!accepts(file)) {
        error.dataset.errorKind = "type";
        error.textContent = `Rejected ${file.name}: unsupported type`;
        continue;
      }
      if (
        deduplicate &&
        jobs.some(
          (job) =>
            job.file.name === file.name &&
            job.file.size === file.size &&
            job.file.type === file.type,
        )
      ) {
        error.dataset.errorKind = "duplicate";
        error.textContent = `Already queued: ${file.name}`;
        continue;
      }
      const row = document.createElement("li"),
        name = document.createElement("span"),
        progress = document.createElement("progress"),
        status = document.createElement("span"),
        percentage = document.createElement("span"),
        cancel = document.createElement("button"),
        retry = document.createElement("button"),
        remove = document.createElement("button");
      name.textContent = file.name;
      progress.max = 1;
      progress.setAttribute("aria-label", `${file.name} progress`);
      status.setAttribute("role", "status");
      cancel.type = retry.type = remove.type = "button";
      cancel.textContent = "Cancel";
      retry.textContent = "Retry";
      remove.textContent = "Remove";
      remove.hidden = !removable;
      const job = {
        file,
        row,
        name,
        state: "queued",
        fraction: 0,
        progress,
        status,
        percentage,
        cancel,
        retry,
        remove,
      };
      jobs.push(job);
      cancel.onclick = () => {
        if (!["queued", "uploading"].includes(job.state)) return;
        job.state = "cancelled";
        job.controller?.abort();
        draw(job);
        pump();
      };
      retry.onclick = () => {
        if (job.state !== "failed") return;
        job.state = "queued";
        job.fraction = 0;
        draw(job);
        pump();
      };
      remove.onclick = () => {
        if (!removable || job.state === "uploading") return;
        const index = jobs.indexOf(job);
        if (index < 0) return;
        jobs.splice(index, 1);
        if (job.previewUrl) URL.revokeObjectURL(job.previewUrl);
        row.remove();
      };
      if (preview) {
        const visual = document.createElement(
          file.type.startsWith("image/") ? "img" : "span",
        );
        visual.className = "wc-upload__preview";
        if (visual.tagName === "IMG") {
          job.previewUrl = URL.createObjectURL(file);
          visual.src = job.previewUrl;
          visual.alt = file.name;
        } else {
          visual.textContent = "📄";
          visual.setAttribute("aria-label", "Document");
        }
        const size = document.createElement("small");
        size.textContent =
          file.size < 1024
            ? `${file.size} B`
            : file.size < 1048576
              ? `${(file.size / 1024).toFixed(1)} KB`
              : `${(file.size / 1048576).toFixed(1)} MB`;
        job.visual = visual;
        job.size = size;
        row.append(visual, size);
      }
      row.append(name, progress, status, percentage, cancel, retry, remove);
      row.dataset.fileName = file.name;
      cancel.dataset.action = "cancel";
      retry.dataset.action = "retry";
      remove.dataset.action = "remove";
      list.append(row);
      onCreate(job);
      draw(job);
    }
    pump();
  }
  input.onchange = () => {
    add([...input.files]);
    input.value = "";
  };
  drop.ondragover = (e) => {
    e.preventDefault();
    drop.classList.add("wc-upload__active");
  };
  drop.ondragleave = () => drop.classList.remove("wc-upload__active");
  drop.ondrop = (e) => {
    e.preventDefault();
    drop.classList.remove("wc-upload__active");
    add([...e.dataTransfer.files]);
  };
  return {
    root,
    input,
    drop,
    list,
    error,
    add,
    snapshot: () =>
      jobs.map((j) => ({
        name: j.file.name,
        state: j.state,
        progress: j.fraction,
      })),
    destroy() {
      closed = true;
      jobs.forEach((j) => j.controller?.abort());
      jobs.forEach((j) => {
        if (j.previewUrl) URL.revokeObjectURL(j.previewUrl);
      });
      root.remove();
    },
  };
}
