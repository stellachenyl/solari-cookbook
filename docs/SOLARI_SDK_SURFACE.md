# Solari SDK Surface (verified)

Everything ExecKit needs from the Solari Python SDK, verified against the
installed package. **Verification method:** `pip install solari-sandbox`
installed `solari-sandbox 0.2.0` + `solari-core 0.2.0` from PyPI, and every
signature below was read from the live package with `inspect.signature` /
`inspect.getsource` on 2026-09-02, cross-checked against
`examples/sandbox-code-interpreter-py/` and `examples/sandbox-quickstart-ts/`
in this repo. Nothing here is invented; anything not confirmable is marked
UNVERIFIED or UNSUPPORTED.

> Repo layout note: the brief mentioned `examples/sandbox-quickstart-py`, but
> the repo has **`examples/sandbox-quickstart-ts`** (TypeScript) and
> **`examples/sandbox-code-interpreter-py`** (Python). There is no Python
> quickstart. The TS example is the reference for commands/files; the Python
> example is the reference for `run_code` and code contexts. Both are cited
> below.

---

## A. Exact import statements

```python
# The sandbox client package (pip install solari-sandbox)
from solari_sandbox import SandboxClient

# Also available from the same package (re-exported from solari_core):
from solari_sandbox import (
    SyncSandboxClient,        # blocking wrapper around SandboxClient
    Sandbox,                  # live session handle returned by create()/connect()
    SandboxView,              # result of client.get()
    CreateSandboxResponse,
    RunCodeResult,            # result of sandbox.run_code()
    CodeResultItem,           # one stdout/stderr/result item inside RunCodeResult
    CommandResult,            # result of sandbox.commands.run()
    FsEntry,                  # files.list() entry: name, dir, size
    FsStat,                   # files.stat() entry: name, dir, size, mode, modTimeMs
    # Errors:
    SolariError,              # base class of every SDK error
    AuthError,                # 401 — bad/missing API key
    GatewayError,             # any other non-2xx from the gateway (has .status)
    TimeoutError,             # SDK per-call RPC timeout (shadows builtin!)
    ConnectionError,          # control WebSocket not open (shadows builtin!)
    NoCapacityError,          # 503 — no host available
    ConcurrencyLimitError,    # 429 — too many live sandboxes
    PlanError,                # 402 FeatureRequiresPlan from Solari's own plans
)
```

Packaging facts (verified): the PyPI distribution is `solari-sandbox`; it
installs two modules, `solari_sandbox` and `solari_core`. The example's
`requirements.txt` pins `solari-sandbox>=0.2.0`. There is **no lockfile and no
pyproject.toml** anywhere in this repo; examples use bare `requirements.txt`
(Python) or `package.json` (TypeScript).

---

## B. Exact client initialization

`SandboxClient.__init__(self, *, api_key: str, base_url: str, http: Optional[httpx.AsyncClient] = None, call_timeout_ms: Optional[int] = None, kind: SandboxKind = 'sandbox')`

All parameters are keyword-only. `base_url` is **required** — only the
TypeScript umbrella `@solarisdk/sdk` `SolariClient` defaults it (see
`examples/sandbox-code-interpreter-py/main.py` line 17–19 comment).

```python
import os
from solari_sandbox import SandboxClient

client = SandboxClient(
    api_key=os.environ["SOLARI_API_KEY"],
    base_url="https://api.getsolari.com",
)
```

It is usable as an async context manager (the code-interpreter example wraps it
in `async with SandboxClient(...) as client:`) and exposes `.aclose()`.

For a blocking context (not needed by ExecKit's async FastAPI stack, listed for
completeness):

```python
from solari_sandbox import SyncSandboxClient
client = SyncSandboxClient(api_key=..., base_url="https://api.getsolari.com")
client.close()
```

Python umbrella client with a defaulted base_url: **UNVERIFIED** — the repo
only ships an umbrella for TypeScript (`@solarisdk/sdk`, see
`examples/sandbox-quickstart-ts/package.json`). ExecKit will always pass
`base_url` explicitly, so this does not block.

---

## C. Running Python code / commands

Two verified paths.

