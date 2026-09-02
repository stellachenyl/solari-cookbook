"""Session cleanup tests: idle reaping, error tolerance, cancellable loop."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session as DBSession

from app.models import SandboxSession, utcnow
from app.services import session_cleanup as sc
from app.services import sessions as sessions_service
from tests.fakes import FakeSolariRunner


def _aged_row(db, *, minutes_old, status="active", solari="sb-1"):
    row = sessions_service.create_session_record(
        db, api_key_id=1, solari_session_id=solari
    )
    stale = (datetime.now(timezone.utc) - timedelta(minutes=minutes_old)).replace(tzinfo=None)
    row.last_used_at = stale
    row.status = status
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_find_idle_sessions_only_returns_old_active_rows(db):
    idle = _aged_row(db, minutes_old=60)
    fresh = _aged_row(db, minutes_old=1, solari="sb-2")
    killed = _aged_row(db, minutes_old=60, status="killed", solari="sb-3")
    rows = sc.find_idle_sessions(db)
    ids = [r.id for r in rows]
    assert idle.id in ids
    assert fresh.id not in ids
    assert killed.id not in ids


def test_cleanup_once_reaps_idle_sessions(db, engine):
    runner = FakeSolariRunner()
    idle = _aged_row(db, minutes_old=90, solari="sb-idle")
    summary = asyncio.run(sc.cleanup_once(runner, engine=engine))
    assert summary == {"reaped": 1, "failed": 0}
    with DBSession(engine) as fresh:
        row = fresh.get(SandboxSession, idle.id)  # re-read: cleanup commits separately
        assert row.status == "timed_out" and row.killed_at is not None
    assert "sb-idle" in runner.killed  # fallback path also fires (fake has no handle)


def test_cleanup_once_survives_unconfigured_solari(db, engine):
    """A server without SOLARI_API_KEY must not crash the cleanup task."""
    from app.services.errors import SolariNotConfigured

    class UnconfiguredRunner(FakeSolariRunner):
        async def kill_session(self, session_id):
            raise SolariNotConfigured("SOLARI_API_KEY is not set on the server")

    _aged_row(db, minutes_old=90, solari="sb-idle")
    summary = asyncio.run(sc.cleanup_once(UnconfiguredRunner(), engine=engine))
    assert summary == {"reaped": 0, "failed": 1}  # logged, app keeps running


def test_cleanup_once_mixed_outcomes(db, engine):
    class FlakyRunner(FakeSolariRunner):
        async def kill_session(self, session_id):
            if session_id == "sb-bad":
                raise RuntimeError("boom")
            return True

    _aged_row(db, minutes_old=90, solari="sb-good")
    _aged_row(db, minutes_old=90, solari="sb-bad")
    summary = asyncio.run(sc.cleanup_once(FlakyRunner(), engine=engine))
    assert summary == {"reaped": 1, "failed": 1}


def test_cleanup_once_falls_back_to_client_kill(db, engine):
    runner = FakeSolariRunner()
    idle = _aged_row(db, minutes_old=90, solari="sb-lost")
    runner.created = []  # handle lost across restart
    summary = asyncio.run(sc.cleanup_once(runner, engine=engine))
    assert summary["reaped"] == 1
    assert "client:sb-lost" in runner.killed


def test_cleanup_loop_runs_and_is_cancellable(db, engine):
    runner = FakeSolariRunner()
    _aged_row(db, minutes_old=90, solari="sb-idle")

    async def scenario():
        task = asyncio.create_task(
            sc.cleanup_loop(runner, interval_seconds=0.05, engine=engine)
        )
        await asyncio.sleep(0.2)  # a few passes
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task

    task = asyncio.run(scenario())
    assert task.cancelled()
    assert "sb-idle" in runner.killed  # the immediate first pass did the work


def test_cleanup_respects_configured_timeout(db, engine, settings):
    """SESSION_IDLE_TIMEOUT_MINUTES drives the cutoff, not a hard-coded 30."""
    settings.session_idle_timeout_minutes = 5
    runner = FakeSolariRunner()
    # 10 minutes old: past the 5-minute window -> reaped
    old_row = _aged_row(db, minutes_old=10, solari="sb-old")
    # 2 minutes old: inside the window -> kept
    new_row = _aged_row(db, minutes_old=2, solari="sb-new")
    summary = asyncio.run(sc.cleanup_once(runner, engine=engine))
    assert summary == {"reaped": 1, "failed": 0}
    assert "sb-old" in runner.killed and "client:sb-old" in runner.killed
    with DBSession(engine) as fresh:
        assert fresh.get(SandboxSession, old_row.id).status == "timed_out"
        assert fresh.get(SandboxSession, new_row.id).status == "active"
