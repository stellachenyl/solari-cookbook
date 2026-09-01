# ExecKit — Implementation Plan

Build order, phase gates, and verification. Companion docs:
[SOLARI_SDK_SURFACE.md](SOLARI_SDK_SURFACE.md) (verified SDK calls),
[EXEKIT_SPEC.md](EXEKIT_SPEC.md) (contracts), [FILE_PLAN.md](FILE_PLAN.md)
(tree).

Ground rules for every phase:
- Only `app/services/solari_runner.py` imports `solari_sandbox`.
- Every SDK call used appears in the verified surface doc — no invented methods.
- Each phase ends runnable: `make run` (and `make test` from Phase 3 on) works.

## Phase 1 — Scaffold

Files: the whole tree from FILE_PLAN.md with stub bodies.

- `config.py`: pydantic-settings `Settings` — `solari_api_key`,
  `solari_base_url="https://api.getsolari.com"`, `database_url`
  (default `sqlite+aiosqlite:///./exekit.db`), `exec_timeout_s=30` (cap 120),
  `session_idle_ms=15*60_000`, `session_ttl_min=30`, `max_concurrent_sandboxes=4`,
  `signup_grant_credits=25`, `admin_token`, `artifact_dir="/tmp/exekit"`.
- `db.py` + `models.py`: async SQLAlchemy engine, all five tables, `init_db()`
  with `create_all` (no migrations in v1).
- `main.py`: app factory, mounts routers, `/` → static playground placeholder.
- `.env.example`, `Makefile` (`install`, `run`), `requirements.txt`,
  `exekit/README.md`.
- **Gate:** `uvicorn` boots, `GET /health` returns
  `{"status":"ok","solari":{"configured": <bool>}}`; playground placeholder
  renders.

## Phase 2 — Solari adapter

Files: `services/solari_runner.py`, `scripts/inspect_solari.py`.

- Implement a narrow `SolariRunner` protocol used by the rest of the app:
  `one_shot(code) -> RunOutcome`, `session_create() -> (sandbox_id, context_id)`,
  `session_run(sandbox_id, context_id, code) -> (RunOutcome, context_id | None)`,
  `session_kill(sandbox_id)`, `list_artifacts(sandbox_id)`, `read_artifact(sandbox_id, name)`.
- Use only verified calls (SDK surface doc §B–§G): `SandboxClient(api_key=…,
  base_url=…)`, `client.create(template="base", timeout_ms=…)`,
  `sandbox.connect()`, `sandbox.create_code_context("python")`,
  `sandbox.run_code(code, context_id=…)`, `client.connect(sandbox_id)`,
  `sandbox.kill()` / `client.kill(id)`, `sandbox.files.list/read`,
  `client.get(id)` for state checks.
- `run_code` has no timeout param (verified) → wrap in `asyncio.wait_for`;
  on `TimeoutError` kill the sandbox (kernel is not interruptible) and return
  `status="timeout"`.
- Assemble `stdout`/`stderr` by joining `RunCodeResult.results` items of type
  `stdout`/`stderr`; map truthy `result.error` → `failed`, `exit_code=1`.
- Map SDK exceptions to the spec's error classes: `AuthError`/`GatewayError`/
  `NoCapacityError`/`ConcurrencyLimitError`/SDK `TimeoutError`/`ConnectionError`
  → `solari_unavailable`; missing env key → `solari_unconfigured` (pre-flight).
- **First resolve the two UNVERIFIED items** with `scripts/inspect_solari.py`
  against a real key: (a) does a kernel `context_id` survive
  `client.connect(sandbox_id)` re-attach in a new process? (b) confirm
  `files.list`/`files.read` on `/tmp/exekit` work right after `connect()`.
  Adjust the reattach fallback (fresh context) accordingly and record findings
  in SOLARI_SDK_SURFACE.md.
- **Gate:** `python scripts/inspect_solari.py` prints sandbox create → run_code
  stateful round-trip → artifact write/read → kill, all green.

## Phase 3 — API keys and quota

Files: `auth.py`, `dependencies.py`, `services/keys_service.py`,
`services/quota.py`, `routes/keys.py`, `routes/admin.py`,
`scripts/create_key.py`, `scripts/add_credits.py`.

- Key format: `ek_` + `secrets.token_urlsafe(24)`; store SHA-256 hash + last4;
  return the plaintext exactly once. Accept `Authorization: Bearer` or
  `X-API-Key`.
- `POST /keys/request`: validate body, per-IP daily limit
  (`key_request_rates` table, hashed IP), create key, write `signup_grant`
  ledger row, respond with the one-time key.
