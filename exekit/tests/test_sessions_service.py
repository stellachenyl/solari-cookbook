"""Session record service tests: id shape, ownership scoping, lifecycle."""

from datetime import timedelta

import pytest

from app.models import utcnow
from app.services import sessions as svc


def make(db, api_key_id=1, solari="sb-1"):
    return svc.create_session_record(db, api_key_id=api_key_id, solari_session_id=solari)


def test_create_record_uses_opaque_sess_id(db):
    row = make(db)
    assert row.id.startswith("sess_") and len(row.id) > 10
    assert row.status == "active"
    assert row.killed_at is None
    assert row.created_at is not None and row.last_used_at is not None
    assert row.solari_session_id == "sb-1"


def test_ids_are_unique_across_creations(db):
    a, b = make(db, solari="sb-1"), make(db, solari="sb-2")
    assert a.id != b.id


def test_get_session_scopes_to_api_key(db):
    row = make(db, api_key_id=7)
    assert svc.get_session_for_api_key(db, row.id, 7) is not None
    assert svc.get_session_for_api_key(db, row.id, 8) is None  # foreign key
    assert svc.get_session_for_api_key(db, "sess_missing", 7) is None


def test_touch_updates_last_used(db):
    row = make(db)
    original = row.last_used_at
    row.last_used_at = utcnow() - timedelta(minutes=10)
    db.add(row)
    db.commit()
    svc.touch_session(db, row)
    assert row.last_used_at > original


def test_mark_killed_sets_status_and_timestamp(db):
    row = make(db)
    out = svc.mark_session_killed(db, row)
    assert out.status == "killed" and out.killed_at is not None


def test_mark_lost_is_representable(db):
    row = make(db)
    out = svc.mark_session_killed(db, row, status="lost")
    assert out.status == "lost" and out.killed_at is not None


def test_is_stale_semantics_with_in_memory_aware_datetimes():
    """Documents the intended semantics on an in-memory row (never committed,
    so default_factory stamps are still offset-aware)."""
    fresh = svc.SandboxSession(solari_session_id="sb-x", api_key_id=1)
    assert svc.is_stale(fresh) is False
    fresh.last_used_at = utcnow() - svc.STALE_AFTER - timedelta(seconds=1)
    assert svc.is_stale(fresh) is True
    fresh.status = "killed"
    assert svc.is_stale(fresh) is False  # terminal states never go stale


def test_is_stale_on_db_loaded_row(db):
    """Regression (BUG-2, fixed): rows straight from the DB carry naive
    datetimes; is_stale must not crash on them. The cleanup task depends on
    this."""
    row = make(db)
    db.refresh(row)  # round-trip through SQLite -> naive datetimes
    assert svc.is_stale(row) is False
