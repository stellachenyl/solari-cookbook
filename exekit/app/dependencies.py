"""FastAPI dependencies: DB session, API-key auth, admin auth, shared runner."""

import fastapi
from fastapi import Depends, Header
from sqlmodel import Session as DBSession

from app.db import get_session
from app.models import ApiKey
from app.services import api_keys as api_keys_service
from app.services import sessions as sessions_service
from app.services.errors import (
    SolariNotConfigured,
    SolariTimeout,
    SolariUnavailable,
    UnsupportedSolariCapability,
)
from app.services.solari_runner import SolariRunner

# Re-exported so route modules can import every shared name from here.
__all__ = [
    "ApiError",
    "get_db",
    "get_current_api_key",
    "get_admin_token",
    "get_runner",
    "get_session_row",
    "SolariNotConfigured",
    "SolariTimeout",
    "SolariUnavailable",
    "UnsupportedSolariCapability",
]


class ApiError(Exception):
    """Raised anywhere in request handling; rendered by main.py's handler as
    the single error envelope (docs/EXEKIT_SPEC.md §D)."""

    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def get_db():
    yield from get_session()


def get_current_api_key(
    db: DBSession = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ApiKey:
    if not x_api_key:
        raise ApiError(401, "missing_api_key", "Provide your API key in the X-API-Key header.")
    api_key = api_keys_service.get_api_key_by_raw_key(db, x_api_key)
    if api_key is None:
        raise ApiError(401, "invalid_api_key", "That API key is not valid.")
    if not api_key.is_active:
        raise ApiError(403, "api_key_inactive", "This API key has been deactivated.")
    return api_key


def get_admin_token(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> None:
    from app.config import get_settings

    expected = get_settings().admin_token
    if not x_admin_token or not expected or x_admin_token != expected:
        raise ApiError(401, "invalid_admin_token", "Admin token missing or incorrect.")


# One shared runner for the whole process: in-memory session handles must be
# process-global so /executions can reach sessions created by /sessions.
_runner: SolariRunner | None = None


def get_runner() -> SolariRunner:
    global _runner
    if _runner is None:
        _runner = SolariRunner()
    return _runner


def get_session_row(
    session_id: str,
    db: DBSession = Depends(get_db),
    api_key: ApiKey = Depends(get_current_api_key),
):
    """Ownership-checked session lookup (path param {session_id}); maps DB
    state to HTTP semantics: 404 unknown/foreign, 410 killed/expired/lost."""
    row = sessions_service.get_session_for_api_key(db, session_id, api_key.id)
    if row is None:
        raise ApiError(404, "invalid_session", f"No session {session_id} for this API key.")
    if row.status in ("killed", "expired", "lost"):
        raise ApiError(410, "session_gone", f"Session {session_id} is no longer available.")
    return row
