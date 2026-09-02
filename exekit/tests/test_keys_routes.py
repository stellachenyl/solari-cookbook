"""Route tests: key issuance and API-key authentication rules."""

import hashlib

import pytest
from sqlmodel import Session as DBSession, select

from app.models import ApiKey


def _keys(db):
    return db.exec(select(ApiKey)).all()


def test_request_key_returns_shape_once(client):
    resp = client.post("/keys/request", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key"].startswith("ek_live_")
    assert body["key_last4"] == body["api_key"][-4:]
    assert body["credits"] == 25 and body["plan"] == "free"


def test_request_key_accepts_optional_email(client):
    resp = client.post("/keys/request", json={"email": "dev@example.com"})
    assert resp.status_code == 200
    me = client.get("/keys/me", headers={"X-API-Key": resp.json()["api_key"]}).json()
    assert me["email"] == "dev@example.com"


def test_request_key_rejects_malformed_email(client):
    resp = client.post("/keys/request", json={"email": "not-an-email"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


def test_raw_key_never_stored(client, engine):
    raw = client.post("/keys/request", json={}).json()["api_key"]
    with DBSession(engine) as db:
        stored = _keys(db)
        assert stored, "expected the created key row"
        for key in stored:
            assert raw not in key.key_hash
            assert len(key.key_hash) == 64
            assert key.key_last4 == raw[-4:]
            assert key.key_hash == hashlib.sha256(raw.encode()).hexdigest()


def test_me_requires_key(client):
    resp = client.get("/keys/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_api_key"


def test_me_rejects_unknown_key(client):
    resp = client.get("/keys/me", headers={"X-API-Key": "ek_live_" + "x" * 40})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


@pytest.mark.xfail(reason="BUG-1: service collapses inactive keys into None, so the "
                           "dependency cannot distinguish 403 from 401; see QA report",
                   strict=True)
def test_me_rejects_inactive_key_with_403(client, engine):
    """Spec (route brief section 3): inactive keys must answer 403
    api_key_inactive so owners can distinguish 'deactivated by admin' from
    'typo in the key'. Currently answers 401 invalid_api_key."""
    raw = client.post("/keys/request", json={}).json()["api_key"]
    with DBSession(engine) as db:
        key = db.exec(select(ApiKey)).first()
        key.is_active = False
        db.add(key)
        db.commit()
    resp = client.get("/keys/me", headers={"X-API-Key": raw})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "api_key_inactive"


def test_me_returns_live_counts(client, key_and_header, fake_runner):
    _, headers = key_and_header
    client.post("/executions", json={"code": "print(1)"}, headers=headers)
    me = client.get("/keys/me", headers=headers).json()
    assert me["executions_count"] == 1
    assert me["credits"] == 24
    assert me["is_active"] is True
    assert "email" in me


def test_error_envelope_shape_on_auth_errors(client):
    body = client.get("/keys/me").json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