- `GET /keys/me`: hash lookup, reject `is_active=False` with 401.
- `quota.py`: `debit(key_id, execution_id)` — single
  `UPDATE ... WHERE credits >= 1` + ledger row with `balance_after` in one
  transaction; `refund(...)` writes `solari_refund`; `grant(...)` writes
  `admin_grant`. 402 via `insufficient_credits` envelope (spec §D).
- `admin.py`: token-gated (`X-Admin-Token` = `ADMIN_TOKEN`), localhost-biased
  helpers: list keys, grant credits.
- **Gate:** curl scripts for request-key → me → 402-when-empty → admin grant
  all pass; `make test` covers auth + quota with the fake runner.

## Phase 4 — Execution and session routes

Files: `services/executions.py`, `services/sessions.py`,
`routes/executions.py`, `routes/sessions.py`, `scripts/smoke_test.py`.

- `POST /executions` orchestration (spec §F/§G):
  pre-flight (key active, language supported, credits) → debit → dispatch
  (one-shot or session reattach) → `asyncio.wait_for` run → artifacts diff
  (list before/after, changed-or-new files) → refund on infra-class outcomes →
  persist `executions` row → response shape exactly per spec.
- `POST /sessions`: quota pre-check, `runner.session_create()`, DB row.
- `GET /sessions/{id}`: owner-checked view. `DELETE`: owner-checked kill,
  `status=killed`. `GET /sessions/{id}/files/{filename}`: basename-sanitized,
  owner-checked `runner.read_artifact`, `Content-Disposition: attachment`.
- Reaper task in `main.py` lifespan: kill expired/lost sessions (spec §F).
- Global semaphore for `max_concurrent_sandboxes`; waiters that exceed a short
  queue timeout get `solari_unavailable`.
- **Gate:** `scripts/smoke_test.py` runs the full user journey against a real
  Solari key (one-shot hello, stateful session two-step, artifact download,
  kill, refund path by pointing `SOLARI_BASE_URL` at an invalid host).

## Phase 5 — Frontend playground

Files: `app/static/index.html`, `app.js`, `styles.css`, `routes/pages.py`.

- Single page, no build step: "Try without a key" (hits `/executions` with a
  demo key auto-provisioned server-side, heavily credit-capped) and
  "Bring your key" panel (`/keys/me`, credits badge, code editor textarea,
  Run / New session / Kill session buttons, stdout/stderr panes, artifact list
  with download links).
- Style consistent with the cookbook's plain, small philosophy — no framework,
  no npm; `styles.css` ~200 lines.
- **Gate:** manual pass in the browser: run one-shot, run in session twice
  (state visible), download artifact, 402 banner on empty credits.

## Phase 6 — Tests and polish

Files: `tests/` per FILE_PLAN.md, README polish.

- `test_quota.py`: debit atomicity (concurrent debits can't go negative),
  refund math, ledger `balance_after` chain, 402 body shape.
- `test_executions.py`: status mapping table (completed/failed/timeout/
  solari_*/unsupported) via fake runner; artifacts diff; session not-found /
  foreign-owner 404; response body matches spec JSON exactly (schema test).
- `test_auth.py`: hash/verify, both header styles, inactive key 401.
- Polish: request logging middleware, consistent error envelope on
  FastAPI validation errors, CORS (config-gated), `Makefile test` green,
  README quickstart + curl examples.
- **Gate:** `make test` green offline (fake runner), `make smoke` green
  online (real key), spec ↔ implementation spot-check of every endpoint.

## Future phase — Stripe billing

Deliberately out of scope until v1 is stable; the extension points are
pre-built (spec §I):

1. Add `stripe` + `stripe_customer_id` on `api_keys`; plans table
   (`plan`, `monthly_included_credits`, `stripe_price_id`).
2. `POST /billing/checkout` (Stripe Checkout session) and
   `POST /billing/webhook` (signature-verified; events: checkout completed →
   plan upgrade + `stripe_purchase`/`subscription_grant` ledger rows,
   invoice paid → monthly credit top-up, subscription canceled → downgrade).
3. `GET /keys/me` gains additive `billing` object; 402 `insufficient_credits`
   responses gain a `checkout_url` hint in `details`.
4. No changes to quota admission (it only reads `credits`) or to the ledger
   schema (new `reason` values only).

## Definition of Done (whole build)

- Every endpoint from spec §D implemented with the exact request/response
  shapes; error envelope everywhere.
- Credits: debit-before-run, refund-on-infra, full ledger; 402 on empty.
- Sessions: create/attach/run/kill/reap lifecycle per spec §F; owner-scoped.
- All SDK usage confined to `solari_runner.py`, matching
  SOLARI_SDK_SURFACE.md; the doc's UNVERIFIED items resolved in Phase 2.
- `make test` (offline) and `make smoke` (online) green; playground usable
  end-to-end in a browser.
