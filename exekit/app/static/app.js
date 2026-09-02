/* ExecKit playground. Vanilla JS, no build step.
   localStorage holds only the API key and session id. */
"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  keyInput: $("api-key-input"),
  getKey: $("btn-get-key"),
  loadInfo: $("btn-load-info"),
  creditsBar: $("credits-bar"),
  credits: $("credits-indicator"),
  plan: $("plan-indicator"),
  billingStatus: $("billing-status"),
  billingPackage: $("billing-package"),
  billingBalance: $("billing-balance"),
  buyCredits: $("btn-buy-credits"),
  ledgerToggle: $("btn-ledger-toggle"),
  ledgerPanel: $("ledger-panel"),
  ledgerBody: $("ledger-body"),
  sessionInput: $("session-input"),
  keepSession: $("keep-session"),
  createSession: $("btn-create-session"),
  killSession: $("btn-kill-session"),
  code: $("code"),
  run: $("btn-run"),
  clear: $("btn-clear"),
  stdout: $("out-stdout"),
  stderr: $("out-stderr"),
  meta: $("exec-meta"),
  banner: $("banner"),
  artifacts: $("artifacts"),
};

const MAX_PREVIEW_BYTES = 64 * 1024; // artifacts above this are download-only
const POST_STRIPE_POLLS = 5;         // /keys/me checks after returning from Stripe
const POST_STRIPE_POLL_MS = 3000;

let billingConfig = null;            // last /billing/config payload

function apiKey() {
  return els.keyInput.value.trim();
}

function saveKey(key) {
  els.keyInput.value = key;
  try { localStorage.setItem("exekit_api_key", key); } catch { /* private mode */ }
}

function setSession(id) {
  els.sessionInput.value = id || "";
  try {
    if (id) localStorage.setItem("exekit_session_id", id);
    else localStorage.removeItem("exekit_session_id");
  } catch { /* private mode */ }
}

function setIndicators(credits, plan) {
  if (typeof credits === "number") {
    els.credits.textContent = `${credits} credit${credits === 1 ? "" : "s"}`;
    els.credits.dataset.state = credits > 0 ? "ok" : "empty";
    // scale the meter against the signup grant, min 6%
    const pct = Math.max(6, Math.min(100, (credits / 25) * 100));
    els.creditsBar.style.width = `${pct}%`;
    els.billingBalance.textContent = String(credits);
    els.billingBalance.dataset.state = credits > 0 ? "ok" : "empty";
  }
  if (plan) els.plan.textContent = `${plan} plan`;
}

function showBanner(kind, text) {
  els.banner.className = `banner ${kind}`; // "success" | "cancel" | "error" | "info" | ""
  els.banner.textContent = text;
}

function hideBanner() {
  els.banner.className = "banner hidden";
  els.banner.textContent = "";
}

function extractError(body, fallback) {
  if (body && body.error) {
    const e = body.error;
    let msg = e.message || fallback;
    const d = e.details || {};
    const extras = [];
    if (typeof d.credits_remaining === "number") extras.push(`credits remaining: ${d.credits_remaining}`);
    if (d.execution_id) extras.push(`execution #${d.execution_id} recorded`);
    if (extras.length) msg += ` (${extras.join(", ")})`;
    return { code: e.code || "error", message: msg };
  }
  return { code: "error", message: fallback };
}

async function apiFetch(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (options.auth !== false && apiKey()) headers["X-API-Key"] = apiKey();
  let resp;
  try {
    resp = await fetch(path, Object.assign({}, options, { headers }));
  } catch (err) {
    throw { code: "network", message: `Cannot reach ExecKit API (${err.message})` };
  }
  let body = null;
  try { body = await resp.json(); } catch { /* non-JSON body */ }
  if (!resp.ok) {
    throw extractError(body, `Request failed (HTTP ${resp.status})`);
  }
  return body;
}