### C.1 `run_code` — stateful kernel (primary path for ExecKit)

`Sandbox.run_code(self, code: str, *, language: Optional[CodeLanguage] = None, context_id: Optional[str] = None, on_stdout: Optional[Callable[[str], None]] = None, on_stderr: Optional[Callable[[str], None]] = None) -> RunCodeResult`

`CodeLanguage` is `Literal['python', 'javascript', 'typescript', 'bash', 'r']`
(default `'python'` via `create_code_context`).

```python
result = await sandbox.run_code("print('hello')")                     # one-shot
result = await sandbox.run_code(code, context_id=ctx)                 # stateful
```

`RunCodeResult` (verified dataclass):

```python
@dataclass
class RunCodeResult:
    results: list[CodeResultItem]   # typed output items, see below
    error: Any = None               # truthy when the code raised / failed
    charts: list[Chart] = []        # flattened chart convenience view

@dataclass
class CodeResultItem:
    type: Literal["stdout", "stderr", "result"]
    text: Optional[str] = None      # used by stdout/stderr items and text results
    png: Optional[str] = None       # base64 rich-media variants
    jpeg: Optional[str] = None
    svg: Optional[str] = None
    html: Optional[str] = None
    latex: Optional[str] = None
    json: Any = None
    markdown: Optional[str] = None
    chart: Optional[Chart] = None
```

**There is no top-level `.stdout`.** Output arrives as the `results` list:
items with `type == "stdout"` / `"stderr"` carry stream text in `.text`,
the final expression value arrives as a `"result"` item
(`examples/sandbox-code-interpreter-py/main.py` lines 46–53 and its README).

**`run_code` has no timeout parameter** (verified from the signature). ExecKit
must enforce execution deadlines itself by wrapping the await in
`asyncio.wait_for(...)` at the application layer. The `on_stdout`/`on_stderr`
callbacks fire while the completed RPC response is parsed (per the
`SessionHandle.run_code` source), i.e. after the call finishes — they are not
incremental streaming. Incremental stdout streaming: **UNSUPPORTED** in v0.2.0.

### C.2 `commands.run` — process execution (fallback / admin path)

`Sandbox.commands.run(self, cmd: str, *, args: Optional[List[str]] = None, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None, user: Optional[str] = None, timeout_ms: Optional[int] = None, background: bool = False, on_stdout: Optional[Callable[[str], None]] = None, on_stderr: Optional[Callable[[str], None]] = None) -> CommandResult`

```python
out = await sandbox.commands.run("python3", args=["-c", "print(sum(range(101)))"])
# CommandResult(exitCode: int, stdout: str, stderr: str)
```

**Gotcha (README + both sandbox examples): commands are NOT shell-interpreted.**
`run("ls -la")` looks for a binary literally named `ls -la`. Put argv in
`args`, or run a shell explicitly: `run("sh", args=["-c", "..."])`.
`commands.run` waits for process exit; long-running servers need `background=True`
or `nohup ... &` inside a shell (`examples/sandbox-port-preview-ts/index.ts`).

---

## D. Creating / reusing a session

A Solari "session" for our purposes = a live sandbox (+ a kernel context id
stored by ExecKit).

**Create** (verified full signature of `SandboxClient.create`):

```python
sandbox = await client.create(
    template="base",          # Optional[str]
    timeout_ms=5 * 60_000,    # rolling IDLE window, see gotcha J3
    # also available: cpu, mem_mb, disk_gb, envs: Dict[str,str],
    #                  metadata: Dict[str,str], from_snapshot, lifecycle, volumes
)
print(sandbox.sandboxId)      # also .id, .controlUrl, .expiresAt
await sandbox.connect()       # opens the control WebSocket
```

**Reuse / re-attach** (verified `SandboxClient.connect` source: it calls
`self.get(sandbox_id)` and rebuilds a handle with a control URL):

```python
sandbox = await client.connect(sandbox_id)   # re-attach to a running sandbox by id
await sandbox.connect()
```

**Kernel context (statefulness across `run_code` calls):**

