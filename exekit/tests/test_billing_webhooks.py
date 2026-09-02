"""Billing webhook + ledger tests: grants, idempotency, signature trust.

Events are signed locally with the Stripe SDK's own signer using a test
secret (test mode semantics); no network calls are made.
"""

import json
import time
import uuid

import pytest
from sqlmodel import Session as DBSession, select

import app.services.stripe_service as stripe_service
from app.models import ApiKey, CreditLedger, StripeWebhookEvent
from app.services import api_keys as api_keys_service
from tests.test_billing import (
    WEBHOOK_SECRET,
    ledger_for,
    make_event,
    post_webhook,
    signed_header,
)


def test_webhook_rejects_missing_signature(client, stripe_enabled):
    resp = client.post("/stripe/webhook", content=json.dumps(make_event(1, 1000)).encode())
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_stripe_signature"


def test_webhook_rejects_bad_signature(client, stripe_enabled):
    payload = json.dumps(make_event(1, 1000)).encode()
    resp = client.post(
        "/stripe/webhook", content=payload,
        headers={"Stripe-Signature": "t=1,v1=" + "0" * 64},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_stripe_signature"
    db = client.app  # sanity only
    assert db is not None


def test_webhook_rejects_tampered_payload(client, stripe_enabled):
    event = make_event(1, 1000)
    payload = json.dumps(event).encode()
    good_sig = signed_header(payload)
    tampered = json.dumps({**event, "data": {"object": {"metadata": {"credits": "999999"}}}}).encode()
    resp = client.post("/stripe/webhook", content=tampered,
                       headers={"Stripe-Signature": good_sig})
    assert resp.status_code == 400


def test_webhook_requires_server_secret(client, key_and_header, settings):
    """Key set but webhook secret missing: clean 400, no grant."""
    settings.stripe_secret_key = "sk_test_x"
    settings.stripe_webhook_secret = ""
    resp = client.post("/stripe/webhook", content=json.dumps(make_event(1, 1000)).encode(),
                       headers={"Stripe-Signature": "t=1,v1=abc"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_stripe_signature"


# --- webhook: granting + idempotency ------------------------------------------------


def test_webhook_grants_credits_once(client, key_and_header, stripe_enabled, engine):
    _, headers = key_and_header
    with DBSession(engine) as db:
        key = db.exec(select(ApiKey)).first()
        key_id, key_last4 = key.id, key.key_last4
        before = key.credits

    event = make_event(key_id, 1000)
    resp = post_webhook(client, event)
    assert resp.status_code == 200
    assert resp.json() == {"received": True}

    with DBSession(engine) as db:
        key = db.get(ApiKey, key_id)
        assert key.credits == before + 1000
        assert key.plan == "paid"
        entries = ledger_for(db, key_id)
        assert entries[-1].reason == "stripe_purchase"
        assert entries[-1].amount == 1000
        assert sum(e.amount for e in entries) == key.credits
        stored = db.exec(select(StripeWebhookEvent)).all()
        assert len(stored) == 1 and stored[0].stripe_event_id == event["id"]
        assert stored[0].credits == 1000 and stored[0].api_key_id == key_id


def test_webhook_duplicate_event_ignored(client, key_and_header, stripe_enabled, engine):
    _, headers = key_and_header
    with DBSession(engine) as db:
        key = db.exec(select(ApiKey)).first()
        key_id = key.id
    event = make_event(key_id, 1000, event_id="evt_dup_1")

    assert post_webhook(client, event).json() == {"received": True}
    assert post_webhook(client, event).json() == {"received": True}  # duplicate delivery

    with DBSession(engine) as db:
        key = db.get(ApiKey, key_id)
        assert key.credits == 25 + 1000  # granted exactly once
        assert len(db.exec(select(StripeWebhookEvent)).all()) == 1


def test_webhook_grant_is_exactly_once_under_retry(client, key_and_header, stripe_enabled, engine):
    _, headers = key_and_header
    with DBSession(engine) as db:
        key_id = db.exec(select(ApiKey)).first().id
    event = make_event(key_id, 1000)
    for _ in range(3):  # Stripe retries on timeouts; result must not change
        post_webhook(client, event)
    with DBSession(engine) as db:
        assert db.get(ApiKey, key_id).credits == 25 + 1000


def test_webhook_unhandled_event_type_ignored(client, stripe_enabled):
    event = make_event(1, 1000, event_type="invoice.paid")
    resp = post_webhook(client, event)
    assert resp.status_code == 200
    assert resp.json() == {"ignored": True}


def test_webhook_missing_metadata_logged_and_ignored(client, key_and_header, stripe_enabled, engine):
    _, headers = key_and_header
    with DBSession(engine) as db:
        key_id = db.exec(select(ApiKey)).first().id
        before = db.get(ApiKey, key_id).credits
    event = make_event(key_id, 1000, metadata={"something": "else"})
    resp = post_webhook(client, event)
    assert resp.status_code == 200
    assert resp.json() == {"ignored": True}
    with DBSession(engine) as db:
        assert db.get(ApiKey, key_id).credits == before
        assert db.exec(select(StripeWebhookEvent)).first() is None


def test_webhook_unknown_api_key_recorded_not_granted(client, key_and_header, stripe_enabled, engine):
    event = make_event(99999, 1000)
    resp = post_webhook(client, event)
    assert resp.status_code == 200
    with DBSession(engine) as db:
        stored = db.exec(select(StripeWebhookEvent)).first()
        assert stored is not None and stored.api_key_id is None
        assert db.exec(select(ApiKey)).first().credits == 25  # nothing granted


def test_webhook_ignores_unexpected_credit_amounts(client, key_and_header, stripe_enabled, engine):
    """Defense-in-depth: even a correctly signed event claiming a non-default
    credit amount is refused — grants only happen for the configured package
    (client-side amounts are never trusted)."""
    _, headers = key_and_header
    with DBSession(engine) as db:
        key_id = db.exec(select(ApiKey)).first().id
        before = db.get(ApiKey, key_id).credits
    event = make_event(key_id, 999999)
    resp = post_webhook(client, event)
    assert resp.status_code == 200
    assert resp.json() == {"ignored": True}
    with DBSession(engine) as db:
        assert db.get(ApiKey, key_id).credits == before
        assert db.exec(select(StripeWebhookEvent)).all() == []


def test_webhook_rejects_tampered_payload_bytes(client, key_and_header, stripe_enabled):
    """Altering event bytes breaks the signature over those exact bytes."""
    event = make_event(1, 1000)
    payload = json.dumps(event).encode()
    good_sig = signed_header(payload)
    tampered = json.dumps({**event, "data": {"object": {**event["data"]["object"],
                                                        "amount_total": 1}}}).encode()
    resp = client.post("/stripe/webhook", content=tampered,
                       headers={"Stripe-Signature": good_sig})
    assert resp.status_code == 400


def test_grant_persists_plan_upgrade_once(client, key_and_header, stripe_enabled, engine):
    _, headers = key_and_header
    with DBSession(engine) as db:
        key_id = db.exec(select(ApiKey)).first().id
    post_webhook(client, make_event(key_id, 1000))
    post_webhook(client, make_event(key_id, 1000))
    with DBSession(engine) as db:
        key = db.get(ApiKey, key_id)
        assert key.plan == "paid" and key.credits == 25 + 2000


# --- ledger endpoint -----------------------------------------------------------------


def test_ledger_requires_auth(client, stripe_enabled):
    assert client.get("/billing/ledger").status_code == 401


def test_ledger_returns_entries_newest_first(client, key_and_header, stripe_enabled, engine):
    _, headers = key_and_header
    with DBSession(engine) as db:
        key_id = db.exec(select(ApiKey)).first().id
    post_webhook(client, make_event(key_id, 1000, event_id="evt_l1"))
    client.post("/executions", json={"code": "print(1)"}, headers=headers)
    post_webhook(client, make_event(key_id, 1000, event_id="evt_l2"))

    resp = client.get("/billing/ledger", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["key_last4"] and isinstance(body["credits"], int)
    reasons = [e["reason"] for e in body["ledger"]]
    assert reasons[0] == "stripe_purchase"  # newest first
    assert reasons == ["stripe_purchase", "execution", "stripe_purchase", "initial_free_credits"]
    for entry in body["ledger"]:
        assert set(entry) == {"amount", "balance_after", "reason", "created_at"}


def test_ledger_caps_at_20_entries(client, key_and_header, stripe_enabled, engine):
    _, headers = key_and_header
    with DBSession(engine) as db:
        key = db.exec(select(ApiKey)).first()
        key_id = key.id
        # 25 executions to generate > 20 ledger rows
        for _ in range(25):
            from app.services.quota import consume_credit

            consume_credit(db, key)
        for i in range(3):
            db.add(CreditLedger(api_key_id=key_id, amount=1, balance_after=key.credits + i + 1,
                                reason="admin_grant"))
        db.commit()
    resp = client.get("/billing/ledger", headers=headers)
    ledger = resp.json()["ledger"]
    assert len(ledger) == 20
    assert ledger[0]["reason"] == "admin_grant"  # newest first


def test_get_recent_stripe_events(client, key_and_header, stripe_enabled, engine):
    _, headers = key_and_header
    with DBSession(engine) as db:
        key_id = db.exec(select(ApiKey)).first().id
    post_webhook(client, make_event(key_id, 1000, event_id="evt_recent_1"))
    post_webhook(client, make_event(key_id, 1000, event_id="evt_recent_2"))
    with DBSession(engine) as db:
        events = stripe_service.get_recent_stripe_events(db)
        ids = [e.stripe_event_id for e in events]
        assert ids == ["evt_recent_2", "evt_recent_1"]  # newest first
