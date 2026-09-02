"""Admin API: stats, entity listings, and key management actions.

Every endpoint requires the X-Admin-Token header (get_admin_token). Read
endpoints are paginated with limit/offset (limit capped at 200); the responses
never include raw key material — only last4 and metadata.
"""

import logging

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session as DBSession, func, select

from app.dependencies import get_admin_token, get_db
from app.errors import InvalidRequest, NotFound
from app.models import (
    ApiKey,
    CreditLedger,
    Execution,
    SandboxSession,
    StripeWebhookEvent,
)
from app.services.quota import add_credits

logger = logging.getLogger("exekit.admin")
router = APIRouter()

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _clamp(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, _MAX_LIMIT)), max(0, offset)


@router.get("/admin/stats")
def admin_stats(
    db: DBSession = Depends(get_db),
    _token: None = Depends(get_admin_token),
) -> dict:
    def count(where=None) -> int:
        statement = select(func.count()).select_from(ApiKey)
        if where is not None:
            statement = statement.where(where)
        return db.exec(statement).one()

    def exec_count(status: str | None) -> int:
        statement = select(func.count()).select_from(Execution)
        if status is not None:
            statement = statement.where(Execution.status == status)
        return db.exec(statement).one()

    def credit_sum(where) -> int:
        return db.exec(
            select(func.coalesce(func.sum(CreditLedger.amount), 0)).where(where)
        ).one()

    return {
        "total_api_keys": count(),
        "active_api_keys": count(ApiKey.is_active),
        "paid_api_keys": count(ApiKey.plan == "paid"),
        "total_executions": exec_count(None),
        "completed_executions": exec_count("completed"),
        "failed_executions": exec_count("failed"),
        "timeout_executions": exec_count("timeout"),
        "total_credits_remaining": db.exec(
            select(func.coalesce(func.sum(ApiKey.credits), 0))
        ).one(),
        "total_credits_granted": credit_sum(CreditLedger.amount > 0),
        "total_credits_consumed": credit_sum(CreditLedger.amount < 0),
        "active_sessions": db.exec(
            select(func.count()).select_from(SandboxSession).where(
                SandboxSession.status == "active")
        ).one(),
        "stripe_events_processed": db.exec(
            select(func.count()).select_from(StripeWebhookEvent)
        ).one(),
    }


@router.get("/admin/keys")
def admin_keys(
    db: DBSession = Depends(get_db),
    _token: None = Depends(get_admin_token),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
) -> dict:
    limit, offset = _clamp(limit, offset)
    rows = db.exec(
        select(ApiKey).order_by(ApiKey.id).offset(offset).limit(limit)
    ).all()
    return {
        "keys": [
            {
                "id": k.id,
                "key_last4": k.key_last4,
                "email": k.email,
                "plan": k.plan,
                "credits": k.credits,
                "executions_count": k.executions_count,
                "is_active": k.is_active,
                "created_at": k.created_at,
            }
            for k in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin/executions")
def admin_executions(
    db: DBSession = Depends(get_db),
    _token: None = Depends(get_admin_token),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
) -> dict:
    limit, offset = _clamp(limit, offset)
    rows = db.exec(
        select(Execution).order_by(Execution.id.desc()).offset(offset).limit(limit)
    ).all()
    return {
        "executions": [
            {
                "id": e.id,
                "api_key_id": e.api_key_id,
                "session_id": e.session_id,
                "status": e.status,
                "exit_code": e.exit_code,
                "created_at": e.created_at,
                "finished_at": e.finished_at,
            }
            for e in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin/sessions")
def admin_sessions(
    db: DBSession = Depends(get_db),
    _token: None = Depends(get_admin_token),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
) -> dict:
    limit, offset = _clamp(limit, offset)
    rows = db.exec(
        select(SandboxSession).order_by(SandboxSession.id.desc())
        .offset(offset).limit(limit)
    ).all()
    return {
        "sessions": [
            {
                "id": s.id,
                "solari_session_id": s.solari_session_id,
                "api_key_id": s.api_key_id,
                "status": s.status,
                "created_at": s.created_at,
                "last_used_at": s.last_used_at,
                "killed_at": s.killed_at,
            }
            for s in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin/ledger")
def admin_ledger(
    db: DBSession = Depends(get_db),
    _token: None = Depends(get_admin_token),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
) -> dict:
    limit, offset = _clamp(limit, offset)
    rows = db.exec(
        select(CreditLedger).order_by(CreditLedger.id.desc()).offset(offset).limit(limit)
    ).all()
    return {
        "ledger": [
            {
                "id": e.id,
                "api_key_id": e.api_key_id,
                "amount": e.amount,
                "balance_after": e.balance_after,
                "reason": e.reason,
                "created_at": e.created_at,
            }
            for e in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin/stripe-events")
def admin_stripe_events(
    db: DBSession = Depends(get_db),
    _token: None = Depends(get_admin_token),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1),
    offset: int = Query(default=0, ge=0),
) -> dict:
    limit, offset = _clamp(limit, offset)
    rows = db.exec(
        select(StripeWebhookEvent).order_by(StripeWebhookEvent.id.desc())
        .offset(offset).limit(limit)
    ).all()
    return {
        "events": [
            {
                "id": e.id,
                "stripe_event_id": e.stripe_event_id,
                "event_type": e.event_type,
                "api_key_id": e.api_key_id,
                "credits": e.credits,
                "processed_at": e.processed_at,
            }
            for e in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.post("/admin/keys/{key_id}/credits")
def admin_add_credits(
    key_id: int,
    body: dict,
    db: DBSession = Depends(get_db),
    _token: None = Depends(get_admin_token),
) -> dict:
    """Add credits to a key and write the ledger entry (amount > 0)."""
    try:
        amount = int(body.get("amount", 0))
    except (TypeError, ValueError):
        raise InvalidRequest("amount must be an integer.")
    if amount <= 0:
        raise InvalidRequest("amount must be positive.")
    reason = str(body.get("reason") or "manual_admin_grant")
    api_key = db.get(ApiKey, key_id)
    if api_key is None:
        raise NotFound(f"No api key with id {key_id}.")
    try:
        add_credits(db, api_key, amount, reason=reason)
    except ValueError as exc:  # quota's own guard; keep the envelope clean
        raise InvalidRequest(str(exc))
    logger.info("admin credits added key_id=%d amount=%d reason=%s", key_id, amount, reason)
    return {
        "id": api_key.id,
        "key_last4": api_key.key_last4,
        "credits": api_key.credits,
        "plan": api_key.plan,
        "is_active": api_key.is_active,
    }


@router.post("/admin/keys/{key_id}/toggle-active")
def admin_toggle_active(
    key_id: int,
    db: DBSession = Depends(get_db),
    _token: None = Depends(get_admin_token),
) -> dict:
    """Flip is_active. Deactivating a key takes effect on its next request."""
    api_key = db.get(ApiKey, key_id)
    if api_key is None:
        raise NotFound(f"No api key with id {key_id}.")
    api_key.is_active = not api_key.is_active
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    logger.info("admin toggled key_id=%d is_active=%s", key_id, api_key.is_active)
    return {
        "id": api_key.id,
        "key_last4": api_key.key_last4,
        "credits": api_key.credits,
        "plan": api_key.plan,
        "is_active": api_key.is_active,
    }