function setBusy(busy) {
  for (const b of [els.run, els.getKey, els.loadInfo, els.createSession,
                   els.killSession, els.buyCredits]) {
    b.disabled = busy;
  }
  els.run.textContent = busy ? "Running…" : "▶ Run";
}

function setLedgerBusy(busy) {
  els.ledgerToggle.disabled = busy;
  els.ledgerToggle.textContent = busy ? "Working…" : (els.ledgerPanel.classList.contains("hidden") ? "Show" : "Hide");
}

function resetOutput() {
  els.stdout.textContent = "";
  els.stderr.textContent = "";
  els.meta.textContent = "";
  els.artifacts.innerHTML = '<span class="placeholder">No artifacts in this run.</span>';
  hideBanner();
}

async function getKey() {
  setBusy(true);
  try {
    const body = await apiFetch("/keys/request", { method: "POST", body: JSON.stringify({}) });
    saveKey(body.api_key);
    setIndicators(body.credits, body.plan);
    loadLedger(); // new key: show its (grant-only) history
    showBanner("info", `New key …${body.key_last4} saved locally. It is shown only once — store it somewhere safe.`);
  } catch (err) {
    showBanner("error", err.message);
  } finally {
    setBusy(false);
  }
}

async function loadInfo() {
  if (!apiKey()) { showBanner("error", "Enter an API key first (or get a free one)."); return; }
  setBusy(true);
  try {
    const me = await apiFetch("/keys/me");
    setIndicators(me.credits, me.plan);
    showBanner("info", `Key …${me.key_last4}: ${me.executions_count} execution${me.executions_count === 1 ? "" : "s"} on record.`
      + (me.is_active ? "" : " (INACTIVE)"));
  } catch (err) {
    showBanner("error", err.message);
  } finally {
    setBusy(false);
  }
}

async function loadBillingConfig() {
  try {
    billingConfig = await apiFetch("/billing/config", { auth: false });
    const on = billingConfig.billing_enabled === true;
    els.billingStatus.textContent = on ? "billing enabled" : "billing disabled";
    els.billingStatus.dataset.state = on ? "ok" : "empty";
    els.billingPackage.textContent =
      `${billingConfig.credit_amount} credits / $${billingConfig.price_usd}`;
    els.buyCredits.textContent = `Buy ${billingConfig.credit_amount} credits`;
    els.buyCredits.title = on
      ? `Opens Stripe Checkout (${billingConfig.currency.toUpperCase()})`
      : "Stripe is not configured on this server";
  } catch (err) {
    els.billingStatus.textContent = "billing unknown";
    els.billingStatus.dataset.state = "empty";
  }
}

async function createSession() {
  setBusy(true);
  try {
    const body = await apiFetch("/sessions", { method: "POST" });
    setSession(body.session_id);
    showBanner("info", `Session ${body.session_id} is live. Runs now share state until you kill it.`);
  } catch (err) {
    if (err.code === "solari_unconfigured") {
      showBanner("error", "Solari is not configured on this server, so sessions are unavailable. One-shot runs still work once it is set up.");
    } else {
      showBanner("error", err.message);
    }
  } finally {
    setBusy(false);
  }
}

async function killSession() {
  const sid = els.sessionInput.value.trim();
  if (!sid) { showBanner("error", "No session to kill."); return; }
  setBusy(true);
  try {
    const body = await apiFetch(`/sessions/${encodeURIComponent(sid)}`, { method: "DELETE" });
    setSession("");
    showBanner("info", `Session ${body.session_id} killed. Sandbox destroyed.`);
  } catch (err) {
    if (err.code === "session_gone") {
      setSession("");
      showBanner("info", "Session was already gone; cleared it locally.");
    } else {
      showBanner("error", err.message);
    }
  } finally {
    setBusy(false);
  }
}

// --- billing -------------------------------------------------------------------

