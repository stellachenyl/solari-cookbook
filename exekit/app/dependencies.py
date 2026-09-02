"""FastAPI dependencies: DB session, API-key auth, admin auth, shared runner,
rate-limit helpers, ownership-checked session lookup."""

import secrets

from fastapi import Depends, Header
from sqlmodel import Session as DBSession

from app.config import get_settings
from app.db import get_session
from app.errors import ApiKeyInactive, InvalidAdminToken, InvalidSession, Unauthorized
from app.models import ApiKey
from app.services import api_keys as api_keys_service
from app.services import sessions as sessions_service
from app.services.rate_limit import get_limiter, limit_for
from app.services.solari_runner import SolariRunner

__all__ = [
    "get_db",
    "get_current_api_key",
    "get_admin_token",
    "get_runner",
    "get_session_row",
    "require_request_rate_limit",
    "reset_runner",
]


def get_db():
    yield from get_session()


def get_current_api_key(
    db: DBSession = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ApiKey:
    if not x_api_key:
        raise Unauthorized("Provide your API key in the X-API-Key header.", code="missing_api_key")
    api_key = api_keys_service.get_api_key_by_hash(
        db, api_keys_service.hash_api_key(x_api_key)
    )
    if api_key is None:
        raise Unauthorized("That API key is not valid.", code="invalid_api_key")
    if not api_key.is_active:
        # BUG-1 fix: inactive keys are distinguishable from unknown keys.
        raise ApiKeyInactive("This API key has been deactivated.")
    return api_key


def get_admin_token(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")) -> None:
    expected = get_settings().admin_token
    if not x_admin_token or not expected or not secrets.compare_digest(x_admin_token, expected):
        raise InvalidAdminToken("Admin token missing or incorrect.")


# One shared runner for the whole process: in-memory session handles must be
# process-global so /executions can reach sessions created by /sessions.
_runner: SolariRunner | None = None


def get_runner() -> SolariRunner:
    global _runner
    if _runner is None:
        _runner = SolariRunner()
    return _runner


def reset_runner() -> None:
    """Test/ops hook: drop the process-wide runner (and its session handles)."""
    global _runner
    _runner = None


def get_session_row(
    session_id: str,
    db: DBSession = Depends(get_db),
    api_key: ApiKey = Depends(get_current_api_key),
):
    """Ownership-checked session lookup (path param {session_id}); maps DB
    state to HTTP semantics: 404 unknown/foreign, 410 killed/expired/lost."""
    row = sessions_service.get_session_for_api_key(db, session_id, api_key.id)
    if row is None:
        raise InvalidSession(f"No session {session_id} for this API key.")
    if row.status in ("killed", "expired", "lost", "timed_out"):
        raise InvalidSession(
            f"Session {session_id} is no longer available.",
            code="session_gone", http_status=410,
        )
    return row


def _apply_rate_limit(bucket: str, api_key: ApiKey) -> None:
    get_limiter().check(bucket, str(api_key.id), limit_for(bucket)())


def require_rate_limit_bucket(bucket: str):
    """Dependency factory: apply `bucket` rate limits for the calling key."""
    from fastapi import Depends

    async def dependency(api_key: ApiKey = Depends(get_current_api_key)):
        _apply_rate_limit(bucket, api_key)
        return api_key

    return dependency


# Pre-built dependency instances (shared limiter state, one per bucket).
require_request_rate_limit = require_rate_limit_bucket("requests")
require_executions_rate_limit = require_rate_limit_bucket("executions")
require_sessions_rate_limit = require_rate_limit_bucket("sessions")
