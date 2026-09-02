# ExecKit

ExecKit is a Solari-powered execution API for AI agents, automations, and untrusted Python workloads.

One `POST /executions` call runs AI-generated Python inside an isolated
[Solari](https://getsolari.com) sandbox microVM and returns stdout, stderr,
exit status, and file artifacts — with hashed API keys, a credit ledger,
Stripe billing, stateful sessions, execution history, and an admin dashboard
built in. This directory contains the FastAPI service, a browser playground,
and a 237-test suite.

## Local setup

```bash
make install          # python -m venv .venv && pip install -r requirements.txt
cp .env.example .env  # then edit values (see below)
```

Python 3.11+ required. On Windows, use `.venv\Scripts\python.exe` and
`.venv\Scripts\python.exe -m uvicorn …` instead of the Make targets.

## Environment variables (`.env`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `SOLARI_API_KEY` | *(empty)* | Solari key from [console.getsolari.com](https://console.getsolari.com). Empty = the app boots, but executions return `503 solari_unconfigured` (credit refunded). |
| `ADMIN_TOKEN` | `change-me` | Header token for `/admin/*`. Must be non-empty unless `DEBUG=true`. |
| `DATABASE_URL` | `sqlite:///./exekit.db` | SQLModel/SQLAlchemy URL (SQLite ships; Postgres-ready later). |
| `EXECUTION_TIMEOUT_SECONDS` | `15` | Hard per-execution deadline, enforced app-side. |
| `FREE_CREDITS` | `25` | Signup grant for new keys. |
| `MAX_OUTPUT_CHARS` | `100000` | stdout/stderr truncation cap (`truncated: true` beyond it). |
| `APP_BASE_URL` | `http://localhost:8000` | Base URL advertised in Stripe redirects. |
| `DEBUG` | `false` | `true` logs exception details (never returned to clients in production mode). |
| `RATE_LIMIT_EXECUTIONS_PER_MINUTE` | `30` | `POST /executions` per key (`<= 0` disables). |
| `RATE_LIMIT_SESSIONS_PER_MINUTE` | `10` | Session create/delete per key. |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `100` | Other authenticated endpoints per key. |
| `SESSION_IDLE_TIMEOUT_MINUTES` | `30` | Idle sessions reaped by the background task. |
| `SESSION_CLEANUP_INTERVAL_SECONDS` | `300` | Reaper cadence. |
| `STRIPE_SECRET_KEY` | *(empty)* | Setting it enables billing (there is no separate on/off flag). |
| `STRIPE_WEBHOOK_SECRET` | *(empty)* | HMAC secret for webhook signature verification. |
| `STRIPE_CREDIT_PRICE_USD` | `19` | Price of one credit package. |
| `STRIPE_CREDIT_AMOUNT` | `1000` | Credits per purchased package. |

## Run

```bash
make dev              # uvicorn app.main:app --reload --port 8000
```

- Playground: <http://localhost:8000/> — get a key, run code, manage
  sessions, browse execution history and artifacts.
- Admin dashboard: <http://localhost:8000/admin> (needs `ADMIN_TOKEN`).
- API docs (Swagger): <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>

## Create keys

From the UI ("Get free API key") or the CLI:

```bash
make key                                        # new key, FREE_CREDITS grant
python scripts/create_key.py --email you@example.com --note "friend"
python scripts/add_credits.py --key ek_live_… --amount 100 --reason manual_admin_grant
python scripts/simulate_stripe_webhook.py --key-id 1   # fake a checkout webhook locally
```

The raw key (`ek_live_…`) is printed **once**; the database stores only a
SHA-256 hash and the last 4 characters.

## Test

```bash
.venv/bin/python -m pytest -p no:warnings -q   # 237 tests, no network, ~1 min
make test                                      # smoke-test a RUNNING server
```

The pytest suite uses a scripted fake runner and a per-test SQLite database.
`scripts/smoke_test.py` adapts to whether `SOLARI_API_KEY` is configured.

## API sketch

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/keys/request` | – | Issue a key (raw key returned once) |
| GET | `/keys/me` | API key | Plan, credits, usage |
| POST | `/executions` | API key | Run Python (one-shot or `session_id`) |
| GET | `/executions` | API key | Execution history (paginate, filter by status) |
| GET | `/executions/{id}` | API key | Execution detail (`?include_code=true` for source) |
| GET | `/executions/{id}/artifacts` | API key | Artifact metadata for a run |
| GET | `/executions/{id}/artifacts/{aid}/download` | API key | Download stored text artifact |
| POST / GET / DELETE | `/sessions…` | API key | Stateful kernel sessions |
| GET | `/sessions/{id}/files/{name}` | API key | Download a file from the sandbox |
| GET / POST | `/billing/config`, `/billing/checkout`, `/billing/ledger` | mixed | Stripe credit purchases and ledger |
| POST | `/stripe/webhook` | Stripe signature | Credit grants (idempotent) |
| GET / POST | `/admin/*` | X-Admin-Token | Stats, keys, executions, credits, toggles |
| GET | `/health` | – | Liveness + Solari/billing status |

Full contract with request/response examples and the error-code table:
[docs/API.md](docs/API.md). Error responses always use
`{"error": {"code", "message", "details"}}`.

## Documentation

- [docs/API.md](docs/API.md) — as-built HTTP reference (all endpoints, error codes, rate limits)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — modules, data model, request lifecycle, security model
- [../docs/EXEKIT_SPEC.md](../docs/EXEKIT_SPEC.md) — original product spec
- [../docs/SOLARI_SDK_SURFACE.md](../docs/SOLARI_SDK_SURFACE.md) — verified Solari SDK surface used by the runner

## Current limitations

- Executions are synchronous, capped at `EXECUTION_TIMEOUT_SECONDS`; a
  timed-out session sandbox is destroyed (kernels cannot be interrupted).
- Rich kernel output (plots, HTML) is captured by the SDK but surfaced only
  as metadata; artifact downloads cover stored text content.
- Python only; the kernel supports other languages but the API doesn't yet.
- Rate limiting and Stripe webhook verification are in-process / single-node;
  a multi-worker deployment should move the limiter and idempotency to
  shared state (Redis/Postgres).
- SQLite is the shipped database; `DATABASE_URL` is kept configurable for a
  Postgres migration.
