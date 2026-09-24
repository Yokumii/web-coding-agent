const asyncFormGroups = new WeakMap();
let asyncValidationId = 0;
function bindAsyncValidation({
  form,
  input,
  submitButton,
  check,
  debounceMs = 300,
  canSubmit = () => true,
}) {
  if (input.form !== form) throw new Error("Input must belong to the form");
  let group = asyncFormGroups.get(form);
  if (!group) {
    group = { members: new Set(), originalDisabled: submitButton.disabled, button: submitButton };
    asyncFormGroups.set(form, group);
  }
  if (group.button !== submitButton) throw new Error("Use the same submit button for one form");
  const member = { status: "idle", canSubmit };
  group.members.add(member);
  let version = 0,
    timer,
    controller,
    closed = false;
  const oldDescribed = input.getAttribute("aria-describedby"),
    oldBusy = input.getAttribute("aria-busy"),
    oldValidity = input.validity.customError ? input.validationMessage : "";
  const status = document.createElement("span");
  status.id = `wc-validation-${++asyncValidationId}`;
  status.setAttribute("role", "status");
  input.after(status);
  input.setAttribute("aria-describedby", [oldDescribed, status.id].filter(Boolean).join(" "));
  function sync() {
    group.button.disabled =
      [...group.members].some((m) => m.status !== "valid" || !m.canSubmit()) ||
      !form.checkValidity();
  }
  function setState(value, message = "") {
    member.status = value;
    status.textContent = message;
    input.setAttribute("aria-busy", String(value === "pending"));
    sync();
  }
  async function run(expected) {
    if (closed || expected !== version) return;
    controller = new AbortController();
    setState("pending", "Checking…");
    try {
      const result = await check(input.value, { signal: controller.signal });
      if (closed || expected !== version) return;
      if (!result || typeof result.valid !== "boolean")
        throw new Error("check must return {valid, message}");
      input.setCustomValidity(result.valid ? "" : result.message || "Invalid value");
      setState(
        result.valid ? "valid" : "invalid",
        result.message || (result.valid ? "Available" : "Invalid value"),
      );
    } catch (error) {
      if (closed || expected !== version) return;
      input.setCustomValidity("Validation unavailable");
      setState("invalid", `Validation failed: ${error.message}`);
    }
  }
  function changed() {
    version++;
    clearTimeout(timer);
    controller?.abort();
    input.setCustomValidity("");
    if (!input.checkValidity()) {
      setState("invalid", input.validationMessage);
      return;
    }
    setState("pending", "Waiting to check…");
    const expected = version;
    timer = setTimeout(() => run(expected), debounceMs);
  }
  const events = new AbortController();
  input.addEventListener("input", changed, { signal: events.signal });
  form.addEventListener("input", sync, { signal: events.signal });
  form.addEventListener(
    "submit",
    (event) => {
      sync();
      if (group.button.disabled) event.preventDefault();
    },
    { signal: events.signal },
  );
  changed();
  return {
    validate: changed,
    snapshot: () => ({ status: member.status, requestVersion: version }),
    destroy() {
      closed = true;
      version++;
      clearTimeout(timer);
      controller?.abort();
      events.abort();
      status.remove();
      group.members.delete(member);
      input.setCustomValidity(oldValidity);
      oldDescribed === null
        ? input.removeAttribute("aria-describedby")
        : input.setAttribute("aria-describedby", oldDescribed);
      oldBusy === null
        ? input.removeAttribute("aria-busy")
        : input.setAttribute("aria-busy", oldBusy);
      if (!group.members.size) {
        submitButton.disabled = group.originalDisabled;
        asyncFormGroups.delete(form);
      } else sync();
    },
  };
}
