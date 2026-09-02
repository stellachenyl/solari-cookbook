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
  history: $("history"),
  historyMeta: $("history-meta"),
  execDetail: $("exec-detail"),
  execDetailTitle: $("exec-detail-title"),
  execDetailMeta: $("exec-detail-meta"),
  detailStatus: $("detail-status"),
  detailStdout: $("detail-stdout"),
  detailStderr: $("detail-stderr"),
  detailError: $("detail-error"),
  detailPreview: $("detail-preview"),
  detailPreviewBody: $("detail-preview-body"),
  detailArtifacts: $("detail-artifacts"),
  closeDetail: $("btn-close-detail"),
};

const MAX_PREVIEW_BYTES = 64 * 1024; // artifacts above this are download-only
const POST_STRIPE_POLLS = 5;         // /keys/me checks after returning from Stripe
const POST_STRIPE_POLL_MS = 3000;
const HISTORY_PAGE_SIZE = 10;        // executions shown in the history panel

let billingConfig = null;            // last /billing/config payload

// The execution currently open in the detail modal; artifact downloads and
// previews resolve against it.
let detailExecution = null;

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
    loadHistory();
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
    loadHistory();
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

// --- execution history & artifacts ------------------------------------------------
// Artifact content is never trusted as markup: everything dynamic is rendered
// via textContent, no eval, no innerHTML with artifact-derived strings.

function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function formatWhen(iso) {
  const when = new Date(iso + (iso.endsWith("Z") ? "" : "Z"));
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
}

// Authenticated artifact fetch: the download endpoint requires X-API-Key, so a
// plain <a href> cannot work — the content is fetched and saved as a blob.
async function fetchArtifact(executionId, artifactId) {
  const resp = await fetch(
    `/executions/${executionId}/artifacts/${artifactId}/download`,
    { headers: { "X-API-Key": apiKey() } },
  );
  if (!resp.ok) {
    let code = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body && body.error && body.error.code) code = body.error.code;
    } catch { /* non-JSON error body */ }
    const message = code === "artifact_not_stored"
      ? "Artifact content was not stored (too large or binary)."
      : `Download failed (${code})`;
    throw new Error(message);
  }
  return resp;
}

// Status line inside the detail modal. Errors also surface as the main banner
// when the modal is closed (e.g. downloads started from the run output).
function setDetailStatus(text, isError = false) {
  els.detailStatus.textContent = text;
  els.detailStatus.classList.toggle("status-error", Boolean(isError));
  if (isError && els.execDetail.classList.contains("hidden")) showBanner("error", text);
}

async function downloadArtifact(executionId, art) {
  try {
    const resp = await fetchArtifact(executionId, art.id);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = art.filename || "artifact.txt";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    setDetailStatus("");
  } catch (err) {
    setDetailStatus(err.message, true);
  }
}

// Preview is text-only by design: text/* mimes, stored content only, capped
// size. The response body is already decoded server-side; nothing is executed.
async function previewArtifact(executionId, art) {
  els.detailPreviewBody.textContent = `Loading ${art.filename}…`;
  els.detailPreview.classList.remove("hidden");
  try {
    const resp = await fetchArtifact(executionId, art.id);
    const text = await resp.text();
    els.detailPreviewBody.textContent = text;
  } catch (err) {
    els.detailPreviewBody.textContent = err.message;
  }
}

function canPreview(art) {
  return Boolean(art.download_available)
    && typeof art.mime_type === "string"
    && art.mime_type.startsWith("text/")
    && art.size_bytes <= MAX_PREVIEW_BYTES;
}

function renderArtifacts(container, artifacts, executionId, { preview = false } = {}) {
  container.textContent = "";
  if (!artifacts || artifacts.length === 0) {
    const span = document.createElement("span");
    span.className = "placeholder";
    span.textContent = "No artifacts in this run.";
    container.appendChild(span);
    return;
  }
  for (const art of artifacts) {
    const row = document.createElement("div");
    row.className = "artifact";
    const name = document.createElement("span");
    name.className = "artifact-name";
    name.textContent =
      `${art.filename} · ${formatBytes(art.size_bytes)} · ${art.mime_type || "unknown type"}`;
    const actions = document.createElement("span");
    actions.className = "artifact-actions";
    if (art.download_available) {
      const dl = document.createElement("a");
      dl.href = "#";
      dl.textContent = "download";
      dl.addEventListener("click", (ev) => {
        ev.preventDefault();
        downloadArtifact(executionId, art);
      });
      actions.appendChild(dl);
      if (preview && canPreview(art)) {
        actions.appendChild(document.createTextNode(" · "));
        const pv = document.createElement("a");
        pv.href = "#";
        pv.textContent = "preview";
        pv.addEventListener("click", (ev) => {
          ev.preventDefault();
          previewArtifact(executionId, art);
        });
        actions.appendChild(pv);
      }
    } else {
      actions.textContent = "not stored (too large or binary)";
    }
    row.appendChild(name);
    row.appendChild(actions);
    container.appendChild(row);
  }
}

