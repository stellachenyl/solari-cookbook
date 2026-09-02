# ExecKit API Reference

As-built reference for the ExecKit HTTP API. For the original product spec see
[EXEKIT_SPEC.md](../../docs/EXEKIT_SPEC.md); for internals see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Conventions

- **Base URL** (local dev): `http://localhost:8000`
- **Content type**: `application/json` for requests and responses, except the
  file-download endpoints and the `/billing/success|cancel` HTML pages.
- **Errors** always use one envelope, rendered by the global handlers in
  `app/main.py` (including FastAPI validation errors):

  ```json
  {"error": {"code": "insufficient_credits",
             "message": "You need more credits to run executions.",
             "details": {"credits_remaining": 0, "request_id": "…"}}}
  ```

- **Request IDs**: every response carries an `X-Request-ID` header; the same
  id appears in server logs and in error `details.request_id`. Send your own
  `X-Request-ID` header to correlate a call across systems.
- **Timestamps** are UTC ISO-8601 strings.
- **Authentication** uses the `X-API-Key` header. Keys look like
  `ek_live_…`, are shown once at creation, and are stored only as a SHA-256
  hash plus the last 4 characters. Invalid or missing keys answer `401`
  (`missing_api_key` / `invalid_api_key`); deactivated keys answer `403`
  (`api_key_inactive`).

## Rate limits

Per API key, sliding one-minute windows, enforced in-process:

| Bucket | Default | Applies to |
| --- | --- | --- |
| `executions` | 30/min | `POST /executions` |
| `sessions` | 10/min | `POST /sessions`, `DELETE /sessions/{id}` |
| `requests` | 100/min | other authenticated endpoints (keys, billing, session reads) |

`429` responses use code `rate_limit_exceeded` with
`details.retry_after_seconds`. Set any `RATE_LIMIT_*_PER_MINUTE` to `<= 0` to
disable a bucket.

## Error codes

| Code | HTTP | Meaning |
| --- | --- | --- |
| `invalid_request` | 400 | Malformed body, oversized code (50,000 chars max), bad query params |
| `missing_api_key` / `invalid_api_key` | 401 | No or unknown `X-API-Key` |
| `invalid_admin_token` | 401 | Missing/wrong `X-Admin-Token` on `/admin/*` |
| `api_key_inactive` | 403 | Key was deactivated |
| `insufficient_credits` | 402 | No credits left; `details.credits_remaining` is 0 |
| `not_found` | 404 | Unknown resource — or one owned by another key (indistinguishable on purpose) |
| `invalid_session` / `session_not_found` | 404 | Session unknown or not owned by this key |
| `session_gone` | 410 | Session was killed, or its runner handle was lost |
| `rate_limit_exceeded` | 429 | Bucket limit hit; retry after `details.retry_after_seconds` |
| `billing_disabled` | 503 | Stripe not configured on the server |
| `solari_unconfigured` | 503 | `SOLARI_API_KEY` missing; the credit is refunded |
| `solari_unavailable` | 502 | Solari gateway failed; the credit is refunded |
| `execution_timeout` | 504 | Execution exceeded the deadline |
| `unsupported_capability` | 501 | Backend does not support the requested operation |

## Endpoints

### Health — `GET /health` (no auth)

```json
{"status": "ok", "solari_configured": true, "billing_enabled": false,
 "database": "sqlite", "active_sessions": 2, "time": "2026-09-02T07:44:41Z"}
```

### Keys

`POST /keys/request` (no auth) — issue a key. Optional body:
`{"email": "you@example.com"}`. The raw `api_key` is returned **once**.

```json
{"api_key": "ek_live_…", "key_last4": "91M0", "credits": 25, "plan": "free"}
```

`GET /keys/me` — plan, credits, and usage for the calling key.

```json
{"key_last4": "91M0", "email": "you@example.com", "plan": "free",
 "credits": 24, "executions_count": 1, "is_active": true}
```

### Executions — `POST /executions`

Runs Python (50,000 characters max) in a Solari sandbox. One credit is
consumed before the run and refunded automatically on `solari_unconfigured`
/ `solari_unavailable` (never for user-code failures). Body:

```json
{"code": "print('hi')", "session_id": "sess_…"}
```

`session_id` is optional; when given, the run joins that session's sandbox
(the session must be active and owned by this key). Success (200):

```json
{"execution_id": 1, "session_id": null, "stdout": "hi\n", "stderr": "",
 "exit_code": 0, "status": "completed", "error": null,
 "artifacts": [{"id": 1, "filename": "out.txt", "mime_type": "text/plain",
                "encoding": "base64", "size_bytes": 4,
                "download_available": true}],
 "credits_remaining": 24, "truncated": false}
```

