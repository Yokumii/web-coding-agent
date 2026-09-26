// Host-first integration and representative composition for WebCompass Edit.
function wireRichTextEditor(host) {
  return mountRichTextEditor({container: host.container, initialHTML: host.state.document.body, output: host.output, onChange: html => host.updateDocument({body: html})});
}
