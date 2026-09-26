// Host-first integration and representative composition for WebCompass Edit.
function wireFileUpload(host) {
  return mountFileUpload({container: host.container, upload: host.upload || createXHRUpload(host.endpoint), accept: host.accept, maxBytes: host.maxBytes, concurrency: host.concurrency || 2, deduplicate: !!host.deduplicate, removable: !!host.removable, preview: !!host.preview, onCreate: job => host.onCreate?.(job), onUpdate: job => host.onUpdate?.(job)});
}