Execution `status` values: `completed`, `failed` (user code exited
non-zero — still 200), `timeout`, `solari_unconfigured`,
`solari_unavailable`, `unsupported`. User-code failures and timeouts cost a
credit and return 200 with the status in the body; infrastructure failures
return 502/503 with a refund.

**Artifacts**: `encoding` is the transfer encoding of the original file
(always `base64`); `download_available` is true only when the content was
small (≤ 100,000 bytes) and UTF-8 text — larger or binary artifacts keep
metadata only. Artifact content is never echoed in API responses.

### Execution history

`GET /executions?limit=20&offset=0&status=completed` — the calling key's
executions, newest first. `limit` 1–100 (default 20), `status` optional
exact match. Listing rows are deliberately lean (no code, no output):

```json
{"executions": [{"id": 3, "session_id": null, "status": "completed",
                 "exit_code": 0, "created_at": "…", "finished_at": "…",
                 "artifact_count": 2}],
 "limit": 20, "offset": 0}
```

`GET /executions/{id}?include_code=false` — full detail: everything above
plus `stdout`, `stderr`, `error`, `duration_ms`, and the artifact list. The
stored source code is returned only with `include_code=true`. Unknown or
foreign executions are the same `404 not_found`.

`GET /executions/{id}/artifacts` — `{"execution_id": 3, "artifacts": […]}`.

`GET /executions/{id}/artifacts/{artifact_id}/download` — the stored text
content as `text/*` (or `application/octet-stream` for non-text mimes), with
`Content-Disposition: attachment` and `X-Artifact-Filename` headers.
`404 artifact_not_stored` when the content was never kept (too large or
binary).

### Sessions

Stateful kernel sessions: variables and imports persist between runs.

| Method & path | Purpose |
| --- | --- |
| `POST /sessions` → 201 | Create a sandbox; returns `{"session_id", "status"}` |
| `GET /sessions/{id}` | Status of one owned session |
| `DELETE /sessions/{id}` | Kill it and destroy the sandbox → `{"session_id", "status"}` |
| `GET /sessions/{id}/files/{name}` | Download a file from the sandbox artifact dir (`application/octet-stream`) |

Filenames may not contain path separators or `.`/`..` entries (traversal is
rejected with `invalid_request`). Idle sessions are reaped after
`SESSION_IDLE_TIMEOUT_MINUTES` (default 30) by a background task.

### Billing

`GET /billing/config` (no auth) — `{"billing_enabled", "credit_amount",
"price_usd", "currency"}`. Billing is enabled exactly when
`STRIPE_SECRET_KEY` is set.

`POST /billing/checkout` — creates a Stripe Checkout Session for
`STRIPE_CREDIT_AMOUNT` credits at `STRIPE_CREDIT_PRICE_USD`; returns
`{"checkout_url"}`. The amount always comes from server config — never from
the client.

`GET /billing/success` / `GET /billing/cancel` — static result pages that
redirect back to the playground.

`GET /billing/ledger` — the calling key's latest 20 ledger entries:

```json
{"key_last4": "91M0", "credits": 24,
 "ledger": [{"amount": -1, "balance_after": 24, "reason": "execution",
             "created_at": "…"}]}
```

Ledger reasons are caller-supplied strings; the built-ins are
`initial_free_credits`, `execution`, `execution_refund`,
`manual_admin_grant` (admin/CLI top-ups), and `stripe_purchase`.

`POST /stripe/webhook` — Stripe webhook receiver. Credits are granted only
after a verified `checkout.session.completed` event (HMAC signature checked
when `STRIPE_WEBHOOK_SECRET` is set); every processed event id is recorded in
`stripe_webhook_events`, so replayed or duplicate deliveries are ignored.

### Admin — requires `X-Admin-Token` header

All answers are raw dicts; wrong/missing tokens get
`401 invalid_admin_token` ("Invalid admin token.").

| Endpoint | Returns |
| --- | --- |
| `GET /admin/stats` | Key/execution/session/credit/Stripe counters |
| `GET /admin/keys?limit&offset` | All keys (last4, email, plan, credits, active) |
| `GET /admin/executions?limit&offset&status` | All executions |
| `GET /admin/sessions?limit&offset` | All sessions |
| `GET /admin/ledger?limit&offset` | Full credit ledger |
| `GET /admin/stripe-events?limit&offset` | Processed webhook events |
| `POST /admin/keys/{id}/credits` `{"amount", "reason"?}` | Grant credits (ledgered) |
| `POST /admin/keys/{id}/toggle-active` | Enable/disable a key |

### Playground

`GET /` serves the browser playground (`app/static/`); `GET /admin` serves
the admin dashboard; both are vanilla JS with no build step.
