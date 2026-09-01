"""Solari execution adapter — the only module that talks to the Solari SDK.

Every SDK call below is verified against the installed solari-sandbox package
(0.2.0) and documented in docs/SOLARI_SDK_SURFACE.md; comments cite the exact
cookbook example that demonstrates each call. Nothing here relies on unverified
SDK behavior: where a capability is unsupported, the adapter says so
(UnsupportedSolariCapability / empty artifacts) instead of guessing.

Execution outcomes (docs/EXEKIT_SPEC.md §D statuses):
- "completed" / "failed"  — user-code outcomes (kernel error -> failed)
- "timeout"               — app-side deadline hit (Solari's timeout_ms is a
                            rolling idle window, not a deadline, so
                            asyncio.wait_for is the real enforcement)
- "solari_unconfigured"   — server has no SOLARI_API_KEY
- "solari_unavailable"    — Solari infrastructure failure (credit refundable
                            at the route layer)

Sessions: Solari supports string ids (sandbox.sandboxId), so create_session()
returns the Solari id directly — no local UUID mapping is needed (verified:
examples/sandbox-code-interpreter-py/main.py prints sandbox.sandboxId). Handles
live in memory only; after a process restart, sessions are recovered by the
DB layer + client.connect(sandbox_id) re-attach (verified, see SDK surface
doc §D), not by this dict.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

from app.config import get_settings
from app.services.errors import (
    SolariNotConfigured,
    SolariTimeout,
    SolariUnavailable,
    UnsupportedSolariCapability,
)

logger = logging.getLogger("exekit.solari")

# Standalone Python SandboxClient requires base_url explicitly (only the TS
# umbrella client defaults it) — value and necessity from
# examples/sandbox-code-interpreter-py/main.py lines 13/17-22.
_SOLARI_BASE_URL = "https://api.getsolari.com"

# Dedicated, registry-free mime table: the module-level mimetypes.guess_type()
# reads the Windows registry (csv -> vnd.ms-excel etc.), which would make
# artifact mime types platform-dependent. A bare MimeTypes() instance uses only
# the standard library's builtin map.
_MIME_DB = mimetypes.MimeTypes()

# Rolling IDLE window for created sandboxes — resets on every use; it is NOT a
# hard deadline (root README gotchas + examples/sandbox-quickstart-ts/index.ts
# lines 15-17). Hard execution deadlines are enforced with asyncio.wait_for.
_SESSION_IDLE_WINDOW_MS = 15 * 60_000

# Budgets for auxiliary RPCs (boot, artifact reads, kill) so nothing can hang
# the API even if Solari stops responding.
_BOOT_BUDGET_S = 30
_AUX_RPC_BUDGET_S = 10

__all__ = [
    "SolariArtifact",
    "SolariExecutionResult",
    "SolariRunner",
    "UnsupportedSolariCapability",
]

# --- optional SDK import -----------------------------------------------------
# The app must boot even when the Solari SDK is missing; health_check() reports
# the failure instead of crashing the process.
try:
    # solari_sandbox.TimeoutError/ConnectionError shadow builtins when
    # star-imported (SDK surface doc gotcha §J10) — import with explicit aliases.
    from solari_sandbox import ConnectionError as SdkConnectionError
    from solari_sandbox import SandboxClient
    from solari_sandbox import SolariError as SdkSolariError

    SOLARI_SDK_AVAILABLE = True
    _SDK_IMPORT_ERROR: str | None = None
except ImportError as _exc:  # pragma: no cover - depends on environment
    SOLARI_SDK_AVAILABLE = False
    _SDK_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"

if TYPE_CHECKING:
    from solari_sandbox import Sandbox


def _sdk_version() -> str | None:
    if not SOLARI_SDK_AVAILABLE:
        return None
    try:
        from importlib.metadata import version

        return version("solari-sandbox")
    except Exception:  # pragma: no cover - metadata should always resolve
        return None


# --- result models -----------------------------------------------------------


class SolariArtifact(BaseModel):
    """A file collected from the sandbox artifact directory."""

    filename: str
    path: str | None = None
    mime_type: str | None = None
    encoding: str = "base64"
    data: str


class SolariExecutionResult(BaseModel):
    """Outcome of one run_python() call (shape per docs/EXEKIT_SPEC.md §D)."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    error: str | None = None
    status: str
    artifacts: list[SolariArtifact] = Field(default_factory=list)
    raw: dict | None = None


