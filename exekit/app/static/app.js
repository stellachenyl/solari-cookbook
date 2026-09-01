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
    // 25 is the default signup grant; scale the meter against it, min 6%.
    const pct = Math.max(6, Math.min(100, (credits / 25) * 100));
    els.creditsBar.style.width = `${pct}%`;
  }
  if (plan) els.plan.textContent = `${plan} plan`;
}

function showBanner(kind, text) {
  els.banner.className = `banner ${kind}`; // "error" | "info" | ""
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
  const key = apiKey();
  if (key) headers["X-API-Key"] = key;
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
  for (const b of [els.run, els.getKey, els.loadInfo, els.createSession, els.killSession]) {
    b.disabled = busy;
  }
  els.run.textContent = busy ? "Running…" : "▶ Run";
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

function renderArtifacts(artifacts) {
  els.artifacts.innerHTML = "";
  if (!artifacts || artifacts.length === 0) {
    els.artifacts.innerHTML = '<span class="placeholder">No artifacts in this run.</span>';
    return;
  }
  for (const art of artifacts) {
    const row = document.createElement("div");
    row.className = "artifact";
    const name = document.createElement("span");
    name.className = "artifact-name";
    name.textContent = art.filename;
    const actions = document.createElement("span");
    actions.className = "artifact-actions";
    const bytes = art.encoding === "base64" ? atob(art.data).length : art.data.length;
    actions.textContent = `${bytes} B · ${art.mime_type || "application/octet-stream"} · `;
    if (bytes <= MAX_PREVIEW_BYTES && looksTextual(art)) {
      const view = document.createElement("a");
      view.href = "#";
      view.textContent = "view";
      view.addEventListener("click", (ev) => {
        ev.preventDefault();
        const text = art.encoding === "base64" ? atob(art.data) : art.data;
        els.stdout.textContent += `\n----- ${art.filename} -----\n${text}`;
      });
      actions.appendChild(view);
    } else {
      const dl = document.createElement("a");
      dl.href = `data:${art.mime_type || "application/octet-stream"};base64,${art.encoding === "base64" ? art.data : btoa(art.data)}`;
      dl.download = art.filename;
      dl.textContent = "download";
      actions.appendChild(dl);
    }
    row.appendChild(name);
    row.appendChild(actions);
    els.artifacts.appendChild(row);
  }
}

function looksTextual(art) {
  return !art.mime_type || /^(text\/|application\/(json|xml|javascript|x-sh))/.test(art.mime_type);
}

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
    if (result.error) {
      showBanner(result.status === "timeout" ? "info" : "error", result.error);
    } else if (result.status === "completed") {
      hideBanner();
    }
  } catch (err) {
    if (err.code === "solari_unconfigured") {
      showBanner("error", "Solari is not configured on this server. Set SOLARI_API_KEY (and restart) to run code. No credit was used.");
    } else if (err.code === "insufficient_credits") {
      showBanner("error", `${err.message} Ask the admin to top you up (billing comes with the Stripe phase).`);
    } else {
      showBanner("error", err.message);
    }
  } finally {
    setBusy(false);
  }
}

els.getKey.addEventListener("click", getKey);
els.loadInfo.addEventListener("click", loadInfo);
els.createSession.addEventListener("click", createSession);
els.killSession.addEventListener("click", killSession);
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
})();
