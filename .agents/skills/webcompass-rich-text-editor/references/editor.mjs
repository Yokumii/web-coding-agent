function mountRichTextEditor({ container, initialHTML = "", output, onChange = () => {} }) {
  const tags = new Set([
    "P",
    "DIV",
    "BR",
    "STRONG",
    "EM",
    "U",
    "S",
    "H1",
    "H2",
    "H3",
    "UL",
    "OL",
    "LI",
    "BLOCKQUOTE",
    "A",
    "IMG",
  ]);
  const root = document.createElement("section"),
    toolbar = document.createElement("div"),
    editor = document.createElement("div"),
    feedback = document.createElement("p");
  root.className = "wc-editor";
  toolbar.className = "wc-editor__toolbar";
  toolbar.setAttribute("role", "toolbar");
  toolbar.setAttribute("aria-label", "Text formatting");
  editor.className = "wc-editor__content";
  editor.contentEditable = "true";
  editor.setAttribute("role", "textbox");
  editor.setAttribute("aria-label", "Document");
  editor.setAttribute("aria-multiline", "true");
  feedback.setAttribute("role", "alert");
  function safeURL(raw, image = false) {
    try {
      const url = new URL(raw, location.href);
      return (image ? ["https:", "http:"] : ["https:", "http:", "mailto:"]).includes(url.protocol)
        ? url.href
        : null;
    } catch {
      return null;
    }
  }
  function clean(html) {
    const parsed = new DOMParser().parseFromString(html, "text/html"),
      fragment = document.createDocumentFragment();
    function append(source, target) {
      for (const child of source.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          target.append(document.createTextNode(child.textContent));
          continue;
        }
        if (
          child.nodeType !== Node.ELEMENT_NODE ||
          ["SCRIPT", "STYLE", "IFRAME", "OBJECT", "TEMPLATE"].includes(child.tagName)
        )
          continue;
        if (!tags.has(child.tagName)) {
          append(child, target);
          continue;
        }
        const node = document.createElement(child.tagName.toLowerCase());
        if (child.tagName === "A") {
          const href = safeURL(child.getAttribute("href") || "");
          if (href) node.setAttribute("href", href);
        }
        if (child.tagName === "IMG") {
          const src = safeURL(child.getAttribute("src") || "", true);
          if (!src) continue;
          node.src = src;
          node.alt = child.getAttribute("alt") || "";
        }
        append(child, node);
        target.append(node);
      }
    }
    append(parsed.body, fragment);
    return fragment;
  }
  editor.append(clean(initialHTML || "<p><br></p>"));
  function html() {
    const box = document.createElement("div");
    box.append(clean(editor.innerHTML));
    return box.innerHTML;
  }
  function publish() {
    const value = html();
    if (output) output.value = value;
    onChange(value);
  }
  let savedRange = null;
  function remember() {
    const selection = getSelection();
    if (
      selection.rangeCount &&
      editor.contains(selection.anchorNode) &&
      editor.contains(selection.focusNode)
    )
      savedRange = selection.getRangeAt(0).cloneRange();
  }
  function range() {
    editor.focus();
    const selection = getSelection();
    const value =
      savedRange && editor.contains(savedRange.commonAncestorContainer)
        ? savedRange.cloneRange()
        : document.createRange();
    if (!savedRange || !editor.contains(value.commonAncestorContainer)) {
      value.selectNodeContents(editor);
      value.collapse(false);
    }
    selection.removeAllRanges();
    selection.addRange(value);
    return value;
  }
  function insert(node) {
    const selected = range();
    selected.deleteContents();
    const tail = node.nodeType === Node.DOCUMENT_FRAGMENT_NODE ? node.lastChild : node;
    if (!tail) return;
    selected.insertNode(node);
    selected.setStartAfter(tail);
    selected.collapse(true);
    getSelection().removeAllRanges();
    getSelection().addRange(selected);
    savedRange = selected.cloneRange();
    publish();
  }
  function inline(tag, attributes = {}) {
    const selected = range();
    if (selected.collapsed) {
      feedback.textContent = "Select text to format.";
      return false;
    }
    // Wrap text fragments inside their original blocks; never put paragraphs in inline tags.
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    const fragments = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!selected.intersectsNode(node)) continue;
      const start = node === selected.startContainer ? selected.startOffset : 0;
      const end = node === selected.endContainer ? selected.endOffset : node.length;
      if (end > start) fragments.push({ node, start, end });
    }
    const wrapped = [];
    for (const { node, start, end } of fragments) {
      const existingLink = tag === "a" ? node.parentElement.closest("a") : null;
      if (existingLink && editor.contains(existingLink)) {
        existingLink.setAttribute("href", attributes.href);
        wrapped.push(existingLink);
        continue;
      }
      const piece = document.createRange();
      piece.setStart(node, start);
      piece.setEnd(node, end);
      const element = document.createElement(tag);
      for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
      element.append(piece.extractContents());
      piece.insertNode(element);
      wrapped.push(element);
    }
    if (!wrapped.length) return false;
    const next = document.createRange();
    next.setStartBefore(wrapped[0]);
    next.setEndAfter(wrapped.at(-1));
    savedRange = next.cloneRange();
    getSelection().removeAllRanges();
    getSelection().addRange(next);
    publish();
    return wrapped;
  }
  function blocks(tag) {
    const selected = range();
    const candidates = [...editor.children].filter((node) => selected.intersectsNode(node));
    if (!candidates.length) {
      feedback.textContent = "Select a text block.";
      return;
    }
    const blockTags = new Set(["P", "DIV", "H1", "H2", "H3", "UL", "OL", "LI", "BLOCKQUOTE"]);
    const segments = [];
    function flatten(block) {
      let inlineNodes = [];
      const flush = () => {
        if (inlineNodes.length) {
          segments.push(inlineNodes);
          inlineNodes = [];
        }
      };
      for (const child of [...block.childNodes]) {
        if (child.nodeType === Node.ELEMENT_NODE && blockTags.has(child.tagName)) {
          flush();
          flatten(child);
        } else inlineNodes.push(child);
      }
      flush();
      if (!block.childNodes.length) segments.push([document.createElement("br")]);
    }
    candidates.forEach(flatten);
    const replacements = [];
    if (tag === "ul" || tag === "ol") {
      const list = document.createElement(tag);
      for (const nodes of segments) {
        const li = document.createElement("li");
        li.append(...nodes);
        list.append(li);
      }
      replacements.push(list);
    } else {
      for (const nodes of segments) {
        const block = document.createElement(tag);
        block.append(...nodes);
        replacements.push(block);
      }
    }
    if (!replacements.length) return;
    candidates[0].before(...replacements);
    candidates.forEach((node) => node.remove());
    const next = document.createRange();
    next.setStartBefore(replacements[0]);
    next.setEndAfter(replacements.at(-1));
    savedRange = next.cloneRange();
    getSelection().removeAllRanges();
    getSelection().addRange(next);
    publish();
  }
  function button(label, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.onmousedown = (e) => e.preventDefault();
    button.onclick = () => {
      feedback.textContent = "";
      action();
    };
    toolbar.append(button);
  }
  for (const [label, tag] of [
    ["Bold", "strong"],
    ["Italic", "em"],
    ["Underline", "u"],
    ["Strike", "s"],
  ])
    button(label, () => inline(tag));
  for (const [label, tag] of [
    ["Paragraph", "p"],
    ["Heading 1", "h1"],
    ["Heading 2", "h2"],
    ["Heading 3", "h3"],
    ["Bullet list", "ul"],
    ["Numbered list", "ol"],
    ["Quote", "blockquote"],
  ])
    button(label, () => blocks(tag));
  const urlForm = document.createElement("form"),
    urlInput = document.createElement("input"),
    labelInput = document.createElement("input"),
    apply = document.createElement("button");
  urlInput.type = "url";
  urlInput.required = true;
  urlInput.setAttribute("aria-label", "Link or image URL");
  labelInput.type = "text";
  labelInput.setAttribute("aria-label", "Link text (optional)");
  apply.type = "submit";
  apply.textContent = "Apply";
  urlForm.append(urlInput, labelInput, apply);
  urlForm.hidden = true;
  let imageMode = false;
  for (const [label, mode] of [
    ["Link", false],
    ["Image", true],
  ])
    button(label, () => {
      remember();
      imageMode = mode;
      labelInput.hidden = mode;
      labelInput.value = "";
      urlForm.hidden = false;
      urlInput.value = "";
      urlInput.focus();
    });
  urlForm.onsubmit = (e) => {
    e.preventDefault();
    const url = safeURL(urlInput.value, imageMode);
    if (!url) {
      feedback.textContent = "Use a safe HTTP(S) URL.";
      return;
    }
    if (imageMode) {
      const img = document.createElement("img");
      img.src = url;
      img.alt = "";
      img.addEventListener("error", () => {
        img.alt = "Image unavailable";
        img.classList.add("wc-editor__image-error");
        feedback.textContent = "Image could not be loaded. Check its URL.";
      }, { once: true });
      insert(img);
    } else {
      const selected = range();
      if (selected.collapsed) {
        feedback.textContent = "Select link text first.";
        return;
      }
      const links = inline("a", { href: url });
      if (!links) return;
      const label = labelInput.value.trim();
      if (label) {
        const uniqueLinks = [...new Set(links)];
        // Rename inside the existing formatting wrappers, rather than deleting
        // the whole selection (which could include <strong>, <em>, or blocks).
        uniqueLinks[0].textContent = label;
        uniqueLinks.slice(1).forEach(link => link.remove());
        savedRange = document.createRange();
        savedRange.selectNodeContents(uniqueLinks[0]);
        getSelection().removeAllRanges();
        getSelection().addRange(savedRange.cloneRange());
        publish();
      }
    }
    urlForm.hidden = true;
  };
  const events = new AbortController();
  document.addEventListener("selectionchange", remember, { signal: events.signal });
  editor.addEventListener("input", publish, { signal: events.signal });
  editor.addEventListener(
    "paste",
    (event) => {
      event.preventDefault();
      remember();
      const content = event.clipboardData.getData("text/html");
      insert(
        content
          ? clean(content)
          : document.createTextNode(event.clipboardData.getData("text/plain")),
      );
    },
    { signal: events.signal },
  );
  editor.addEventListener(
    "drop",
    (event) => {
      event.preventDefault();
    },
    { signal: events.signal },
  );
  root.append(toolbar, urlForm, editor, feedback);
  container.append(root);
  publish();
  return {
    root, toolbar, editor, urlForm, urlInput, labelInput, apply, feedback,
    getHTML: html,
    setHTML(value) {
      savedRange = null;
      editor.replaceChildren(clean(value || "<p><br></p>"));
      publish();
    },
    destroy() {
      events.abort();
      root.remove();
    },
  };
}

export { mountRichTextEditor };
