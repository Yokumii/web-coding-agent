function mountAuthentication({
  container,
  authenticate,
  restoreSession,
  signOut = async () => {},
  onSession = () => {},
  allowRegistration = false,
}) {
  if (typeof authenticate !== "function") throw new Error("Supply the host authentication adapter");
  let user = null,
    mode = "login",
    pending = false,
    closed = false,
    version = 0,
    controller;
  const root = document.createElement("section"),
    identity = document.createElement("p"),
    form = document.createElement("form"),
    status = document.createElement("p");
  root.className = "wc-auth";
  status.setAttribute("role", "status");
  function field(label, type, name) {
    const wrap = document.createElement("label"),
      input = document.createElement("input");
    wrap.textContent = label;
    input.type = type;
    input.name = name;
    input.required = true;
    wrap.append(input);
    form.append(wrap);
    return input;
  }
  const company = field("Company", "text", "company"),
    email = field("Email", "email", "email"),
    password = field("Password", "password", "password");
  password.autocomplete = "current-password";
  email.autocomplete = "email";
  const submit = document.createElement("button"),
    toggle = document.createElement("button"),
    logout = document.createElement("button");
  submit.type = "submit";
  toggle.type = logout.type = "button";
  logout.textContent = "Log out";
  form.append(submit, toggle);
  root.append(identity, form, logout, status);
  container.append(root);
  function render() {
    identity.textContent = user ? `Signed in as ${user.name || user.email}` : "Not signed in";
    form.hidden = Boolean(user);
    logout.hidden = !user;
    logout.disabled = pending;
    submit.disabled = pending;
    toggle.disabled = pending;
    submit.textContent = pending ? "Please wait…" : mode === "login" ? "Log in" : "Register";
    toggle.hidden = !allowRegistration;
    toggle.textContent = mode === "login" ? "Create account" : "Use existing account";
    company.parentElement.hidden = mode === "login";
    company.disabled = mode === "login";
    password.autocomplete = mode === "login" ? "current-password" : "new-password";
  }
  function commit(value) {
    if (value !== null && (!value || typeof value !== "object" || (!value.name && !value.email)))
      throw new Error("Authentication must return a user identity or null");
    user = value ? { name: value.name || "", email: value.email || "" } : null;
    render();
    onSession(user && { ...user });
  }
  async function request(action) {
    if (pending || closed) return;
    pending = true;
    const current = ++version;
    controller = new AbortController();
    status.textContent = "";
    render();
    try {
      const value = await action(controller.signal);
      if (!closed && current === version) commit(value);
    } catch (error) {
      if (!closed && current === version)
        status.textContent = `Authentication failed: ${error.message}`;
    } finally {
      if (!closed && current === version) {
        pending = false;
        password.value = "";
        render();
      }
    }
  }
  form.onsubmit = (event) => {
    event.preventDefault();
    if (!form.checkValidity()) {
      form.reportValidity();
      return;
    }
    const credentials = {
      email: email.value,
      password: password.value,
      ...(mode === "register" ? { company: company.value } : {}),
    };
    request((signal) => authenticate(credentials, { mode, signal }));
  };
  toggle.onclick = () => {
    if (pending) return;
    mode = mode === "login" ? "register" : "login";
    status.textContent = "";
    render();
  };
  logout.onclick = () =>
    request(async (signal) => {
      await signOut({ signal });
      return null;
    });
  render();
  if (restoreSession) request((signal) => restoreSession({ signal }));
  return {
    snapshot: () => ({ user: user && { ...user }, pending, mode }),
    destroy() {
      closed = true;
      version++;
      controller?.abort();
      root.remove();
    },
  };
}
