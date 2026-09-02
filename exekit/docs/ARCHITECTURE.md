# ExecKit Architecture

How ExecKit is built, as implemented in `exekit/app/`. The contract with the
Solari SDK is documented separately in
[SOLARI_SDK_SURFACE.md](../../docs/SOLARI_SDK_SURFACE.md).

```
Browser playground (static/)      Admin dashboard (static/)
        │  fetch + X-API-Key              │  fetch + X-Admin-Token
        ▼                                 ▼
┌──────────────────────── FastAPI app (main.py) ────────────────────────┐
│ RequestIDMiddleware → global error handlers → static mounts           │
│ routes: keys · executions · sessions · history · billing · admin      │
│    │ Depends: get_db · get_current_api_key · rate-limit buckets       │
│    ▼                                                                  │
│ services: api_keys · quota · sessions · executions · rate_limit ·     │
│           solari_runner · stripe · session cleanup task               │
│    ▼                          │                                       │
│ SQLModel (SQLite, WAL)        ▼                                       │
│                       solari-sandbox SDK (network boundary)           │
└───────────────────────────────────────────────────────────────────────┘
```

## Module map

| Module | Responsibility |
| --- | --- |
| `app/config.py` | Pydantic settings from env/`.env`; fails fast on empty `ADMIN_TOKEN` outside debug; `billing_enabled` derived from the Stripe key |
| `app/main.py` | App assembly, router registration, error handlers, `/` and `/admin` pages, lifespan (create tables, start session reaper) |
| `app/models.py` | SQLModel tables (below) |
| `app/schemas.py` | Pydantic request/response models; code-length and session-id validators |
| `app/errors.py` | Domain exception taxonomy; every error carries `code` + `http_status` |
| `app/dependencies.py` | `get_db`, `get_current_api_key`, `get_runner`, `get_admin_token`, rate-limit dependencies |
| `app/routes/*` | Thin HTTP layer: validate, call a service, map errors |
| `app/services/solari_runner.py` | The only module that imports `solari-sandbox`; typed results in, domain errors out |
| `app/services/executions.py` | Execution business logic: validate → bill → run → persist → respond |
| `app/services/api_keys.py` | Key generation (`ek_live_…`), SHA-256 hashing, lookups |
| `app/services/quota.py` | Credit consume/refund on the append-only ledger |
| `app/services/sessions.py` | Session ownership, kill/lost marking |
| `app/services/stripe.py` | Checkout session creation, webhook verification, credit grants |
| `app/services/rate_limit.py` | In-process sliding-window limiter (3 buckets) |
| `app/middleware/request_id.py` | `X-Request-ID` generation/echo |

## Data model

| Table | Notes |
| --- | --- |
| `api_keys` | `key_hash` (SHA-256, unique), `key_last4`, `email`, `plan`, `credits`, `executions_count`, `is_active` |
| `credit_ledger` | Append-only: `api_key_id`, `amount`, `balance_after`, `reason` (`initial_free_credits`, `execution`, `execution_refund`, `manual_admin_grant`, `stripe_purchase`) |
| `sandbox_sessions` | Public `session_id` ↔ internal `solari_session_id`, `status` (`active`/`killed`/`lost`), `killed_at` |
| `executions` | `api_key_id`, `session_id`, `code`, `stdout`, `stderr`, `exit_code`, `status`, `error`, `created_at`/`finished_at` |
| `execution_artifacts` | `execution_id` (indexed), `filename`, `path`, `mime_type`, `encoding`, `size_bytes`, `data_text` (nullable) |
| `stripe_webhook_events` | Processed Stripe event ids — makes webhook handling idempotent |

## Request lifecycle (POST /executions)

1. `RequestIDMiddleware` assigns/echoes `X-Request-ID`.
2. Route dependencies resolve the DB session, apply the `executions` rate
   limit, and resolve the API key (401/403 before any work).
