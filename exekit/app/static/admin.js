/* ExecKit Admin — core: helpers, data loaders, actions (no wiring here).
   Vanilla JS; token in sessionStorage only. Wiring lives in admin_page.js. */
"use strict";

const $ = (id) => document.getElementById(id);

const els = {
  tokenInput: $("token-input"), connect: $("btn-connect"),
  refresh: $("btn-refresh"), disconnect: $("btn-disconnect"),
  banner: $("banner"), tabs: $("tabs"), layout: $("layout"),
  statsGrid: $("stats-grid"),
  bodies: {
    keys: $("keys-body"), executions: $("executions-body"),
    sessions: $("sessions-body"), ledger: $("ledger-body"),
    stripe: $("stripe-body"),
  },
};

const MAX_LIMIT = 200;

function token() {
  return els.tokenInput.value.trim();
}

function saveToken(value) {
  els.tokenInput.value = value;
  try { sessionStorage.setItem("exekit_admin_token", value); } catch { /* ignore */ }
}

function loadToken() {
  try { return sessionStorage.getItem("exekit_admin_token") || ""; }
  catch { return ""; }
}

function clearToken() {
  els.tokenInput.value = "";
  try { sessionStorage.removeItem("exekit_admin_token"); } catch { /* ignore */ }
}

function showBanner(kind, text) {
  els.banner.className = `banner ${kind}`;
  els.banner.textContent = text;
}

function hideBanner() {
  els.banner.className = "banner hidden";
  els.banner.textContent = "";
}

async function apiFetch(path) {
  let resp;
  try {
    resp = await fetch(path, { headers: { "X-Admin-Token": token() } });
  } catch (err) {
    throw { code: "network", message: `Cannot reach ExecKit admin (${err.message})` };
  }
  let body = null;
  try { body = await resp.json(); } catch { /* non-JSON */ }
  if (!resp.ok) {
    const e = (body && body.error) || {};
    throw { code: e.code || "error", message: e.message || `Request failed (HTTP ${resp.status})` };
  }
  return body;
}