```python
ctx = await sandbox.create_code_context("python")   # -> context_id: str
await sandbox.run_code("import math\nradius = 7", context_id=ctx)
await sandbox.run_code("print(radius)", context_id=ctx)   # variables survive
```

Omitting `context_id` starts a fresh kernel each call
(`examples/sandbox-code-interpreter-py/main.py` lines 29–33).

**Inspecting / listing:**

```python
view = await client.get(sandbox_id)   # SandboxView(sandboxId, kind, state, metadata, expiresAt, cpu, memMb)
listing = await client.list(metadata={...}, state=..., limit=..., cursor=...)  # Dict[str, Any]
all_views = await client.list_all(metadata={...})                              # List[SandboxView]
```

`SandboxState = Literal['starting', 'running', 'paused', 'archived', 'releasing', 'gone']`.

Whether a kernel `context_id` survives a disconnect/re-attach cycle
(`client.connect(sandbox_id)` after a new process) is **UNVERIFIED** — the SDK
docs in this repo don't cover it. ExecKit will verify at runtime (Phase 2
`scripts/inspect_solari.py`) and treat "context missing" as recoverable by
creating a fresh context on the same sandbox.

---

## E. Killing / closing a session

```python
await sandbox.kill()          # destroys the remote VM AND closes the local channel
# or, client-side without a handle:
await client.kill(sandbox_id)
```

**Gotcha (README + both sandbox examples): `kill()`, not `close()`, ends a VM.**
`await sandbox.close()` only drops the local control channel — the VM keeps
running until its idle timeout. The code-interpreter example kills in a
`finally:` block. `sandbox.set_timeout(timeout_ms)` (returns `Dict[str, Any]`)
resizes the idle window on a live session.

---

## F. Writing files into the sandbox

`Sandbox.files` (verified `_Files` methods):

```python
await sandbox.files.write(path, data)        # data: bytes | str, optional mode: int
await sandbox.files.upload(path, data)       # data: bytes | str
await sandbox.files.mkdir(path)
# Large-file alternative (verified; requires a client-created handle):
info = await sandbox.upload_url(path)        # -> Dict[str, Any]: signed upload URL
```

`files.write(path, "text")` and `files.write(path, b"bytes")` are both accepted
(`examples/sandbox-quickstart-ts/index.ts` shows the TS equivalent).

---

## G. Reading files from the sandbox

```python
data: bytes = await sandbox.files.read(path)
text: str   = await sandbox.files.read_text(path)
data: bytes = await sandbox.files.download(path)
entries     = await sandbox.files.list(path)      # List[FsEntry(name: str, dir: bool, size: int)]
st          = await sandbox.files.stat(path)      # FsStat(name, dir, size, mode, modTimeMs)
info = await sandbox.download_url(path)           # -> Dict[str, Any]: signed download URL
```

Extras available but not needed for v1 ExecKit: `remove(path, recursive=False)`,
`rename(frm, to)`, `search(path, query, max_results)`, `watch(path, cb, recursive=False)`.

---

## H. Capturing stdout/stderr

- **`run_code`:** parse `result.results` — items with `type == "stdout"` /
  `"stderr"` carry `.text`. The optional `on_stdout` / `on_stderr` callbacks
  receive the same text during response parsing (not incremental).
- **`commands.run`:** `CommandResult.stdout` / `CommandResult.stderr` strings
  plus `exitCode`. Optional `on_stdout` / `on_stderr` callbacks exist here too.

For ExecKit's `POST /executions` response, stdout/stderr are assembled by
joining `type == "stdout"` / `"stderr"` item texts from `RunCodeResult.results`.

---

## I. Required API key environment variable

```
SOLARI_API_KEY=slr_live_...
```

From every example's `.env.example` and the root README ("grab one at
console.getsolari.com"). The SDK does **not** read it automatically — ExecKit
passes it explicitly to `SandboxClient(api_key=...)`. One `slr_live_` key works
across browsers, sandboxes, and desktops and bills to one balance (root README).

---

## J. Gotchas (from the repo README and examples)

1. **`base_url` is required** on the standalone Python `SandboxClient`; only
   the TS umbrella client defaults it.