# --- helpers ------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} chars truncated]"


def _join_streams(result: Any) -> tuple[str, str]:
    """Collect stdout/stderr from a RunCodeResult.

    There is no top-level .stdout on code results — output arrives as a list of
    typed items (stdout/stderr/result), see
    examples/sandbox-code-interpreter-py/main.py lines 46-53 and its README.
    """
    stdout: list[str] = []
    stderr: list[str] = []
    for item in result.results:
        if item.type == "stdout" and item.text:
            stdout.append(item.text)
        elif item.type == "stderr" and item.text:
            stderr.append(item.text)
    return "".join(stdout), "".join(stderr)


def _raw_view(result: Any) -> dict:
    """Small, key-free summary of the raw SDK result for debugging."""
    return {
        "error": result.error if isinstance(result.error, str) else str(result.error),
        "items": [
            {
                "type": item.type,
                "text_chars": len(item.text or ""),
                "has_media": any([item.png, item.jpeg, item.svg, item.html]),
            }
            for item in result.results
        ],
        "charts": len(result.charts),
    }


@dataclass
class _SessionHandle:
    """In-memory handle for a live session (never stores secrets)."""

    sandbox: "Sandbox"
    sandbox_id: str
    api_key_id: int
    context_id: str | None  # kernel context; None until first successful create


# --- the runner ----------------------------------------------------------------


