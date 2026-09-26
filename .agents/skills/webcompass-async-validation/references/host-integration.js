// Host-first integration and representative composition for WebCompass Edit.
function wireAsyncValidation(host) {
  const fields = host.fields || [host.input];
  return fields.map(input => bindAsyncValidation({form: host.form, input, submitButton: host.submitButton, check: (value, ctx) => host.check(input, value, ctx), debounceMs: host.debounceMs || 500, canSubmit: () => host.canSubmit ? host.canSubmit(host.form) : true}));
}