async function loadLedger() {
  if (!apiKey()) {
    els.ledgerBody.innerHTML =
      '<tr><td colspan="4" class="placeholder">Enter an API key to see credit history.</td></tr>';
    return;
  }
  setLedgerBusy(true);
  try {
    const body = await apiFetch("/billing/ledger");
    setIndicators(body.credits, null);
    renderLedger(body.ledger || []);
  } catch (err) {
    els.ledgerBody.innerHTML =
      `<tr><td colspan="4" class="placeholder">${escapeHtml(err.message)}</td></tr>`;
  } finally {
    setLedgerBusy(false);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function renderLedger(entries) {
  els.ledgerBody.innerHTML = "";
  if (entries.length === 0) {
    els.ledgerBody.innerHTML =
      '<tr><td colspan="4" class="placeholder">No credit events yet.</td></tr>';
    return;
  }
  for (const e of entries) {
    const tr = document.createElement("tr");
    const when = new Date(e.created_at + (e.created_at.endsWith("Z") ? "" : "Z"));
    const amount = e.amount > 0 ? `+${e.amount}` : String(e.amount);
    const cells = [
      when.toLocaleString(),
      e.reason,
      amount,
      String(e.balance_after),
    ];
    for (const value of cells) {
      const td = document.createElement("td");
      td.textContent = value;
      if (value === amount) td.className = e.amount > 0 ? "ledger-plus" : "ledger-minus";
      tr.appendChild(td);
    }
    els.ledgerBody.appendChild(tr);
  }
}

async function buyCredits() {
  if (!apiKey()) {
    showBanner("error", "Create or enter an API key first.");
    return;
  }
  setBusy(true);
  try {
    const body = await apiFetch("/billing/checkout", {
      method: "POST",
      body: JSON.stringify({ credits: null }),
    });
    // Redirect to Stripe Checkout; the post-Stripe logic runs on the way back.
    window.location.href = body.checkout_url;
  } catch (err) {
    if (err.code === "billing_disabled") {
      showBanner("error", "Billing is disabled because Stripe is not configured.");
    } else if (err.code === "network") {
      showBanner("error", err.message);
    } else {
      showBanner("error", err.message);
    }
  } finally {
    setBusy(false);
  }
}

// After returning from Stripe, poll /keys/me a few times: the webhook that
// grants the credits usually lands within seconds of the redirect.
async function pollCreditsAfterStripe() {
  let baseline = 0;
  try {
    const me = await apiFetch("/keys/me");
    baseline = me.credits;
    setIndicators(me.credits, me.plan);
  } catch { /* key problems surface below */ }
  if (!apiKey()) {
    showBanner("info", "Payment received. Load an API key to see your credits once the webhook lands.");
    return;
  }
  for (let attempt = 1; attempt <= POST_STRIPE_POLLS; attempt += 1) {
    await new Promise((r) => setTimeout(r, POST_STRIPE_POLL_MS));
    try {
      const me = await apiFetch("/keys/me");
      setIndicators(me.credits, me.plan);
      if (me.credits > baseline) {
        showBanner("success",
          `Payment confirmed: ${me.credits - baseline} credit${me.credits - baseline === 1 ? "" : "s"} added.`);
        loadLedger();
        return;
      }
    } catch { /* transient; keep polling */ }
  }
  showBanner("info",
    `Still ${baseline} credits — the webhook has not landed yet. It usually arrives within a minute; reload to check again.`);
}

function handleStripeReturn() {
  // The /billing/success and /billing/cancel pages redirect back here with a
  // ?billing= flag after showing their static copy.
  const flag = new URLSearchParams(window.location.search).get("billing");
  if (flag === "success") {
    showBanner("success", "Payment received. Checking whether your credits have landed…");
    history.replaceState(null, "", "/");
    if (apiKey()) pollCreditsAfterStripe();
    else showBanner("info", "Payment received. Load an API key to see your credits once the webhook lands.");
    return true;
  }
  if (flag === "cancel") {
    showBanner("cancel", "Payment canceled — nothing was charged.");
    history.replaceState(null, "", "/");
    return true;
  }
  return false;
}

// --- run -------------------------------------------------------------------------

async function runCode() {
  const code = els.code.value;
  if (!apiKey()) {
    showBanner("error", "No API key. Click “Get free API key” or paste yours above.");
    return;
  }
  if (!code.trim()) {
    showBanner("error", "Nothing to run — the editor is empty.");
    return;
  }
  setBusy(true);
  resetOutput();
  try {
    let sessionId = els.sessionInput.value.trim() || null;
    if (els.keepSession.checked && !sessionId) {
      const created = await apiFetch("/sessions", { method: "POST" });
      setSession(created.session_id);
      sessionId = created.session_id;
    }
    const result = await apiFetch("/executions", {
      method: "POST",
      body: JSON.stringify({ code, session_id: sessionId }),
    });
    els.stdout.textContent = result.stdout || "";
    els.stderr.textContent = result.stderr || "";
    renderArtifacts(result.artifacts);
    els.meta.textContent =
      `#${result.execution_id} · ${result.status} · exit ${result.exit_code ?? "–"} · ${result.credits_remaining} credits left`;
    setIndicators(result.credits_remaining, null);
    loadLedger();
    if (result.error) {
      showBanner(result.status === "timeout" ? "info" : "error", result.error);
    } else if (result.status === "completed") {
      hideBanner();
    }
  } catch (err) {
    if (err.code === "solari_unconfigured") {
      showBanner("error", "Solari is not configured on this server. Set SOLARI_API_KEY (and restart) to run code. No credit was used.");
    } else if (err.code === "insufficient_credits") {
      showBanner("error", `${err.message} Use “Buy credits” in the Billing panel.`);
    } else if (err.code === "billing_disabled") {
      showBanner("error", "Billing is disabled because Stripe is not configured.");
    } else {
      showBanner("error", err.message);
    }
  } finally {
    setBusy(false);
  }
}

// --- wiring -------------------------------------------------------------------------

els.getKey.addEventListener("click", getKey);
els.loadInfo.addEventListener("click", loadInfo);
els.createSession.addEventListener("click", createSession);
els.killSession.addEventListener("click", killSession);
els.buyCredits.addEventListener("click", buyCredits);
els.ledgerToggle.addEventListener("click", () => {
  const hidden = els.ledgerPanel.classList.toggle("hidden");
  els.ledgerToggle.setAttribute("aria-expanded", String(!hidden));
  els.ledgerToggle.textContent = hidden ? "Show" : "Hide";
  if (!hidden) loadLedger();
});
els.run.addEventListener("click", runCode);
els.clear.addEventListener("click", () => {
  resetOutput();
  els.code.value = "";
  els.code.focus();
});
els.code.addEventListener("keydown", (ev) => {
  // Tab inserts spaces instead of leaving the textarea; Ctrl/Cmd+Enter runs.
  if (ev.key === "Tab") {
    ev.preventDefault();
    const { selectionStart: s, selectionEnd: e } = els.code;
    els.code.setRangeText("    ", s, e, "end");
  } else if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
    ev.preventDefault();
    runCode();
  }
});

(function init() {
  try {
    const savedKey = localStorage.getItem("exekit_api_key");
    if (savedKey) els.keyInput.value = savedKey;
    const savedSession = localStorage.getItem("exekit_session_id");
    if (savedSession) els.sessionInput.value = savedSession;
  } catch { /* private mode */ }

  if (!handleStripeReturn()) {
    loadBillingConfig();
    if (apiKey()) {
      loadLedger();
      els.ledgerPanel.classList.remove("hidden");
      els.ledgerToggle.textContent = "Hide";
      els.ledgerToggle.setAttribute("aria-expanded", "true");
    }
  } else {
    loadBillingConfig();
  }
})();
