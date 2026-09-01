"""Session lifecycle endpoints (docs/EXEKIT_SPEC.md §F)."""

import logging

from fastapi import APIRouter, Depends, Response
from sqlmodel import Session as DBSession

from app.dependencies import (
    ApiError,
    get_current_api_key,
    get_db,
    get_runner,
    get_session_row,
)
from app.models import ApiKey, SandboxSession
from app.schemas import SessionResponse
from app.services import sessions as sessions_service
from app.services.errors import (
    SolariNotConfigured,
    SolariTimeout,
    SolariUnavailable,
    UnsupportedSolariCapability,
)
from app.services.solari_runner import SolariRunner

logger = logging.getLogger("exekit.sessions")
router = APIRouter()


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(
    db: DBSession = Depends(get_db),
    api_key: ApiKey = Depends(get_current_api_key),
    runner: SolariRunner = Depends(get_runner),
) -> SessionResponse:
    """Create a Solari sandbox + kernel context and record it.

    If the sandbox record write fails the sandbox is killed immediately, so a
    billing error can't leak an untracked VM.
    """
    try:
        solari_id = await runner.create_session(api_key_id=api_key.id)
    except SolariNotConfigured as exc:
        raise ApiError(503, "solari_unconfigured", str(exc))
    except SolariTimeout as exc:
        raise ApiError(504, "solari_timeout", str(exc))
    except SolariUnavailable as exc:
        raise ApiError(503, "solari_unavailable", str(exc))

    try:
        row = sessions_service.create_session_record(
            db, api_key_id=api_key.id, solari_session_id=solari_id
        )
    except Exception:
        logger.exception("session record write failed; killing orphan sandbox %s", solari_id)
        await runner.kill_session(solari_id)
        raise ApiError(500, "internal", "Could not record the new session.")

    logger.info("session %s created for key last4=%s", row.id, api_key.key_last4)
    return SessionResponse(session_id=row.id, status=row.status)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(
    row: SandboxSession = Depends(get_session_row),
    db: DBSession = Depends(get_db),
) -> SessionResponse:
    sessions_service.touch_session(db, row)
    return SessionResponse(session_id=row.id, status=row.status)


@router.delete("/sessions/{session_id}", response_model=SessionResponse)
async def delete_session(
    row: SandboxSession = Depends(get_session_row),
    db: DBSession = Depends(get_db),
    runner: SolariRunner = Depends(get_runner),
) -> SessionResponse:
    """Kill the session's VM and mark the record killed. Idempotent on the
    Solari side (kill() is documented idempotent), so a best-effort kill still
    reports success — the VM ends up dead either way."""
    killed = await runner.kill_session(row.solari_session_id)
    if not killed:
        # Handle lost (e.g. process restart): fall back to the client-side kill.
        await runner.kill_by_sandbox_id(row.solari_session_id)
    sessions_service.mark_session_killed(db, row)
    logger.info("session %s killed (runner_handle=%s)", row.id, killed)
    return SessionResponse(session_id=row.id, status=row.status)


@router.get("/sessions/{session_id}/files/{filename}")
async def read_session_file(
    filename: str,
    row: SandboxSession = Depends(get_session_row),
    runner: SolariRunner = Depends(get_runner),
) -> Response:
    """Download a file from the session sandbox's artifact directory.

    Traversal is rejected up front (no separators, no dot names); the runner
    reads only inside its configured artifact directory. Unknown files are
    404; an unreadable Solari backend is 503.
    """
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise ApiError(400, "invalid_request", "Invalid filename.")
    try:
        content = await runner.read_file(row.solari_session_id, f"{runner.artifact_dir}/{filename}")
    except KeyError:
        raise ApiError(404, "not_found", f"No file {filename} in session {row.id}.")
    except UnsupportedSolariCapability as exc:
        raise ApiError(501, "unsupported_capability", str(exc))
    except SolariUnavailable as exc:
        raise ApiError(503, "solari_unavailable", str(exc))
    except SolariNotConfigured as exc:
        raise ApiError(503, "solari_unconfigured", str(exc))
    except SolariTimeout as exc:
        raise ApiError(504, "solari_timeout", str(exc))
    return Response(
        content=content.encode("utf-8"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