2. **Commands are not shell-interpreted.** `run("ls -la")` looks for a binary
   named `ls -la`. Use `args=[...]` or `run("sh", args=["-c", "..."])`.
3. **`timeout_ms` is a rolling idle window**, not a hard deadline — it resets
   on every use. ExecKit cannot rely on it for per-execution timeouts; it must
   wrap awaits in `asyncio.wait_for` (see C.1).
4. **`kill()`, not `close()`, ends a VM.** `close()` only drops the local
   control channel. Always kill in a `finally:` block.
5. **No top-level `.stdout` on `run_code` results** — output is a list of typed
   items (`stdout`/`stderr`/`result`).
6. **Call `sandbox.connect()` before files/git/code RPCs** — the control
   WebSocket must be open. (TS comment: bare commands can take a one-shot HTTP
   path without it; `run_code` cannot — it is a control-channel RPC.)
7. **`commands.run` waits for process exit.** Foreground servers block until
   the idle timeout; use `background=True` or `nohup ... &`.
8. **Sandboxes boot from a memory snapshot in about a second** (root README) —
   acceptable cold-start for one-shot executions.
9. **TypeScript-only gotcha:** the TS browser client needs `await solari.close()`
   or the script hangs. Not applicable to the Python sandbox package, but the
   Python analog is: close the client (`async with` / `aclose()`) so httpx
   connections don't leak.
10. **`solari_sandbox.TimeoutError` and `ConnectionError` shadow the builtins**
    when star-imported — ExecKit should reference them via the module
    (`from solari_sandbox import TimeoutError as SolariTimeoutError`) to avoid
    clobbering builtins.

---

## K. Explicitly unsupported capabilities

| Capability | Status | Note |
| --- | --- | --- |
| Per-call timeout on `run_code` | **UNSUPPORTED** | No `timeout_ms` param on `run_code` (v0.2.0). Enforce app-side with `asyncio.wait_for`. |
| Top-level `.stdout` string on code results | **UNSUPPORTED** | Output is `RunCodeResult.results` item list. |
| Incremental streaming of stdout during `run_code` | **UNSUPPORTED** | Callbacks fire when the RPC response is parsed, after completion. |
| Interrupt/cancel a running kernel | **UNSUPPORTED** | No interrupt method on `SessionHandle` (verified full method list). Timeout handling must kill the sandbox. |
| Python umbrella client with defaulted `base_url` | **UNVERIFIED** | Repo only shows TS umbrella (`@solarisdk/sdk`). Non-blocking; ExecKit passes `base_url` explicitly. |
| `context_id` validity across disconnect/re-attach | **UNVERIFIED** | Verify in Phase 2 via `scripts/inspect_solari.py`; fall back to creating a fresh context. |
| Listing / deleting kernel contexts | **UNSUPPORTED** | Only `create_code_context` exists on the handle. |
| Billing/credits data from Solari | **UNSUPPORTED (irrelevant)** | Solari bills the single `slr_live_` account; ExecKit does its own credit ledger on top. |

## ExecKit adapter summary (the exact calls we will make)

```python
client = SandboxClient(api_key=settings.solari_api_key, base_url="https://api.getsolari.com")

# One-shot execution
sandbox = await client.create(template="base", timeout_ms=...)
await sandbox.connect()
result = await asyncio.wait_for(sandbox.run_code(code), timeout=EXEC_TIMEOUT_S)
await sandbox.kill()

# Stateful session
sandbox = await client.create(template="base", timeout_ms=SESSION_IDLE_MS)
await sandbox.connect()
ctx = await sandbox.create_code_context("python")
# ... later, any worker:
sandbox = await client.connect(sandbox_id)
await sandbox.connect()
result = await sandbox.run_code(code, context_id=ctx)
# ... teardown:
await sandbox.kill()            # or client.kill(sandbox_id) / session reaper

# Artifacts
entries = await sandbox.files.list("/tmp/exekit")
data = await sandbox.files.read(f"/tmp/exekit/{name}")

# Error handling: catch solari_sandbox.SolariError; AuthError/GatewayError/
# NoCapacityError/ConcurrencyLimitError/solari TimeoutError => infra class.
```
