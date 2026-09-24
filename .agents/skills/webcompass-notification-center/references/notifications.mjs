function mountNotificationCenter({
  container,
  notifications = [],
  allowDelete = false,
  onAction = () => {},
  onChange = () => {},
  onCreateRow = () => {},
}) {
  let entries = [];
  const root = document.createElement("section");
  root.className = "wc-notifications";
  const title = document.createElement("h2"),
    count = document.createElement("output"),
    list = document.createElement("ul"),
    all = document.createElement("button");
  title.textContent = "Notifications";
  count.setAttribute("aria-live", "polite");
  all.type = "button";
  all.textContent = "Mark all as read";
  root.append(title, count, all, list);
  container.append(root);
  function normalize(item) {
    if (typeof item.id !== "string" || !item.id || typeof item.message !== "string")
      throw new Error("Notification requires id and message");
    return {
      ...item,
      id: item.id,
      message: item.message,
      read: Boolean(item.read),
      actionLabel: item.actionLabel || null,
    };
  }
  function render() {
    const unread = entries.filter((n) => !n.read).length;
    count.textContent = `${unread} unread`;
    all.disabled = !unread;
    list.replaceChildren();
    for (const entry of entries) {
      const row = document.createElement("li");
      row.dataset.notificationId = entry.id;
      row.dataset.read = String(entry.read);
      const text = document.createElement("span"),
        mark = document.createElement("button");
      text.textContent = entry.message;
      mark.type = "button";
      mark.textContent = entry.read ? "Read" : "Mark as read";
      mark.disabled = entry.read;
      mark.onclick = () => {
        entry.read = true;
        render();
      };
      row.append(text, mark);
      if (entry.actionLabel) {
        const action = document.createElement("button");
        action.type = "button";
        action.textContent = entry.actionLabel;
        action.onclick = () => {
          onAction({ ...entry });
          entry.read = true;
          render();
        };
        row.append(action);
      }
      if (allowDelete) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Delete";
        remove.onclick = () => {
          entries = entries.filter((n) => n.id !== entry.id);
          render();
        };
        row.append(remove);
      }
      list.append(row);
      onCreateRow(row, { ...entry });
    }
    if (!entries.length) {
      const empty = document.createElement("li");
      empty.textContent = "No notifications";
      list.append(empty);
    }
    onChange(entries.map((entry) => ({ ...entry })), unread);
  }
  function push(item) {
    const entry = normalize(item);
    if (entries.some((n) => n.id === entry.id)) throw new Error("Duplicate notification ID");
    entries.unshift(entry);
    render();
  }
  entries = notifications.map(normalize);
  if (new Set(entries.map((n) => n.id)).size !== entries.length) {
    root.remove();
    throw new Error("Duplicate notification ID");
  }
  all.onclick = () => {
    entries.forEach((n) => (n.read = true));
    render();
  };
  render();
  return { root, count, list, all, push,
    snapshot: () => entries.map((n) => ({ ...n })), destroy: () => root.remove() };
}

export { mountNotificationCenter };
