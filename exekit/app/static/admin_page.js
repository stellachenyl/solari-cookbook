/* ExecKit Admin — page wiring: events, tabs, init.
   Requires admin.js (defines the `admin` object and globals). */
"use strict";


els.connect.addEventListener("click", connect);
els.tokenInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") connect();
});
els.refresh.addEventListener("click", async () => {
  setBusy(true);
  try { await loadAll(); } finally { setBusy(false); }
});
els.disconnect.addEventListener("click", () => {
  clearToken();
  els.tabs.classList.add("hidden");
  els.layout.classList.add("hidden");
  els.refresh.classList.add("hidden");
  els.disconnect.classList.add("hidden");
  showBanner("info", "Disconnected. Token cleared from this tab.");
});
els.tabs.addEventListener("click", (ev) => {
  const btn = ev.target.closest(".tab");
  if (!btn) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
  btn.classList.add("active");
  document.querySelectorAll(".section").forEach((s) => s.classList.remove("active"));
  $(`section-${btn.dataset.section}`).classList.add("active");
});
els.layout.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-action]");
  if (!btn) return;
  if (btn.dataset.action === "credits") addCredits(Number(btn.dataset.id));
  if (btn.dataset.action === "toggle") toggleActive(Number(btn.dataset.id));
});

(function init() {
  const saved = loadToken();
  if (saved) {
    els.tokenInput.value = saved;
    connect();
  }
})();
