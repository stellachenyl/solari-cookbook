# ExecKit

ExecKit is a Solari-powered execution API for AI agents, automations, and untrusted Python workloads.

One `POST /executions` call runs AI-generated Python inside an isolated
[Solari](https://getsolari.com) sandbox microVM and returns stdout, stderr,
exit status, and file artifacts — with hashed API keys, a credit ledger, and
stateful sessions built in. This directory contains both the FastAPI service
and a browser playground for it.

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
| `SOLARI_API_KEY` | *(empty)* | Solari key from [console.getsolari.com](https://console.getsolari.com). Empty = the app boots, but executions return `503 solari_unconfigured` (with the credit refunded). |
| `ADMIN_TOKEN` | `change-me` | Header token for admin operations. Must be non-empty unless `DEBUG=true`. |
| `DATABASE_URL` | `sqlite:///./exekit.db` | SQLModel/SQLAlchemy URL (SQLite ships; Postgres-ready later). |
| `EXECUTION_TIMEOUT_SECONDS` | `15` | Hard per-execution deadline, enforced app-side. |
| `FREE_CREDITS` | `25` | Signup grant for new keys. |
| `MAX_OUTPUT_CHARS` | `100000` | stdout/stderr truncation cap. |
| `APP_BASE_URL` | `http://localhost:8000` | Advertised base URL. |
| `DEBUG` | `false` | `true` logs exception details (never returned to clients in production mode). |

## Run

```bash
make dev              # uvicorn app.main:app --reload --port 8000
```

- Playground: <http://localhost:8000/> — get a key, run code, manage sessions.
- API docs (Swagger): <http://localhost:8000/docs>
- Health: <http://localhost:8000/health>

## Create keys

From the UI ("Get free API key") or the CLI:

```bash
make key                                        # new key, FREE_CREDITS grant
python scripts/create_key.py --email you@example.com --note "friend"
python scripts/add_credits.py --key-id 1 --amount 100 --reason topup
```

The raw key (`ek_live_…`) is printed **once**; the database stores only a
SHA-256 hash and the last 4 characters.

## Run smoke tests

With the server running:

```bash
make test             # python scripts/smoke_test.py
```

Prints PASS/FAIL per check against <http://localhost:8000> and exits non-zero
on failure. Tests adapt to whether `SOLARI_API_KEY` is configured: with a key
they exercise a real execution and the full session create/get/delete loop;
without one they verify clean `503 solari_unconfigured` responses and that
credits are refunded.

## API sketch

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/keys/request` | – | Issue a key (raw key returned once) |
| GET | `/keys/me` | API key | Plan, credits, usage |
| POST | `/executions` | API key | Run Python (one-shot or `session_id`) |
| POST / GET / DELETE | `/sessions…` | API key | Stateful kernel sessions |
| GET | `/sessions/{id}/files/{name}` | API key | Download an artifact |
| GET | `/health` | – | Liveness + Solari config status |

Full contract: [docs/EXEKIT_SPEC.md](../docs/EXEKIT_SPEC.md). Error responses
always use `{"error": {"code", "message", "details"}}`.

## Current limitations

- Executions are synchronous, capped at `EXECUTION_TIMEOUT_SECONDS`; a timed-out
  session sandbox is destroyed (kernels cannot be interrupted).
- Sessions live in the running process; a server restart orphans them until
  the reaper (next phase) cleans up. One-shot runs are unaffected.
- Only stdout/stderr/text artifacts surface in v1; rich kernel output (plots,
  HTML) is captured by the SDK but not yet exposed.
- Python only; the kernel supports other languages but the API doesn't yet.
- No Stripe billing yet: credits come from the signup grant and
  `scripts/add_credits.py`.

## Billing (planned)

Stripe is the next phase. The schema is ready for it: `plan` lives on every
key, the credit ledger is append-only with typed reasons, and checkout/webhook
routes can mount without changing the quota logic. See
[docs/EXEKIT_SPEC.md §I](../docs/EXEKIT_SPEC.md) for the extension points.