function setBusy(busy) {
  els.connect.disabled = busy;
  els.refresh.disabled = busy;
  els.connect.textContent = busy ? "Working…" : "Connect";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtDate(value) {
  if (!value) return "–";
  return new Date(value).toLocaleString();
}

function statusBadge(status) {
  const kind = {
    active: "green", completed: "green", paid: "green",
    killed: "red", failed: "red", lost: "red", expired: "red",
    timeout: "yellow", timed_out: "yellow",
    free: "neutral", "inactive": "red",
  }[status] || "neutral";
  return `<span class="badge ${kind}">${escapeHtml(status)}</span>`;
}

const setBody = (section, html) => { els.bodies[section].innerHTML = html; };
const emptyRow = (section, message, columns) =>
  setBody(section, `<tr><td colspan="${columns}" class="placeholder">${escapeHtml(message)}</td></tr>`);

// --- data loads ---------------------------------------------------------------

async function loadStats() {
  const stats = await apiFetch("/admin/stats");
  const labels = [
    ["total_api_keys", "API keys"], ["active_api_keys", "Active keys"],
    ["paid_api_keys", "Paid keys"], ["total_executions", "Executions"],
    ["completed_executions", "Completed"], ["failed_executions", "Failed"],
    ["timeout_executions", "Timeouts"], ["total_credits_remaining", "Credits left"],
    ["total_credits_granted", "Credits granted"], ["total_credits_consumed", "Credits used"],
    ["active_sessions", "Active sessions"], ["stripe_events_processed", "Stripe events"],
  ];
  els.statsGrid.innerHTML = labels.map(([key, label]) =>
    `<div class="stat-card"><div class="stat-value">${stats[key]}</div>` +
    `<div class="stat-label">${escapeHtml(label)}</div></div>`
  ).join("");
}

async function loadKeys() {
  const body = await apiFetch("/admin/keys?limit=50&offset=0");
  if (body.keys.length === 0) {
    emptyRow("keys", "No API keys yet.", 9);
    return;
  }
  setBody("keys", body.keys.map((k) =>
    `<tr>
      <td>${k.id}</td>
      <td class="mono">…${escapeHtml(k.key_last4)}</td>
      <td>${k.email ? escapeHtml(k.email) : "–"}</td>
      <td>${statusBadge(k.plan)}</td>
      <td class="mono">${k.credits}</td>
      <td>${k.executions_count}</td>
      <td>${k.is_active ? statusBadge("active") : statusBadge("inactive")}</td>
      <td>${fmtDate(k.created_at)}</td>
      <td class="actions">
        <button class="btn small" data-action="credits" data-id="${k.id}">+ Credits</button>
        <button class="btn small ${k.is_active ? "danger" : "primary"}"
                data-action="toggle" data-id="${k.id}">
          ${k.is_active ? "Deactivate" : "Activate"}
        </button>
      </td>
    </tr>`).join(""));
}

async function loadExecutions() {
  const body = await apiFetch("/admin/executions?limit=50&offset=0");
  if (body.executions.length === 0) {
    emptyRow("executions", "No executions yet.", 7);
    return;
  }
  setBody("executions", body.executions.map((e) =>
    `<tr>
      <td>${e.id}</td>
      <td>${e.api_key_id}</td>
      <td>${e.session_id ? escapeHtml(e.session_id) : "–"}</td>
      <td>${statusBadge(e.status)}</td>
      <td>${e.exit_code ?? "–"}</td>
      <td>${fmtDate(e.created_at)}</td>
      <td>${fmtDate(e.finished_at)}</td>
    </tr>`).join(""));
}

async function loadSessions() {
  const body = await apiFetch("/admin/sessions?limit=50&offset=0");
  if (body.sessions.length === 0) {
    emptyRow("sessions", "No sessions yet.", 7);
    return;
  }
  setBody("sessions", body.sessions.map((s) =>
    `<tr>
      <td class="mono">${escapeHtml(s.id)}</td>
      <td class="mono">${escapeHtml(s.solari_session_id)}</td>
      <td>${s.api_key_id}</td>
      <td>${statusBadge(s.status)}</td>
      <td>${fmtDate(s.created_at)}</td>
      <td>${fmtDate(s.last_used_at)}</td>
      <td>${s.killed_at ? fmtDate(s.killed_at) : "–"}</td>
    </tr>`).join(""));
}

async function loadLedger() {
  const body = await apiFetch("/admin/ledger?limit=50&offset=0");
  if (body.ledger.length === 0) {
    emptyRow("ledger", "No ledger entries yet.", 6);
    return;
  }
  setBody("ledger", body.ledger.map((e) =>
    `<tr>
      <td>${e.id}</td>
      <td>${e.api_key_id}</td>
      <td class="${e.amount >= 0 ? "ledger-plus" : "ledger-minus"}">${e.amount > 0 ? "+" : ""}${e.amount}</td>
      <td class="mono">${e.balance_after}</td>
      <td>${escapeHtml(e.reason)}</td>
      <td>${fmtDate(e.created_at)}</td>
    </tr>`).join(""));
}

async function loadStripeEvents() {
  const body = await apiFetch("/admin/stripe-events?limit=50&offset=0");
  if (body.events.length === 0) {
    emptyRow("stripe", "No Stripe events processed yet.", 6);
    return;
  }
  setBody("stripe", body.events.map((e) =>
    `<tr>
      <td>${e.id}</td>
      <td class="mono">${escapeHtml(e.stripe_event_id)}</td>
      <td>${escapeHtml(e.event_type)}</td>
      <td>${e.api_key_id ?? "–"}</td>
      <td>${e.credits ?? "–"}</td>
      <td>${fmtDate(e.processed_at)}</td>
    </tr>`).join(""));
}

async function loadAll() {
  await Promise.all([
    loadStats(), loadKeys(), loadExecutions(),
    loadSessions(), loadLedger(), loadStripeEvents(),
  ]);
}

// --- actions -------------------------------------------------------------------

async function connect() {
  const value = token();
  if (!value) { showBanner("error", "Enter the admin token first."); return; }
  setBusy(true);
  hideBanner();
  try {
    await apiFetch("/admin/stats");
    saveToken(value);
    els.tabs.classList.remove("hidden");
    els.layout.classList.remove("hidden");
    els.refresh.classList.remove("hidden");
    els.disconnect.classList.remove("hidden");
    showBanner("success", "Connected. Loading all sections…");
    await loadAll();
    hideBanner();
  } catch (err) {
    if (err.code === "invalid_admin_token") {
      showBanner("error", "Invalid admin token. Check ADMIN_TOKEN in the server's .env.");
    } else {
      showBanner("error", err.message);
    }
  } finally {
    setBusy(false);
  }
}

async function addCredits(keyId) {
  const amount = window.prompt(`Credits to add to key #${keyId}:`);
  if (amount === null) return; // canceled
  const reason = window.prompt("Ledger reason:", "manual_admin_grant");
  if (reason === null) return;
  let resp;
  try {
    resp = await fetch(`/admin/keys/${keyId}/credits`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Admin-Token": token() },
      body: JSON.stringify({ amount: Number(amount), reason }),
    });
  } catch (err) {
    showBanner("error", `Network error: ${err.message}`);
    return;
  }
  let body = null;
  try { body = await resp.json(); } catch { /* ignore */ }
  if (!resp.ok) {
    showBanner("error", (body && body.error && body.error.message) || `Failed (HTTP ${resp.status})`);
    return;
  }
  showBanner("success", `Key …${body.key_last4} now has ${body.credits} credits.`);
  await Promise.all([loadKeys(), loadLedger(), loadStats()]);
}

async function toggleActive(keyId) {
  let resp;
  try {
    resp = await fetch(`/admin/keys/${keyId}/toggle-active`, {
      method: "POST", headers: { "X-Admin-Token": token() },
    });
  } catch (err) {
    showBanner("error", `Network error: ${err.message}`);
    return;
  }
  let body = null;
  try { body = await resp.json(); } catch { /* ignore */ }
  if (!resp.ok) {
    showBanner("error", (body && body.error && body.error.message) || `Failed (HTTP ${resp.status})`);
    return;
  }
  showBanner("success", `Key …${body.key_last4} is now ${body.is_active ? "ACTIVE" : "INACTIVE"}.`);
  await Promise.all([loadKeys(), loadStats()]);
}

// --- wiring ----------------------------------------------------------------------

// Exposed for admin_page.js (loads after this file).
const admin = {
  els, token, showBanner, hideBanner, apiFetch, setBusy, loadAll,
  loadKeys, loadLedger, loadStats, connect,
};