3. Pydantic validates the body (string type, ≤ 50,000 chars, session-id
   length); failures render as `invalid_request`.
4. The service validates again (defense in depth), resolves the session
   (404/410), and **consumes a credit before running**.
5. `SolariRunner.run_python` executes in the sandbox. `KeyError` on a lost
   runner handle marks the session `lost` and refunds.
6. The `Execution` row is committed, `executions_count` incremented.
7. Infra failures (`solari_unconfigured`/`solari_unavailable`) refund the
   credit and raise a domain error → 502/503. User-code failures and
   timeouts are normal 200 responses with the status in the body.
8. `persist_artifacts` stores each artifact (second commit — see below) and
   the response carries `ArtifactMeta`, never artifact bytes.

### Artifact storage rules

Artifacts arrive base64-encoded from the SDK. `persist_artifacts` decodes
once to size them:

- decoded size ≤ 100,000 bytes **and** valid UTF-8 → stored inline in
  `data_text` (the base64 wire data itself is never written to the DB);
- otherwise (oversized, binary, undecodable) → metadata only, with the
  reason logged. `download_available` in every response mirrors
  `data_text is not None`, and the download endpoint answers
  `404 artifact_not_stored` for anything not stored.

The execution and its artifacts commit in two transactions; a crash in
between leaves a completed execution with zero artifacts (tested), which
degrades to a missing download rather than corrupt state.

### Billing flow

Checkout is a Stripe Checkout Session built entirely from server config —
client-supplied amounts are ignored. Credits move only through verified
`checkout.session.completed` webhooks, keyed by metadata (`api_key_id`,
`credits`) set at session creation. Every event id is stored before
granting, so duplicate deliveries never double-credit. `billing_enabled` is
derived from the presence of `STRIPE_SECRET_KEY`; there is no env flag that
can enable billing without credentials.

### Security model

- Raw API keys exist only at creation; the DB holds a SHA-256 hash and the
  last 4. Deactivated keys are distinguishable from unknown ones (403 vs
  401) but lose access to everything.
- **Ownership scoping**: every execution/session/artifact lookup filters on
  the caller's `api_key_id` and renders a mismatch as a plain 404, so
  another user's resources are indistinguishable from nonexistent ones —
  ids (sequential integers) cannot be enumerated.
- Admin routes compare the token with `secrets.compare_digest`; the exact
  failure message ("Invalid admin token.") is asserted by tests.
- Untrusted bytes are never reflected as markup: downloads force
  `text/*`/`application/octet-stream` content types with
  `Content-Disposition: attachment`, filenames are regex-validated into
  headers, and the playground renders artifact content via `textContent`
  only.
- Logs carry `key_last4`, request ids, and byte counts — never raw keys or
  artifact content.
- Session file reads reject traversal (`/`, `\`, `.`/`..`) before touching
  the runner.

### Sessions and the reaper

A session holds a live Solari sandbox (`solari_session_id`) owned by one API
key. Killing destroys the sandbox server-side. Timeouts destroy the sandbox
too (kernels cannot be interrupted). If the process restarts, in-memory
runner handles are gone; the first run against such a session gets a
`KeyError`, marks it `lost`, and refunds. A background task started in the
app lifespan reaps idle sessions every `SESSION_CLEANUP_INTERVAL_SECONDS`.

## Testing

237 tests (pytest, `asyncio_mode = auto`) run against a per-test file-backed
SQLite database with dependency overrides and a scripted
`FakeSolariRunner` — no network, no randomness, no shared state. Suites map
to features (`test_executions_route.py`, `test_history.py`,
`test_billing*.py`, `test_admin.py`, `test_hardening.py`,
`test_session_cleanup.py`, …) plus runner-level tests against fake SDK
objects. Coverage on the newest modules: `routes/history.py` 100%,
`services/executions.py` 98%. `scripts/smoke_test.py` exercises a *running*
server (with or without `SOLARI_API_KEY`) and the frontend was verified
end-to-end in headless Chromium.
