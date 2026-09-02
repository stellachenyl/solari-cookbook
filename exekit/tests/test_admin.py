"""Admin API tests: token auth, stats, listings, key actions."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session as DBSession, select

from app.models import ApiKey, Execution
from app.services import api_keys as svc
from app.services.quota import consume_credit

ADMIN = {"X-Admin-Token": "test-admin-token"}


@pytest.fixture()
def seeded(client, engine):
    """A key with one completed execution + one with none."""
    with DBSession(engine) as db:
        key1, raw1 = svc.create_api_key(db, email="a@example.com")
        consume_credit(db, key1)
        db.add(Execution(api_key_id=key1.id, code="print(1)", status="completed",
                         exit_code=0, stdout="", stderr=""))
        key2, _ = svc.create_api_key(db, email="b@example.com", credits=0)
        db.commit()
        db.refresh(key1)
        return {"key1_id": key1.id, "key2_id": key2.id, "raw1": raw1}


# --- auth ----------------------------------------------------------------------


def test_all_admin_endpoints_reject_missing_token(client, seeded):
    for path in ("/admin/stats", "/admin/keys", "/admin/executions",
                 "/admin/sessions", "/admin/ledger", "/admin/stripe-events"):
        resp = client.get(path)
        assert resp.status_code == 401, path
        err = resp.json()["error"]
        assert err["code"] == "invalid_admin_token"
        assert err["message"] == "Invalid admin token."


def test_admin_endpoints_reject_wrong_token(client, seeded):
    resp = client.get("/admin/stats", headers={"X-Admin-Token": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_admin_token"


def test_admin_endpoints_reject_user_api_key(client, seeded):
    """A regular API key must not grant admin access."""
    resp = client.get("/admin/stats", headers={"X-API-Key": seeded["raw1"]})
    assert resp.status_code == 401


def test_admin_page_served(client):
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "ExecKit Admin" in resp.text
    assert 'src="/static/admin.js"' in resp.text


# --- stats -----------------------------------------------------------------------


def test_stats_shape_and_counts(client, seeded, fake_runner):
    client.post("/executions", json={"code": "print(1)"},
                headers={"X-API-Key": seeded["raw1"]})
    body = client.get("/admin/stats", headers=ADMIN).json()
    expected_keys = {
        "total_api_keys", "active_api_keys", "paid_api_keys",
        "total_executions", "completed_executions", "failed_executions",
        "timeout_executions", "total_credits_remaining",
        "total_credits_granted", "total_credits_consumed",
        "active_sessions", "stripe_events_processed",
    }
    assert set(body) == expected_keys
    assert body["total_api_keys"] == 2
    assert body["active_api_keys"] == 2
    # 1 seeded completed + 1 posted by this test (fake runner completes it)
    assert body["total_executions"] == 2
    assert body["completed_executions"] == 2
    assert body["failed_executions"] == 0
    # key1: grant 25, consumed twice (seed + posted run) -> 23; key2: 0 credits
    assert body["total_credits_remaining"] == 23
    assert body["total_credits_granted"] == 25
    assert body["total_credits_consumed"] == -2
    assert body["stripe_events_processed"] == 0


# --- listings --------------------------------------------------------------------


def test_keys_listing_fields(client, seeded):
    body = client.get("/admin/keys", headers=ADMIN).json()
    assert body["limit"] == 50 and body["offset"] == 0
    assert len(body["keys"]) == 2
    key = body["keys"][0]
    assert set(key) == {"id", "key_last4", "email", "plan", "credits",
                        "executions_count", "is_active", "created_at"}
    assert key["key_last4"] and key["email"] == "a@example.com"
    assert key["credits"] == 24


def test_executions_listing_pagination(client, seeded):
    body = client.get("/admin/executions?limit=1", headers=ADMIN).json()
    assert body["limit"] == 1
    assert len(body["executions"]) == 1
    # newest first: the only execution is the completed one
    assert body["executions"][0]["status"] == "completed"
    row = body["executions"][0]
    assert set(row) == {"id", "api_key_id", "session_id", "status",
                        "exit_code", "created_at", "finished_at"}


def test_sessions_listing(client, seeded, fake_runner):
    resp = client.post("/sessions", headers={"X-API-Key": seeded["raw1"]})
    assert resp.status_code == 201
    body = client.get("/admin/sessions", headers=ADMIN).json()
    assert len(body["sessions"]) == 1
    row = body["sessions"][0]
    assert set(row) == {"id", "solari_session_id", "api_key_id", "status",
                        "created_at", "last_used_at", "killed_at"}
    assert row["status"] == "active" and row["killed_at"] is None


def test_ledger_listing(client, seeded):
    body = client.get("/admin/ledger", headers=ADMIN).json()
    assert len(body["ledger"]) >= 2  # two grants + one debit
    row = body["ledger"][0]
    assert set(row) == {"id", "api_key_id", "amount", "balance_after",
                        "reason", "created_at"}


def test_stripe_events_listing_empty_then_populated(client, seeded, stripe_enabled, engine):
    from tests.test_billing import post_webhook, make_event

    body = client.get("/admin/stripe-events", headers=ADMIN).json()
    assert body["events"] == []
    with DBSession(engine) as db:
        key_id = db.exec(select(ApiKey)).first().id
    event = make_event(key_id, 1000, event_id="evt_admin_1")
    assert post_webhook(client, event).json() == {"received": True}
    body = client.get("/admin/stripe-events", headers=ADMIN).json()
    assert len(body["events"]) == 1
    row = body["events"][0]
    assert row["stripe_event_id"] == "evt_admin_1" and row["credits"] == 1000
    assert set(row) == {"id", "stripe_event_id", "event_type", "api_key_id",
                        "credits", "processed_at"}


# --- key actions -----------------------------------------------------------------


def test_add_credits_with_reason(client, seeded, engine):
    resp = client.post(f"/admin/keys/{seeded['key2_id']}/credits",
                       json={"amount": 500, "reason": "manual_admin_grant"},
                       headers=ADMIN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["credits"] == 500 and body["key_last4"]
    assert set(body) == {"id", "key_last4", "credits", "plan", "is_active"}
    with DBSession(engine) as db:
        key = db.get(ApiKey, seeded["key2_id"])
        assert key.credits == 500
        from app.models import CreditLedger
        rows = db.exec(select(CreditLedger).where(
            CreditLedger.api_key_id == key.id,
            CreditLedger.reason == "manual_admin_grant")).all()
        assert len(rows) == 1 and rows[0].amount == 500


def test_add_credits_rejects_non_positive(client, seeded):
    for bad in (0, -5, "abc", None):
        resp = client.post(f"/admin/keys/{seeded['key2_id']}/credits",
                           json={"amount": bad, "reason": "x"}, headers=ADMIN)
        assert resp.status_code in (400, 422), bad


def test_add_credits_unknown_key_404(client, seeded):
    resp = client.post("/admin/keys/9999/credits",
                       json={"amount": 5, "reason": "x"}, headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_toggle_active_roundtrip(client, seeded, engine):
    resp = client.post(f"/admin/keys/{seeded['key2_id']}/toggle-active",
                       headers=ADMIN)
    assert resp.json()["is_active"] is False
    with DBSession(engine) as db:
        assert db.get(ApiKey, seeded["key2_id"]).is_active is False
    resp = client.post(f"/admin/keys/{seeded['key2_id']}/toggle-active",
                       headers=ADMIN)
    assert resp.json()["is_active"] is True
    with DBSession(engine) as db:
        assert db.get(ApiKey, seeded["key2_id"]).is_active is True


def test_toggle_unknown_key_404(client, seeded):
    resp = client.post("/admin/keys/9999/toggle-active", headers=ADMIN)
    assert resp.status_code == 404


def test_deactivated_key_gets_403_on_user_api(client, seeded, engine):
    """The admin kill switch + BUG-1 fix working together: deactivate via
    admin, then the user API must answer 403 api_key_inactive."""
    raw2 = None
    # seed() returned raw1 only; mint + deactivate a fresh key here
    with DBSession(engine) as db:
        key3, raw3 = svc.create_api_key(db, credits=0)
        key3_id = key3.id
    client.post(f"/admin/keys/{key3_id}/toggle-active", headers=ADMIN)
    resp = client.get("/keys/me", headers={"X-API-Key": raw3})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "api_key_inactive"
