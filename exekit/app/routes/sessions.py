"""Thin session routes: lifecycle + file access, logic in services."""

import logging

from fastapi import APIRouter, Depends, Response
from sqlmodel import Session as DBSession

from app.dependencies import (
    get_current_api_key,
    get_db,
    get_runner,
    get_session_row,
    require_sessions_rate_limit,
)
from app.errors import InvalidRequest, NotFound
from app.models import ApiKey, SandboxSession
from app.schemas import SessionResponse
from app.services import sessions as sessions_service
from app.errors import ExecKitError
from app.services.solari_runner import SolariRunner

logger = logging.getLogger("exekit.sessions")
router = APIRouter()


@router.post("/sessions", response_model=SessionResponse, status_code=201)
async def create_session(
    db: DBSession = Depends(get_db),
    api_key: ApiKey = Depends(get_current_api_key),
    runner: SolariRunner = Depends(get_runner),
    _rl=Depends(require_sessions_rate_limit),
) -> SessionResponse:
    """Create a Solari sandbox + kernel context and record it.

    If the sandbox record write fails the sandbox is killed immediately, so a
    storage error can't leak an untracked VM.
    """
    # Domain errors from the runner (not configured / timeout / unavailable)
    # propagate as-is; main.py renders them with their HTTP statuses.
    solari_id = await runner.create_session(api_key_id=api_key.id)

    try:
        row = sessions_service.create_session_record(
            db, api_key_id=api_key.id, solari_session_id=solari_id
        )
    except Exception:
        logger.exception("session record write failed; killing orphan sandbox %s", solari_id)
        await runner.kill_session(solari_id)
        raise ExecKitError("Could not record the new session.", code="internal", http_status=500)

    logger.info("session created session_id=%s key_last4=%s", row.id, api_key.key_last4)
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
    logger.info("session killed session_id=%s (runner_handle=%s)", row.id, killed)
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
    404; an unreadable Solari backend maps to the standard Solari errors.
    """
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise InvalidRequest("Invalid filename.")
    try:
        content = await runner.read_file(
            row.solari_session_id, f"{runner.artifact_dir}/{filename}"
        )
    except KeyError:
        raise NotFound(f"No file {filename} in session {row.id}.")
    return Response(
        content=content.encode("utf-8"),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
