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
    setTimeout(() => { submitter.disabled = true; }, 0);  // after the browser has captured the form data
  }
});

// Back/forward cache: never show a button stuck in its busy state.
window.addEventListener("pageshow", () => {
  document.querySelectorAll('button[aria-busy="true"]').forEach((b) => { b.disabled = false; b.removeAttribute("aria-busy"); });
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
