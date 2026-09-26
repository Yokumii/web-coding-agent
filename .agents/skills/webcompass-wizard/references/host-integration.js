// Host-first integration and representative composition for WebCompass Edit.
function wireWizard(host) {
  return mountWizard({form: host.form, steps: host.steps, onComplete: data => host.submit(data)});
}
