// Minimal progressive enhancement: confirmations and copy-to-clipboard.
document.addEventListener("submit", (ev) => {
  const form = ev.target;
  const tpl = form.dataset.actionTemplate;
  const idSel = form.querySelector("[data-action-id]");
  if (tpl && idSel) form.action = tpl.replace("{id}", encodeURIComponent(idSel.value));
  const submitter = ev.submitter;
  const msg = (submitter && submitter.dataset.confirm) || form.dataset.confirm;
  if (msg && !window.confirm(msg)) { ev.preventDefault(); return; }
  // Loading state: one submission per click (a double click must not post twice, e.g. "Create Gmail draft").
  // GET forms (search/filters) are left alone.
  if ((form.method || "get").toLowerCase() === "post" && submitter && submitter.tagName === "BUTTON") {
    submitter.setAttribute("aria-busy", "true");
    if (submitter.dataset.busyLabel) { submitter.dataset.idleLabel = submitter.textContent; submitter.textContent = submitter.dataset.busyLabel; }
    setTimeout(() => { submitter.disabled = true; }, 0);  // after the browser has captured the form data
  }
});

// Back/forward cache: never show a button stuck in its busy state.
window.addEventListener("pageshow", () => {
  document.querySelectorAll('button[aria-busy="true"]').forEach((b) => {
    b.disabled = false; b.removeAttribute("aria-busy");
    if (b.dataset.idleLabel) b.textContent = b.dataset.idleLabel;
  });
});

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-copy]");
  if (!btn) return;
  const src = document.getElementById(btn.dataset.copy);
  if (!src) return;
  const text = src.value !== undefined ? src.value : src.innerText;
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.className = "copy-src"; document.body.appendChild(ta);
    ta.select(); document.execCommand("copy"); ta.remove();
  }
  const old = btn.textContent; btn.textContent = "Copied"; setTimeout(() => (btn.textContent = old), 1200);
});

document.addEventListener("click", (ev) => {
  const back = ev.target.closest("[data-back]");
  if (back && window.history.length > 1) { ev.preventDefault(); window.history.back(); }
});

// Search: "/" jumps to the search box; changing a filter dropdown applies it straight away.
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "/" || ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const t = ev.target;
  if (t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
  const box = document.getElementById("job-q");
  if (box) { ev.preventDefault(); box.focus(); box.select(); }
});
document.addEventListener("change", (ev) => {
  const form = ev.target.form;
  if (form && form.hasAttribute("data-autosubmit") && ev.target.tagName === "SELECT") form.requestSubmit();
});

// Theme toggle (topbar): dark by default, light on request; remembered in this browser only.
document.addEventListener("click", (ev) => {
  if (!ev.target.closest("[data-theme-toggle]")) return;
  const root = document.documentElement;
  const next = root.dataset.theme === "light" ? "dark" : "light";
  root.dataset.theme = next;
  try { localStorage.setItem("jobpilot-theme", next); } catch (e) { /* storage blocked: applies to this page only */ }
});
