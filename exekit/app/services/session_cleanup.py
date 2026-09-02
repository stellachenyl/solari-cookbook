"""Background cleanup of idle sessions.

cleanup_once() reaps active sessions whose last_used_at is older than
SESSION_IDLE_TIMEOUT_MINUTES: kills the sandbox via SolariRunner (falling
back to the client-side kill when the in-memory handle is gone) and marks the
row timed_out. cleanup_loop() runs it periodically; it is cancellable and
never lets a pass failure escape.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session as DBSession, select

from app.config import get_settings
from app.models import SandboxSession
from app.services import sessions as sessions_service
from app.services.solari_runner import SolariRunner

logger = logging.getLogger("exekit.cleanup")


def _naive_utc(dt: datetime) -> datetime:
    """SQLite round-trips drop tzinfo; normalize to naive UTC for comparison."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def find_idle_sessions(db: DBSession, *, now: datetime | None = None) -> list[SandboxSession]:
    """Active sessions idle past SESSION_IDLE_TIMEOUT_MINUTES (killed, expired
    and lost rows are terminal and never reaped)."""
    settings = get_settings()
    now = _naive_utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(minutes=settings.session_idle_timeout_minutes)
    rows = db.exec(
        select(SandboxSession).where(
            SandboxSession.status == "active",
            SandboxSession.last_used_at < cutoff,
        )
    ).all()
    return list(rows)


async def cleanup_once(runner: SolariRunner, *, engine=None) -> dict[str, int]:
    """Reap all idle sessions; returns {reaped, failed}. Never raises."""
    from app.db import engine as default_engine

    db_engine = engine or default_engine
    reaped = failed = 0
    with DBSession(db_engine) as db:
        rows = find_idle_sessions(db)
        if not rows:
            return {"reaped": 0, "failed": 0}
        for row in rows:
            try:
                killed = await runner.kill_session(row.solari_session_id)
                if not killed:
                    # handle lost (restart): client-side kill is idempotent
                    await runner.kill_by_sandbox_id(row.solari_session_id)
                sessions_service.mark_session_killed(db, row, status="timed_out")
                reaped += 1
                logger.info("idle session reaped session_id=%s solari_id=%s",
                            row.id, row.solari_session_id)
            except Exception as exc:  # never let one bad session stop the sweep
                failed += 1
                logger.warning("cleanup failed for session %s: %s", row.id, exc)
    logger.info("session cleanup pass reaped=%d failed=%d", reaped, failed)
    return {"reaped": reaped, "failed": failed}


async def cleanup_loop(runner: SolariRunner, *, interval_seconds: int, engine=None) -> None:
    """Periodic sweep. Runs one pass immediately, then every interval. Cancel
    with task.cancel(); CancelledError propagates for the caller to absorb."""
    while True:
        try:
            await cleanup_once(runner, engine=engine)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("session cleanup pass crashed")
        await asyncio.sleep(interval_seconds)
