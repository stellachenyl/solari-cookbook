"""API key issuance and lookup (docs/EXEKIT_SPEC.md §E api_keys).

The raw key is shown exactly once at creation; only its SHA-256 hash and last
four characters are ever stored.
"""

import hashlib
import secrets

from sqlmodel import Session as DBSession
from sqlmodel import select

from app.config import get_settings
from app.models import ApiKey, CreditLedger
from app.services.quota import grant_credits

_KEY_PREFIX = "ek_live_"


def generate_api_key() -> str:
    """Full plaintext key, e.g. ek_live_<43 url-safe chars>. Never persisted."""
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_api_key(
    db: DBSession,
    email: str | None = None,
    credits: int | None = None,
    note: str | None = None,
) -> tuple[ApiKey, str]:
    """Create a key row and its signup-grant ledger entry; return (key, raw).

    `credits=None` grants FREE_CREDITS from config; pass 0 to start empty.
    The raw key is returned once and never stored.
    """
    if credits is None:
        credits = get_settings().free_credits

    raw_key = generate_api_key()
    api_key = ApiKey(
        key_hash=hash_api_key(raw_key),
        key_last4=raw_key[-4:],
        email=email,
        plan="free",
        credits=0,
        note=note,
    )
    db.add(api_key)
    db.flush()  # assign api_key.id for the ledger FK

    if credits > 0:
        # Shared ledger-writing path so balance_after is always consistent.
        grant_credits(db, api_key, credits, reason="initial_free_credits")
    db.commit()
    db.refresh(api_key)
    return api_key, raw_key


def get_api_key_by_hash(db: DBSession, key_hash: str) -> ApiKey | None:
    """Row lookup by hash, regardless of active state. Auth decides whether an
    inactive key is 403 (api_key_inactive) vs 401 (invalid_api_key) — the
    service must not collapse the distinction."""
    if not key_hash:
        return None
    statement = select(ApiKey).where(ApiKey.key_hash == key_hash)
    return db.exec(statement).first()


def get_api_key_by_raw_key(db: DBSession, raw_key: str) -> ApiKey | None:
    """Hash-lookup with inactive keys rejected (None): keys authenticate
    exactly like unknown keys. Kept for admin scripts."""
    api_key = get_api_key_by_hash(db, hash_api_key(raw_key))
    if api_key is None or not api_key.is_active:
        return None
    return api_key