function renderHistoryPlaceholder(text) {
  els.history.textContent = "";
  const span = document.createElement("span");
  span.className = "placeholder";
  span.textContent = text;
  els.history.appendChild(span);
  els.historyMeta.textContent = "";
}

async function loadHistory() {
  if (!apiKey()) {
    renderHistoryPlaceholder("Enter an API key to see your recent executions.");
    return;
  }
  try {
    const body = await apiFetch(`/executions?limit=${HISTORY_PAGE_SIZE}&offset=0`);
    renderHistory(body.executions || []);
  } catch (err) {
    renderHistoryPlaceholder(`History unavailable: ${err.message}`);
  }
}

function renderHistory(executions) {
  els.history.textContent = "";
  if (executions.length === 0) {
    renderHistoryPlaceholder("No executions yet — run some code!");
    return;
  }
  els.historyMeta.textContent = `latest ${executions.length}`;
  for (const e of executions) {
    const row = document.createElement("div");
    row.className = "history-row";

    const id = document.createElement("span");
    id.className = "history-id";
    id.textContent = `#${e.id}`;

    const status = document.createElement("span");
    status.className = `history-status status-${e.status}`;
    status.textContent = e.status;

    const when = document.createElement("span");
    when.className = "history-when";
    when.textContent = e.created_at ? formatWhen(e.created_at) : "";

    const arts = document.createElement("span");
    arts.className = "history-count";
    arts.textContent = `${e.artifact_count} artifact${e.artifact_count === 1 ? "" : "s"}`;

    const view = document.createElement("button");
    view.className = "btn btn-small";
    view.textContent = "View";
    view.addEventListener("click", () => viewExecution(e.id));

    row.append(id, status, when, arts, view);
    els.history.appendChild(row);
  }
}

async function viewExecution(executionId) {
  detailExecution = executionId;
  setDetailStatus("");
  els.detailPreviewBody.textContent = "";
  els.detailPreview.classList.add("hidden");
  els.execDetailTitle.textContent = `Execution #${executionId}`;
  els.execDetailMeta.textContent = "loading…";
  els.detailStdout.textContent = "";
  els.detailStderr.textContent = "";
  els.detailError.textContent = "";
  els.detailArtifacts.textContent = "";
  els.execDetail.classList.remove("hidden");
  try {
    const d = await apiFetch(`/executions/${executionId}?include_code=false`);
    els.execDetailMeta.textContent =
      `${d.status} · exit ${d.exit_code ?? "–"}`
      + (d.duration_ms != null ? ` · ${d.duration_ms} ms` : "")
      + ` · ${formatWhen(d.created_at)}`;
    els.detailStdout.textContent = d.stdout || "(empty)";
    els.detailStderr.textContent = d.stderr || "(empty)";
    els.detailError.textContent = d.error || "–";
    renderArtifacts(els.detailArtifacts, d.artifacts, d.id, { preview: true });
    setDetailStatus("");
  } catch (err) {
    els.execDetailMeta.textContent = "";
    setDetailStatus(err.message, true);
  }
}

function closeDetail() {
  els.execDetail.classList.add("hidden");
  detailExecution = null;
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
    renderArtifacts(els.artifacts, result.artifacts, result.execution_id);
    els.meta.textContent =
      `#${result.execution_id} · ${result.status} · exit ${result.exit_code ?? "–"} · ${result.credits_remaining} credits left`;
    setIndicators(result.credits_remaining, null);
    loadLedger();
    loadHistory();
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
els.closeDetail.addEventListener("click", closeDetail);
els.execDetail.addEventListener("click", (ev) => {
  if (ev.target === els.execDetail) closeDetail(); // click on the backdrop
});
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !els.execDetail.classList.contains("hidden")) closeDetail();
});
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
      loadHistory();
      els.ledgerPanel.classList.remove("hidden");
      els.ledgerToggle.textContent = "Hide";
      els.ledgerToggle.setAttribute("aria-expanded", "true");
    }
  } else {
    loadBillingConfig();
    loadHistory();
  }
})();
