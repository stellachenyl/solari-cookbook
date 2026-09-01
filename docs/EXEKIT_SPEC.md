# ExecKit — Product Spec (v1)

Status: planning. Implementation plan: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).
File tree: [FILE_PLAN.md](FILE_PLAN.md). Verified SDK surface:
[SOLARI_SDK_SURFACE.md](SOLARI_SDK_SURFACE.md).

## A. Product name

**ExecKit** — an API and web playground for executing AI-generated Python code
inside [Solari](https://getsolari.com) sandboxes.

## B. One-line pitch

> **ExecKit: one POST request turns LLM-generated Python into sandboxed,
> stateful execution — stdout, files, and credits included.**

## C. Core user flow

1. **Request a key** — `POST /keys/request` with an email (optional). ExecKit
   issues an API key (`slr_...`-style prefix is unnecessary; ExecKit uses its
   own `ek_` prefix), shows it **once**, stores only the hash + last4, and
   grants starter credits.
2. **Run code** — the user (or their agent) calls `POST /executions` with
   `{"code": "..."}` and gets stdout/stderr/exit code back. One credit consumed
   per execution, atomically checked-and-debited before the sandbox runs.
3. **Keep state** (optional) — `POST /sessions` creates a Solari sandbox and a
   kernel context; subsequent executions with `session_id` share variables and
   imports, notebook-style. Sessions are killed by the owner or reaped after
   inactivity.
4. **Grab artifacts** — code that writes to `/tmp/exekit/` (ExecKit's mounted
   artifact directory convention) gets those files returned as downloadable
   artifacts on the execution response and via
   `GET /sessions/{session_id}/files/{filename}`.
5. **Monitor** — the built-in playground (served at `/`) is a keyless
   demo + a "paste your key" console; `GET /keys/me` shows credits, plan, and
   usage. When credits run out, execution returns HTTP **402
   `insufficient_credits`** with instructions on how to top up (v1: admin
   grants; later: Stripe).

The audience is AI-agent developers who need a reliable "run the model's
Python somewhere safe" primitive without operating Firecracker/E2B-style
infrastructure themselves.

## D. API surface

Base URL: `https://<host>` (v1: no versioned prefix; mount under `/v1` later is
an extension point). All bodies JSON. Auth: `Authorization: Bearer <key>` **or**
`X-API-Key: <key>`.

### Endpoints

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/keys/request` | none (rate-limited) | Issue a new API key |
| GET | `/keys/me` | API key | Key metadata, plan, credits, usage |
| POST | `/executions` | API key | Run Python (one-shot or in a session) |
| POST | `/sessions` | API key | Create a stateful session (Solari sandbox + kernel ctx) |
| GET | `/sessions/{session_id}` | API key (owner) | Session status/metadata |
| DELETE | `/sessions/{session_id}` | API key (owner) | Kill the session's sandbox |
| GET | `/sessions/{session_id}/files/{filename}` | API key (owner) | Download an artifact file |
| GET | `/health` | none | Liveness + Solari configuration status |

`POST /keys/request` body: `{"email": "optional@example.com", "note": "optional free-text"}`.
Response: the full key string **once** (`"api_key": "ek_..."`), plus the
key object. Rate limit: per-IP, configurable (default 5/day) — implemented with
a simple DB counter in v1, not a separate limiter service.

`GET /health` response: `{"status": "ok", "version": "...", "solari": {"configured": true}}`
— always HTTP 200 when the app is up (Solari misconfiguration is reported in
the body, not the status code, so orchestrators distinguish app-down from
misconfigured).

### Execution request body

```json
{
  "code": "print('hello')",
  "session_id": null
}
```

- `code` (string, required, max 100_000 chars).
- `session_id` (string|null, optional) — run inside an existing session's
  kernel instead of a fresh one-shot sandbox.

Optional v1 fields (accepted, safe to ignore by clients): none — keep it at
exactly this shape for v1.

### Execution response body

```json
{
  "execution_id": 1,
  "session_id": "optional",
  "stdout": "",
  "stderr": "",
  "exit_code": 0,
  "status": "completed",
  "error": null,
  "artifacts": [],
  "credits_remaining": 24
}
```

- `execution_id` — local DB id (integer, monotonic).
- `session_id` — echoed when the execution ran in a session; `null` for one-shot.
- `stdout` / `stderr` — joined stream text from `RunCodeResult.results` items
  (verified: there is no top-level `.stdout`).
- `exit_code` — `0` on success, `1` when the code raised, `null` when there is
  no meaningful code (timeout, infrastructure failure).
- `status` — one of the statuses below.
- `error` — human-readable message string or `null` (structured errors live in
  the top-level error envelope on failures; see Error model).
- `artifacts` — list of `{"filename": str, "size": int}` for files found in the
  sandbox artifact directory after the run. Contents are fetched via the files
  endpoint.
- `credits_remaining` — balance after this execution was debited.

### Execution statuses

| Status | Meaning | HTTP |
| --- | --- | --- |
| `completed` | Code ran; exit semantics in `exit_code`/`stderr` | 200 |
| `failed` | Code raised / non-zero kernel outcome | 200 |
| `timeout` | ExecKit's app-side deadline hit (see Session lifecycle) | 200 |
| `solari_unconfigured` | ExecKit has no `SOLARI_API_KEY` set | 503 |
| `solari_unavailable` | Solari infra error (auth rejected, capacity, gateway) — credit refunded | 503 |
| `unsupported` | Requested feature not in v1 (e.g. non-Python language) | 400 |

`completed`/`failed`/`timeout` are *user-code outcomes* (HTTP 200 — the API
worked; the code didn't). `solari_*`/`unsupported` are *service outcomes*
(non-200) and do **not** consume credits (see Credit model).

### Error response shape

Every non-2xx response:

```json
{
  "error": {
    "code": "insufficient_credits",
    "message": "You need more credits to run executions.",
    "details": {}
  }
}
```

Error codes (stable strings, v1 set):

| Code | HTTP | When |
| --- | --- | --- |
| `invalid_request` | 400 | Malformed body, oversized code |
| `unsupported` | 400 | Requested capability not in v1 |
| `unauthorized` | 401 | Missing/invalid/inactive API key |
| `insufficient_credits` | 402 | Balance < 1 credit at admission |
| `not_found` | 404 | Unknown execution/session/file, or session owned by another key |
| `rate_limited` | 429 | `/keys/request` IP limit hit |
| `solari_unconfigured` | 503 | Server lacks Solari credentials |
| `solari_unavailable` | 503 | Solari error while executing (credit refunded) |

## E. Database tables

SQLite via SQLAlchemy (async driver `aiosqlite`) for v1; the schema is written
to be portable to Postgres later. All timestamps are UTC `DateTime`.

### `api_keys`

| Column | Type | Notes |
| --- | --- | --- |
| id | INTEGER PK | |
| key_hash | TEXT UNIQUE NOT NULL | SHA-256 of the full `ek_...` key |
| last4 | TEXT NOT NULL | last 4 chars for display (`••••abcd`) |
| email | TEXT NULL | optional |
| plan | TEXT NOT NULL DEFAULT 'free' | `free` \| `starter` \| `pro` (v1 treats them identically except price/tag) |
| credits | INTEGER NOT NULL DEFAULT 0 | current balance |
| executions_count | INTEGER NOT NULL DEFAULT 0 | lifetime counter |
| is_active | BOOLEAN NOT NULL DEFAULT TRUE | admin kill switch |
| note | TEXT NULL | free-text |
| created_at / updated_at | DATETIME NOT NULL | UTC |

### `sessions`

| Column | Type | Notes |
| --- | --- | --- |
| id | TEXT PK | ExecKit's opaque id (`sess_` + 22 url-safe chars) |
| api_key_id | INTEGER FK → api_keys.id NOT NULL | owning key |
| solari_sandbox_id | TEXT NOT NULL | `sandbox.sandboxId` |
| context_id | TEXT NULL | kernel context id; set right after creation, cleared if found invalid on reattach |
| status | TEXT NOT NULL | `active` \| `killed` \| `expired` \| `lost` |
| created_at | DATETIME NOT NULL | |
| last_used_at | DATETIME NOT NULL | bumped on each execution; drives reaping |
| killed_at | DATETIME NULL | |

### `executions`

| Column | Type | Notes |
| --- | --- | --- |
| id | INTEGER PK | `execution_id` |
| api_key_id | INTEGER FK NOT NULL | |
| session_id | TEXT NULL FK → sessions.id | null for one-shot |
| code | TEXT NOT NULL | |
| stdout | TEXT NOT NULL DEFAULT '' | |
| stderr | TEXT NOT NULL DEFAULT '' | |
| exit_code | INTEGER NULL | |
| status | TEXT NOT NULL | execution status enum above |
| error | TEXT NULL | |
| duration_ms | INTEGER NULL | app-measured |
| credits_charged | INTEGER NOT NULL DEFAULT 0 | 0 or 1 |
| created_at | DATETIME NOT NULL | |

### `credit_ledger`

Append-only; every credit change gets exactly one row, and `api_keys.credits`
is always the sum of the ledger (reconcilable).

| Column | Type | Notes |
| --- | --- | --- |
| id | INTEGER PK | |
| api_key_id | INTEGER FK NOT NULL | |
| delta | INTEGER NOT NULL | negative = spend, positive = grant/refund |
| reason | TEXT NOT NULL | `signup_grant` \| `execution` \| `solari_refund` \| `admin_grant` |
| execution_id | INTEGER NULL FK | set for `execution`/`solari_refund` |
| balance_after | INTEGER NOT NULL | written in the same transaction |
| created_at | DATETIME NOT NULL | |

### `key_request_rates`

| Column | Type | Notes |
| --- | --- | --- |
| ip_hash | TEXT PK | SHA-256 of request IP + server salt |
| day | TEXT NOT NULL | `YYYY-MM-DD` UTC |
| count | INTEGER NOT NULL | |

## F. Session lifecycle

```
POST /sessions
  → quota check (credits ≥ 1)                 [402 if not]
  → SandboxClient.create(template="base", timeout_ms=SESSION_IDLE_MS, metadata={"exekit_key_id": ...})
  → sandbox.connect(); ctx = sandbox.create_code_context("python")
  → DB row: status=active, solari_sandbox_id, context_id
  → 201 {"session_id": "sess_...", "status": "active", "created_at", "last_used_at"}

POST /executions {"session_id": "sess_..."}
  → ownership + status=active check           [404 otherwise]
  → attach: client.connect(sandbox_id) + sandbox.connect()
      - failure → try one fresh create_code_context() on reattach; if the
        sandbox is gone (state gone/releasing) → mark session lost, return
        404 {"code":"not_found","details":{"reason":"session_lost"}} (credit refunded)
  → run_code(code, context_id=ctx) under asyncio.wait_for(EXEC_TIMEOUT_S)
  → bump last_used_at; set_timeout(SESSION_IDLE_MS) to reset Solari's rolling idle window
  → response with session_id echoed

DELETE /sessions/{id}
  → ownership check → sandbox.kill() (best effort) → status=killed, killed_at
  → 204. The sandbox dies even if Solari is unreachable (logged for reaper).

Reaper (background task, every 60s):
  → kill DB sessions with last_used_at older than SESSION_TTL (default 30 min)
  → mark sessions expired; kill their sandboxes; kill any DB-active sessions
    whose sandbox is already gone client.get() → state in (gone, releasing)
```

Key timeout facts driving this design (verified — see SDK surface doc §J):
Solari's `timeout_ms` is a **rolling idle window**, not a hard deadline;
`run_code` has **no timeout parameter**. Therefore ExecKit enforces the hard
per-execution deadline with `asyncio.wait_for` (default 30s, cap 120s), and on
timeout **kills the one-shot sandbox** (or marks a session `lost` and kills it),
because the kernel cannot be interrupted. Cold start is ~1s (snapshot boot), so
one-shot executions create a sandbox per execution and always kill it in a
`finally:` block.

Concurrency: v1 uses a global semaphore (default 4 concurrent sandboxes,
configurable) — requests beyond it wait briefly, then return
`solari_unavailable` with details `{"reason": "capacity"}`.

## G. Credit / quota model

- **Unit:** 1 credit = 1 execution, regardless of runtime. Sessions cost
  nothing to *create* but each execution inside them costs 1 credit (v1
  simplicity; per-second pricing is a future extension point).
- **Admission:** atomic check-and-debit (`UPDATE api_keys SET credits = credits
  - 1, executions_count = executions_count + 1 WHERE id = ? AND credits >= 1`
  + ledger row in the same transaction). No read-then-write races.
- **Refunds:** if the execution ends in `solari_unavailable`,
  `solari_unconfigured`, `timeout` *caused by Solari infra*, or the session is
  lost, the credit is refunded as a new ledger row (`solari_refund`,
  `+1`). User-code failures (`failed`, user-code `timeout`) are **not**
  refunded.
- **Exhaustion:** HTTP 402 with code `insufficient_credits`. The error
  `details` includes `{"credits_remaining": 0}`.
- **Ledger integrity:** every delta is recorded with `balance_after`;
  `scripts/add_credits.py` writes `admin_grant` rows.
- **Plans (v1):** `free` (signup grant: 25 credits), `starter` (500), `pro`
  (5000). Plans only differ in the grant an admin gives; no enforcement code.
  This keeps the Stripe phase a data change, not a schema change.

## H. Error model

- All errors use the single envelope from §D. `code` is a stable string;
  `message` is human-readable and safe to show; `details` is a free object.
- Unknown exceptions are logged server-side and returned as
  `500 {"code":"internal","message":"Internal error"}` — never leaking
  tracebacks.
- Mapping from Solari SDK exceptions (all verified subclasses of
  `SolariError`, see SDK surface doc): `AuthError` → `solari_unavailable`
  (details: `reason: "solari_auth"` — the *server's* key is broken), any
  `GatewayError`, `NoCapacityError`, `ConcurrencyLimitError`,
  SDK `TimeoutError`, `ConnectionError`, `OSError` from transport →
  `solari_unavailable`. A missing `SOLARI_API_KEY` env var →
  `solari_unconfigured` *before* any debit.
- Idempotency: none in v1 (documented non-goal). Executions are POSTs that
  clients must not blindly retry; the retry cost is 1 credit.

## I. Future billing extension points

1. **`plan` column already exists** on `api_keys` — Stripe phase maps
   price IDs → plan names without schema change.
2. **`credit_ledger` is append-only with `reason`** — add reasons
   `stripe_purchase`, `subscription_grant` without touching existing rows.
3. **`/keys/me` response** gains `billing` object (stripe_customer_id,
   payment_method_status, next_invoice) — additive JSON.
4. **New routes** (`POST /billing/checkout`, `POST /billing/webhook`) mount
   cleanly; webhook signature verification middleware is route-scoped.
5. **Paid enforcement:** `plan` + a `monthly_included_credits` lookup table is
   the only new state needed; quota admission logic (§G) is unchanged because
   it only reads `credits`.
6. **SQLite → Postgres** migration: schema is deliberately portable (no
   SQLite-only types); `db.py` abstracts the engine/URL.

## J. Non-goals for the first version

1. **No streaming output** — the SDK doesn't stream `run_code` output
   (verified); v1 is request/response.
2. **No non-Python languages** in the API (kernel supports them; not exposed,
   mapped to `unsupported`).
3. **No code editing beyond the playground textarea** — no notebooks, no file
   upload UI, no package-install UX.
4. **No multi-tenancy beyond API keys** — no teams, orgs, roles, or sharing.
5. **No Stripe billing** — credits come from signup grants and admin scripts.
6. **No self-serve top-ups** — 402 responses tell the user to contact the
   admin email.
7. **No custom sandbox templates, snapshots, volumes, or port previews** —
   fixed `template="base"`.
8. **No rich output (png/html charts) in v1** — stdout/stderr/text only;
   `CodeResultItem` rich fields are dropped (recorded as an extension point).
9. **No idempotency keys, webhooks, or async job queue** — synchronous
   executions only (bounded by the 120s cap).
10. **No rate limiting on paid endpoints** — only `/keys/request` is
    IP-limited in v1; credits are the throttle.
