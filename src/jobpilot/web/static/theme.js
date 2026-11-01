// Applies the saved theme before first paint. Dark ("Midnight") is the default; light is opt-in.
try {
  var saved = localStorage.getItem("jobpilot-theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
} catch (e) { /* storage blocked: keep the default */ }
