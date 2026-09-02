# ExecKit — File Plan

Exact planned file tree for the implementation. Baseline from the brief,
adjusted in two places (marked ⬚ below): `app/api_keys.py` service renamed to
`app/keys_service.py` (a module named `api_keys` inside `services/` is easily
confused with the `api_keys` DB table model and `routes/keys.py`), and three
support files added (`admin router`, `reaper`, `pytest config/tests`) because
the spec requires admin tooling, session reaping, and Phase 6 tests — nothing
else was added or removed.

Everything under `exekit/` is new; nothing in the existing cookbook examples
is modified.

```
exekit/
  app/
    __init__.py
    main.py               # FastAPI app factory, router mounting, reaper startup hook
    config.py             # pydantic-settings: SOLARI_API_KEY, SOLARI_BASE_URL, DB URL, timeouts, credits defaults
    db.py                 # async SQLAlchemy engine/sessionmaker, init_db(), transactional helpers
    models.py             # ORM: ApiKey, Session, Execution, CreditLedger, KeyRequestRate
    schemas.py            # pydantic request/response models (spec §D bodies, error envelope)
    auth.py               # key generation (ek_...), hashing, verify, FastAPI security dependency
    dependencies.py       # get_db, get_current_key, require_active_key, http-error helpers
    routes/
      __init__.py         # api_router aggregation
      health.py           # GET /health
      keys.py             # POST /keys/request, GET /keys/me
      executions.py       # POST /executions
      sessions.py         # POST/GET/DELETE /sessions..., GET /sessions/{id}/files/{name}
      pages.py            # GET / → playground (serves static/index.html)
      admin.py            # (added) localhost-only: list keys, grant credits — wraps scripts
    services/
      __init__.py
      solari_runner.py    # the ONLY module importing solari_sandbox; create/attach/run_code/kill, error mapping
      quota.py            # atomic credit debit/refund + credit_ledger writes
      keys_service.py     # (renamed from api_keys.py) key issuance, /keys/me payload, /keys/request rate limit
      sessions.py         # session creation, reattach, kill, reaper logic
      executions.py       # orchestration: quota → runner → artifacts → ledger → response shaping
    static/
      index.html          # playground UI
      app.js              # fetch calls to /executions, /sessions, /keys/me
      styles.css
  scripts/
    create_key.py         # CLI: issue a key + signup grant (prints full key once)
    add_credits.py        # CLI: admin_grant rows for a key (by last4 or id)
    smoke_test.py         # end-to-end: request key → /executions one-shot → session lifecycle → artifacts
    inspect_solari.py     # verifies Solari config & SDK assumptions (SDK surface doc §D/§K UNVERIFIED items)
  tests/
    __init__.py           # (added) pytest package
    conftest.py           # (added) FakeSolariRunner fixture, temp-DB app fixture
    test_quota.py         # (added) atomic debit, refund, 402, ledger integrity
    test_executions.py    # (added) status mapping, artifact listing, session routing
    test_auth.py          # (added) key hashing, auth headers, inactive keys
  requirements.txt        # fastapi, uvicorn, sqlalchemy[asyncio], aiosqlite, pydantic-settings, httpx, pytest, pytest-asyncio, solari-sandbox>=0.2.0
  .env.example            # SOLARI_API_KEY, SOLARI_BASE_URL, DATABASE_URL, EXEC_TIMEOUT_S, SESSION_IDLE_MS, SESSION_TTL_MIN, MAX_CONCURRENT_SANDBOXES, SIGNUP_GRANT_CREDITS, ADMIN_TOKEN
  Makefile                # install, run, test, smoke, key, credits targets
  README.md               # ExecKit-specific setup/usage (cookbook repo README stays untouched)
```

Notes:
- `requirements.txt` includes `solari-sandbox>=0.2.0` (matches the cookbook
  example's pin; the version we verified against is 0.2.0).
- Tests run against a `FakeSolariRunner` implementing the same narrow
  interface `solari_runner.py` exposes, so the suite never needs a real
  `SOLARI_API_KEY`; `scripts/smoke_test.py` is the real-API check.
- `static/` is served by FastAPI `StaticFiles` mounted at `/app` and
  `pages.py` renders `/`; no build step, no npm.
