"""Credit ledger operations (docs/EXEKIT_SPEC.md §G).

Every balance change is a SQL-expression UPDATE (not read-modify-write) so the
debit check is atomic under concurrent requests: the WHERE clause re-evaluates
against the committed row when two writers race, and SQLite's single-writer
WAL model serializes the rest. Each function commits its own transaction —
balance + ledger row land together — and refreshes the instance afterwards.

InsufficientCredits is the domain error from app/errors.py (HTTP 402),
re-exported here for callers that think in quota terms.
"""

import logging

from sqlalchemy import select, update
from sqlmodel import Session as DBSession

from app.errors import InsufficientCredits  # noqa: F401  (re-exported)
from app.models import ApiKey, CreditLedger

logger = logging.getLogger("exekit.quota")


def _balance(db: DBSession, api_key_id: int) -> int:
    return db.execute(
        select(ApiKey.credits).where(ApiKey.id == api_key_id)
    ).scalar_one()


def _write_ledger(db: DBSession, api_key_id: int, amount: int, balance_after: int, reason: str) -> None:
    db.add(
        CreditLedger(
            api_key_id=api_key_id,
            amount=amount,
            balance_after=balance_after,
            reason=reason,
        )
    )


def get_balance(db: DBSession, api_key: ApiKey) -> int:
    """Committed balance for the key."""
    db.refresh(api_key)
    return api_key.credits


def consume_credit(db: DBSession, api_key: ApiKey, reason: str = "execution") -> None:
    """Atomically debit 1 credit; raises InsufficientCredits when empty."""
    result = db.execute(
        update(ApiKey)
        .where(ApiKey.id == api_key.id, ApiKey.credits > 0)
        .values(credits=ApiKey.credits - 1)
    )
    if result.rowcount == 0:
        logger.info("credit consumption refused key_last4=%s reason=%s", api_key.key_last4, reason)
        raise InsufficientCredits(f"api key ...{api_key.key_last4} has no credits")
    balance_after = _balance(db, api_key.id)
    _write_ledger(db, api_key.id, -1, balance_after, reason)
    db.commit()
    db.refresh(api_key)
    logger.info("credit consumed key_last4=%s amount=-1 balance=%d reason=%s",
                api_key.key_last4, balance_after, reason)


def add_credits(db: DBSession, api_key: ApiKey, amount: int, reason: str) -> None:
    """Credit `amount` (> 0) with a ledger row."""
    if amount <= 0:
        raise ValueError("credit amounts must be positive")
    db.execute(
        update(ApiKey)
        .where(ApiKey.id == api_key.id)
        .values(credits=ApiKey.credits + amount)
    )
    balance_after = _balance(db, api_key.id)
    _write_ledger(db, api_key.id, amount, balance_after, reason)
    db.commit()
    db.refresh(api_key)
    logger.info("credits added key_last4=%s amount=%d balance=%d reason=%s",
                api_key.key_last4, amount, balance_after, reason)


def grant_credits(db: DBSession, api_key: ApiKey, amount: int, reason: str) -> None:
    """Alias of add_credits for grant-shaped ledger reasons (signup etc.)."""
    add_credits(db, api_key, amount, reason)


def refund_credit(db: DBSession, api_key: ApiKey, reason: str = "execution_refund") -> None:
    """Return 1 credit (infra-failure refunds only — see spec §G)."""
    add_credits(db, api_key, 1, reason)
    logger.info("credit refunded key_last4=%s reason=%s", api_key.key_last4, reason)