class SolariRunner:
    """Async adapter around a shared SandboxClient and live session handles.

    Raises (see app/services/errors.py):
    - SolariNotConfigured / SolariUnavailable / SolariTimeout: infra problems
    - UnsupportedSolariCapability: capability not present in the verified SDK
    - KeyError: unknown session_id (caller error; routes map to 404)
    run_python() itself returns statuses instead of raising infra errors.
    """

    def __init__(self, artifact_dir: str = "/tmp/exekit") -> None:
        self._artifact_dir = artifact_dir
        self._client: SandboxClient | None = None
        self._client_lock = asyncio.Lock()
        self._handles: dict[str, _SessionHandle] = {}

    @property
    def artifact_dir(self) -> str:
        """Guest directory where executions may write downloadable artifacts."""
        return self._artifact_dir

    # -- health / introspection -------------------------------------------------

    async def health_check(self) -> dict:
        """Configuration + SDK import status. Never includes the API key."""
        settings = get_settings()
        return {
            "configured": settings.solari_configured(),
            "sdk_imported": SOLARI_SDK_AVAILABLE,
            "sdk_version": _sdk_version(),
            "base_url": _SOLARI_BASE_URL,
            "error": _SDK_IMPORT_ERROR,
        }

    def inspect_client(self) -> dict:
        """Synchronous, key-free description of the installed SDK surface."""
        if not SOLARI_SDK_AVAILABLE:
            return {"sdk_imported": False, "error": _SDK_IMPORT_ERROR}
        import inspect as _inspect

        import solari_sandbox as sdk
        from solari_core import handle as _handle_mod

        def method_map(cls: type) -> dict[str, str]:
            out: dict[str, str] = {}
            for name, member in _inspect.getmembers(cls):
                if name.startswith("_"):
                    continue
                if isinstance(member, property):
                    out[name] = "property"
                elif _inspect.isfunction(member):
                    try:
                        out[name] = f"{name}{_inspect.signature(member)}"
                    except (ValueError, TypeError):
                        out[name] = f"{name}(...)"
            return out

        info: dict[str, Any] = {
            "sdk_imported": True,
            "sdk_version": _sdk_version(),
            "base_url": _SOLARI_BASE_URL,
            "client": method_map(sdk.SandboxClient),
            "sandbox": method_map(sdk.SessionHandle),  # run_code/files/lifecycle live here
            "command_fs_helpers": {
                "files": method_map(_handle_mod._Files),
                "commands": method_map(_handle_mod._Commands),
            },
            "result_types": sorted(
                n
                for n in (
                    "RunCodeResult",
                    "CodeResultItem",
                    "CommandResult",
                    "FsEntry",
                    "FsStat",
                    "SandboxView",
                )
                if hasattr(sdk, n)
            ),
            "error_classes": sorted(n for n in dir(sdk) if n.endswith("Error")),
        }
        return info

    # -- client management --------------------------------------------------------

    async def _get_client(self) -> SandboxClient:
        if not SOLARI_SDK_AVAILABLE:
            raise SolariUnavailable("the solari-sandbox package is not installed on the server")
        settings = get_settings()
        if not settings.solari_configured():
            raise SolariNotConfigured("SOLARI_API_KEY is not set on the server")
        async with self._client_lock:
            if self._client is None:
                # All params keyword-only (verified signature); base_url required.
                self._client = SandboxClient(
                    api_key=settings.solari_api_key,
                    base_url=_SOLARI_BASE_URL,
                )
            return self._client

    # -- sessions -------------------------------------------------------------------

    async def create_session(self, api_key_id: int) -> str:
        """Create a sandbox + kernel context; return the Solari sandbox id.

        client.create / sandbox.connect / create_code_context verified:
        examples/sandbox-code-interpreter-py/main.py lines 23, 27, 31.
        """
        client = await self._get_client()
        sandbox: Sandbox | None = None
        try:
            # Boots from a snapshot in ~1s (root README); generous idle window.
            sandbox = await asyncio.wait_for(
                client.create(template="base", timeout_ms=_SESSION_IDLE_WINDOW_MS),
                _BOOT_BUDGET_S,
            )
            # connect() opens the control WebSocket; required for code/files RPCs
            # (gotcha §J6; example connects right after create).
            await asyncio.wait_for(sandbox.connect(), _BOOT_BUDGET_S)
            # Pre-create the artifact directory so executions can drop files;
            # files.mkdir is a verified _Files method (SDK surface doc §F).
            try:
                await asyncio.wait_for(
                    sandbox.files.mkdir(self._artifact_dir), _AUX_RPC_BUDGET_S
                )
            except Exception:
                logger.debug("artifact dir mkdir skipped for %s", sandbox.sandboxId)
            # Stateful kernel context: reuse the id across run_code calls to keep
            # variables/imports (example lines 29-33).
            context_id = await asyncio.wait_for(
                sandbox.create_code_context("python"), _BOOT_BUDGET_S
            )
        except asyncio.TimeoutError as exc:
            await self._safe_kill(sandbox)
            raise SolariTimeout("timed out creating Solari sandbox") from exc
        except (SdkSolariError, SdkConnectionError, httpx.HTTPError, OSError) as exc:
            await self._safe_kill(sandbox)
            logger.warning("session creation failed: %s", exc)
            raise SolariUnavailable(f"Solari sandbox creation failed: {exc}") from exc

        assert sandbox is not None
        self._handles[sandbox.sandboxId] = _SessionHandle(
            sandbox=sandbox,
            sandbox_id=sandbox.sandboxId,
            api_key_id=api_key_id,
            context_id=context_id,
        )
        logger.info(
            "session created sandbox_id=%s api_key_id=%d", sandbox.sandboxId, api_key_id
        )
        return sandbox.sandboxId

    async def kill_session(self, session_id: str) -> bool:
        """Destroy the session's VM and drop the local handle.

        kill() (not close()) ends the VM — root README gotcha and
        examples/sandbox-code-interpreter-py/main.py line 56 ('Destroys the VM.
        Without this it lingers until the idle timeout.').
        Returns False only when we hold no such handle (e.g. already killed, or
        lost across a process restart — the DB reaper path uses client.kill()).
        """
        handle = self._handles.get(session_id)
        if handle is None:
            return False
        # A kill RPC that fails because the VM already expired still ends the
        # session; we log the RPC outcome and report the session as killed.
        await self._safe_kill(handle.sandbox)
        del self._handles[session_id]
        logger.info("session killed sandbox_id=%s", session_id)
        return True

    async def kill_by_sandbox_id(self, sandbox_id: str) -> None:
        """Best-effort client-side kill for ids we may not hold a handle for
        (reaper path). Verified: SandboxClient.kill (SDK surface doc §B)."""
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.kill(sandbox_id), _AUX_RPC_BUDGET_S)
            except Exception as exc:
                logger.warning("client.kill(%s) failed: %s", sandbox_id, exc)

    async def _safe_kill(self, sandbox: "Sandbox | None") -> None:
        """kill() with a deadline; never raises (best-effort teardown).

        kill() destroys the remote VM and closes the channel (SDK surface doc
        §E); client-side SandboxClient.kill is documented idempotent
        (SOLARI_INTROSPECTION.txt), so a double kill or an already-expired VM
        is logged, not fatal.
        """
        if sandbox is None:
            return
        try:
            await asyncio.wait_for(sandbox.kill(), _AUX_RPC_BUDGET_S)
        except Exception as exc:
            logger.warning(
                "kill failed for sandbox %s: %s", getattr(sandbox, "sandboxId", "?"), exc
            )

    # -- execution -------------------------------------------------------------------

    async def run_python(self, code: str, session_id: str | None = None) -> SolariExecutionResult:
        """Run Python via the stateful kernel and return a structured result.

        - session_id given: reuse the live session's kernel (variables persist).
        - session_id None: one-shot fresh kernel on a throwaway sandbox
          (run_code without context_id starts fresh each call — example
          lines 29-31); the sandbox is always killed afterwards.
        - Unknown session_id raises KeyError (caller error -> 404 at routes).
        - Infra problems are returned as statuses, never raised.
        """
        settings = get_settings()
        limit = max(1, settings.execution_timeout_seconds)
        cap = settings.max_output_chars

        handle = self._handles.get(session_id) if session_id else None
        if session_id and handle is None:
            raise KeyError(f"unknown session: {session_id}")

        logger.info(
            "execution start mode=%s sandbox_id=%s code_chars=%d",
            "session" if handle else "one-shot",
            handle.sandbox_id if handle else None,
            len(code),
        )
        try:
            if handle is not None:
                return await self._run_in_session(code, handle, limit, cap)
            return await self._run_one_shot(code, limit, cap)
        except SolariNotConfigured as exc:
            logger.warning("execution rejected: solari unconfigured")
            return SolariExecutionResult(status="solari_unconfigured", error=str(exc))
        except (SolariUnavailable, SolariTimeout) as exc:
            logger.warning("execution infrastructure failure: %s", exc)
            return SolariExecutionResult(status="solari_unavailable", error=str(exc))

    async def _run_one_shot(self, code: str, limit: int, cap: int) -> SolariExecutionResult:
        started = time.monotonic()
        client = await self._get_client()
        sandbox: Sandbox | None = None
        try:
            sandbox = await asyncio.wait_for(
                client.create(template="base", timeout_ms=_SESSION_IDLE_WINDOW_MS),
                _BOOT_BUDGET_S,
            )
            await asyncio.wait_for(sandbox.connect(), _BOOT_BUDGET_S)
            try:
                await asyncio.wait_for(
                    sandbox.files.mkdir(self._artifact_dir), _AUX_RPC_BUDGET_S
                )
            except Exception:
                pass  # exists already, or artifact dir unusable -> empty artifacts
            # Hard deadline: run_code has NO timeout parameter (verified), and a
            # running kernel cannot be interrupted — wait_for cancels our await,
            # then the finally block destroys the VM.
            remaining = limit - (time.monotonic() - started)
            result = await asyncio.wait_for(
                sandbox.run_code(code), timeout=max(0.1, remaining)
            )
        except asyncio.TimeoutError:
            logger.warning("execution timeout sandbox_id=%s",
                           sandbox.sandboxId if sandbox else None)
            artifacts = await self._collect_artifacts(sandbox)
            return SolariExecutionResult(
                status="timeout",
                error=f"execution exceeded the {limit}s limit",
                artifacts=artifacts,
            )
        except (SdkSolariError, SdkConnectionError, httpx.HTTPError, OSError) as exc:
            logger.warning("solari infrastructure failure during one-shot: %s", exc)
            raise SolariUnavailable(f"solari execution failed: {exc}") from exc
        except Exception as exc:  # unexpected — map, log, never crash the API
            logger.exception("unexpected failure during one-shot execution")
            raise SolariUnavailable(f"unexpected error: {exc}") from exc
        else:
            stdout, stderr = _join_streams(result)
            failed = bool(result.error)
            artifacts = await self._collect_artifacts(sandbox)
            return SolariExecutionResult(
                stdout=_truncate(stdout, cap),
                stderr=_truncate(stderr, cap),
                exit_code=1 if failed else 0,
                error=str(result.error) if failed else None,
                status="failed" if failed else "completed",
                artifacts=artifacts,
                raw=_raw_view(result),
            )
        finally:
            # One-shot VMs are always destroyed (example's finally: kill, line 56).
            await self._safe_kill(sandbox)

    async def _run_in_session(
        self, code: str, handle: _SessionHandle, limit: int, cap: int
    ) -> SolariExecutionResult:
        sandbox = handle.sandbox
        # Snapshot artifact names so only files created by THIS execution are
        # reported (one-shot sandboxes are fresh, so everything is new there).
        before = await self._artifact_names(sandbox)
        try:
            result = await asyncio.wait_for(
                self._session_run(sandbox, handle, code), timeout=limit
            )
        except asyncio.TimeoutError:
            # The kernel keeps spinning; it cannot be interrupted (verified: no
            # interrupt method on SessionHandle), so the only recovery is to
            # destroy the VM — spec §F marks the session lost.
            logger.warning(
                "session execution timeout sandbox_id=%s; destroying sandbox", handle.sandbox_id
            )
            artifacts = await self._collect_artifacts(sandbox, exclude=before)
            await self._safe_kill(sandbox)
            self._handles.pop(handle.sandbox_id, None)
            return SolariExecutionResult(
                status="timeout",
                error=(
                    f"execution exceeded the {limit}s limit; the session sandbox "
                    "was destroyed because a running kernel cannot be interrupted"
                ),
                artifacts=artifacts,
            )
        except (SdkSolariError, SdkConnectionError, httpx.HTTPError, OSError) as exc:
            # Session survives infra errors — next run will reconnect/recover.
            logger.warning("solari infrastructure failure in session %s: %s",
                           handle.sandbox_id, exc)
            raise SolariUnavailable(f"solari execution failed: {exc}") from exc
        except Exception as exc:
            logger.exception("unexpected failure in session %s", handle.sandbox_id)
            raise SolariUnavailable(f"unexpected error: {exc}") from exc

        stdout, stderr = _join_streams(result)
        failed = bool(result.error)
        artifacts = await self._collect_artifacts(sandbox, exclude=before)
        return SolariExecutionResult(
            stdout=_truncate(stdout, cap),
            stderr=_truncate(stderr, cap),
            exit_code=1 if failed else 0,
            error=str(result.error) if failed else None,
            status="failed" if failed else "completed",
            artifacts=artifacts,
            raw=_raw_view(result),
        )

    async def _session_run(
        self, sandbox: "Sandbox", handle: _SessionHandle, code: str
    ) -> Any:
        """run_code inside a session, with one reconnect+fresh-context retry.

        run_code(code, context_id=...) verified:
        examples/sandbox-code-interpreter-py/main.py lines 33-40.
        """
        try:
            if handle.context_id is None:
                handle.context_id = await sandbox.create_code_context("python")
            return await sandbox.run_code(code, context_id=handle.context_id)
        except SdkConnectionError:
            # Control channel dropped. reconnect() is a verified handle method
            # (SDK surface doc §B). Whether a kernel context survives a
            # re-connect is UNVERIFIED (doc §K), so recover by creating a fresh
            # context on the same sandbox — state loss is logged, not hidden.
            logger.warning(
                "control channel lost for sandbox %s; reconnecting with a fresh kernel",
                handle.sandbox_id,
            )
            await sandbox.reconnect()
            handle.context_id = await sandbox.create_code_context("python")
            return await sandbox.run_code(code, context_id=handle.context_id)

    # -- artifacts --------------------------------------------------------------------

    async def _artifact_names(self, sandbox: "Sandbox") -> set[str]:
        """Best-effort listing; a missing/unreadable dir just means no diff base."""
        try:
            entries = await asyncio.wait_for(
                sandbox.files.list(self._artifact_dir), _AUX_RPC_BUDGET_S
            )
            return {entry.name for entry in entries}
        except Exception:
            return set()

    async def _collect_artifacts(
        self, sandbox: "Sandbox | None", exclude: set[str] | None = None
    ) -> list[SolariArtifact]:
        """Collect files from the artifact directory as base64 artifacts.

        files.list / files.read are verified (_Files signatures; TS equivalents
        in examples/sandbox-quickstart-ts/index.ts lines 33-35). Any failure
        here degrades to empty artifacts — never crashes the execution result.
        """
        if sandbox is None:
            return []
        try:
            entries = await asyncio.wait_for(
                sandbox.files.list(self._artifact_dir), _AUX_RPC_BUDGET_S
            )
        except Exception as exc:
            logger.info(
                "artifact listing unavailable (%s); returning no artifacts",
                type(exc).__name__,
            )
            return []

        artifacts: list[SolariArtifact] = []
        for entry in entries:
            if entry.dir:
                continue
            if exclude is not None and entry.name in exclude:
                continue  # existed before this execution
            path = f"{self._artifact_dir}/{entry.name}"
            try:
                data = await asyncio.wait_for(sandbox.files.read(path), _AUX_RPC_BUDGET_S)
            except Exception as exc:
                logger.warning("artifact %s unreadable (%s)", entry.name, type(exc).__name__)
                continue
            artifacts.append(
                SolariArtifact(
                    filename=entry.name,
                    path=path,
                    mime_type=_MIME_DB.guess_type(entry.name)[0],
                    encoding="base64",
                    data=base64.b64encode(data).decode("ascii"),
                )
            )
        logger.info("collected %d artifact(s) from %s", len(artifacts), self._artifact_dir)
        return artifacts

    # -- file operations ----------------------------------------------------------------

    def _require_handle(self, session_id: str) -> _SessionHandle:
        handle = self._handles.get(session_id)
        if handle is None:
            raise KeyError(f"unknown session: {session_id}")
        return handle

    async def write_file(self, session_id: str, path: str, content: str) -> None:
        """Write a text file into the session's sandbox.

        files.write(path, data) accepts str or bytes (verified _Files signature;
        string write demonstrated in examples/sandbox-quickstart-ts/index.ts
        line 33). One reconnect retry on a dropped control channel.
        """
        handle = self._require_handle(session_id)
        try:
            await handle.sandbox.files.write(path, content)
        except SdkConnectionError:
            logger.warning("channel lost writing file in %s; reconnecting", session_id)
            await handle.sandbox.reconnect()
            await handle.sandbox.files.write(path, content)
        except (SdkSolariError, httpx.HTTPError, OSError) as exc:
            raise SolariUnavailable(f"file write failed: {exc}") from exc
        logger.info("file written sandbox_id=%s path=%s chars=%d", session_id, path, len(content))

    async def read_file(self, session_id: str, path: str) -> str:
        """Read a text file from the session's sandbox (files.read_text, verified)."""
        handle = self._require_handle(session_id)
        try:
            return await handle.sandbox.files.read_text(path)
        except SdkConnectionError:
            logger.warning("channel lost reading file in %s; reconnecting", session_id)
            await handle.sandbox.reconnect()
            return await handle.sandbox.files.read_text(path)
        except (SdkSolariError, httpx.HTTPError, OSError) as exc:
            raise SolariUnavailable(f"file read failed: {exc}") from exc

    async def list_files(self, session_id: str) -> list[str]:
        """List file names (directories excluded) in the session's artifact dir.

        files.list -> List[FsEntry(name, dir, size)] (verified signature; TS
        equivalent in examples/sandbox-quickstart-ts/index.ts line 35).
        """
        handle = self._require_handle(session_id)
        try:
            entries = await handle.sandbox.files.list(self._artifact_dir)
        except SdkConnectionError:
            logger.warning("channel lost listing files in %s; reconnecting", session_id)
            await handle.sandbox.reconnect()
            entries = await handle.sandbox.files.list(self._artifact_dir)
        except (SdkSolariError, httpx.HTTPError, OSError) as exc:
            raise SolariUnavailable(f"file listing failed: {exc}") from exc
        return [entry.name for entry in entries if not entry.dir]
