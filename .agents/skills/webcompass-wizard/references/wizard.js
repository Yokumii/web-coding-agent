function mountWizard({ form, steps, onComplete }) {
  if (!steps.length || steps.some((s) => !form.contains(s.element)))
    throw new Error("Steps must belong to the existing form");
  let currentStep = 0,
    pending = false,
    closed = false,
    completed = false;
  const oldNoValidate = form.noValidate;
  form.noValidate = true;
  const original = steps.map((s) => ({ hidden: s.element.hidden, inert: s.element.inert }));
  const navigation = document.createElement("nav"),
    progress = document.createElement("p"),
    error = document.createElement("p");
  navigation.setAttribute("aria-label", "Form steps");
  progress.setAttribute("role", "status");
  error.setAttribute("role", "alert");
  const back = document.createElement("button"),
    next = document.createElement("button");
  back.type = next.type = "button";
  back.textContent = "Back";
  navigation.append(back, next);
  form.append(progress, error, navigation);
  function controls(step) {
    return [...step.element.querySelectorAll("input,select,textarea")];
  }
  function valid() {
    return controls(steps[currentStep]).every((el) => el.disabled || el.checkValidity());
  }
  function data() {
    const result = {};
    for (const step of steps)
      for (const el of controls(step)) {
        if (!el.name || el.disabled || (["checkbox", "radio"].includes(el.type) && !el.checked))
          continue;
        const values =
          el.tagName === "SELECT" && el.multiple
            ? [...el.selectedOptions].map((o) => o.value)
            : [el.value];
        for (const value of values) {
          if (Object.hasOwn(result, el.name)) result[el.name] = [].concat(result[el.name], value);
          else result[el.name] = value;
        }
      }
    return result;
  }
  function render() {
    steps.forEach((step, i) => {
      step.element.hidden = i !== currentStep;
      step.element.inert = i !== currentStep;
    });
    progress.textContent = `Step ${currentStep + 1} of ${steps.length}: ${steps[currentStep].label}`;
    back.disabled = currentStep === 0 || pending;
    next.disabled = pending || !valid();
    next.textContent = pending
      ? "Submitting…"
      : currentStep === steps.length - 1
        ? "Finish"
        : "Next";
  }
  async function advance() {
    if (pending || closed || completed || !valid()) return;
    error.textContent = "";
    if (currentStep < steps.length - 1) {
      currentStep++;
      render();
      controls(steps[currentStep])
        .find((el) => !el.disabled)
        ?.focus();
      return;
    }
    const invalidStep = steps.findIndex((step) =>
      controls(step).some((el) => !el.disabled && !el.checkValidity()),
    );
    if (invalidStep >= 0) {
      currentStep = invalidStep;
      render();
      controls(steps[currentStep])
        .find((el) => !el.disabled && !el.checkValidity())
        ?.focus();
      return;
    }
    pending = true;
    render();
    try {
      await onComplete(data());
      if (!closed) {
        pending = false;
        completed = true;
        progress.textContent = "Completed";
        next.disabled = true;
        back.disabled = true;
      }
    } catch (cause) {
      if (!closed) {
        error.textContent = `Submission failed: ${cause.message}`;
        pending = false;
        render();
      }
    }
  }
  back.onclick = () => {
    if (currentStep > 0 && !pending) {
      currentStep--;
      error.textContent = "";
      render();
    }
  };
  next.onclick = advance;
  const events = new AbortController();
  form.addEventListener(
    "input",
    () => {
      if (!pending && !completed) render();
    },
    { signal: events.signal },
  );
  form.addEventListener(
    "submit",
    (event) => {
      event.preventDefault();
      advance();
    },
    { signal: events.signal },
  );
  render();
  return {
    snapshot: () => ({ currentStep, formData: data(), pending, completed }),
    destroy() {
      closed = true;
      form.noValidate = oldNoValidate;
      events.abort();
      navigation.remove();
      progress.remove();
      error.remove();
      steps.forEach((s, i) => {
        s.element.hidden = original[i].hidden;
        s.element.inert = original[i].inert;
      });
    },
  };
}
