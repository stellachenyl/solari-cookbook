"""DB helpers for sandbox session records (docs/EXEKIT_SPEC.md §E/§F).

The public session id is ExecKit's opaque `sess_...` primary key; the Solari
sandbox id lives in solari_session_id and is only passed to SolariRunner.
Ownership: sessions are looked up by (id, api_key_id), so a foreign or unknown
id is indistinguishable (None → 404) and never leaks existence.
"""

import secrets
from datetime import timedelta

from sqlmodel import Session as DBSession
from sqlmodel import select

from app.models import SandboxSession, utcnow

# A session is stale when unused this long; stale != killed. Reads still work
# (Solari may still honor the VM), but creation of new runs is refused with
# 410 after this window.
STALE_AFTER = timedelta(minutes=30)


def create_session_record(
    db: DBSession, *, api_key_id: int, solari_session_id: str
) -> SandboxSession:
    row = SandboxSession(
        id="sess_" + secrets.token_urlsafe(16),
        solari_session_id=solari_session_id,
        api_key_id=api_key_id,
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_session_for_api_key(
    db: DBSession, session_id: str, api_key_id: int
) -> SandboxSession | None:
    """The session row iff it exists and belongs to this key; unknown and
    foreign ids are indistinguishable (None)."""
    statement = select(SandboxSession).where(
        SandboxSession.id == session_id,
        SandboxSession.api_key_id == api_key_id,
    )
    return db.exec(statement).first()


def touch_session(db: DBSession, row: SandboxSession) -> None:
    """Bump last_used_at so the reaper does not reap a busy session."""
    row.last_used_at = utcnow()
    db.add(row)
    db.commit()


def mark_session_killed(
    db: DBSession, row: SandboxSession, status: str = "killed"
) -> SandboxSession:
    row.status = status
    row.killed_at = utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def is_stale(row: SandboxSession) -> bool:
    return row.status == "active" and (utcnow() - row.last_used_at) > STALE_AFTER
