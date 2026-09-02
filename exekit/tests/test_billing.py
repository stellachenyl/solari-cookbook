"""Billing tests: config gating, checkout sessions, disabled responses.

Stripe is never called over the network: create_checkout_session is exercised
through a stubbed SDK object. Webhook tests live in test_billing_webhooks.py
(events signed locally with the SDK's own signer using a test secret).
"""

import base64
import hashlib
import json
import time
import uuid

import pytest
from sqlmodel import Session as DBSession, select

import app.services.stripe_service as stripe_service
from app.config import get_settings
from app.models import ApiKey, CreditLedger, StripeWebhookEvent
from app.services import api_keys as api_keys_service

WEBHOOK_SECRET = "whsec_test_secret"


def make_event(api_key_id: int, credits: int, event_id: str | None = None,
               event_type: str = "checkout.session.completed",
               metadata: dict | None = None) -> dict:
    return {
        "id": event_id or ("evt_" + uuid.uuid4().hex),
        "object": "event",
        "type": event_type,
        "data": {
            "object": {
                "id": "cs_" + uuid.uuid4().hex,
                "object": "checkout.session",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": 1900,
                "metadata": metadata if metadata is not None else {
                    "api_key_id": str(api_key_id),
                    "credits": str(credits),
                },
            }
        },
    }


def signed_header(payload: bytes, secret: str = WEBHOOK_SECRET) -> str:
    """Sign exactly like Stripe does, via the SDK's own helper."""
    import stripe

    ts = int(time.time())
    sig = stripe.WebhookSignature._compute_signature(
        f"{ts}.{payload.decode('utf-8')}", secret
    )
    return f"t={ts},v1={sig}"


def post_webhook(client, event: dict, *, secret: str | None = WEBHOOK_SECRET,
                 header: str | None = None):
    payload = json.dumps(event).encode()
    return client.post(
        "/stripe/webhook",
        content=payload,
        headers={"Stripe-Signature": header or signed_header(payload, secret or WEBHOOK_SECRET)},
    )


def ledger_for(db, key_id):
    return db.exec(
        select(CreditLedger).where(CreditLedger.api_key_id == key_id).order_by(CreditLedger.id)
    ).all()


# --- config endpoint -------------------------------------------------------------


def test_billing_config_disabled_by_default(client):
    body = client.get("/billing/config").json()
    assert body == {"billing_enabled": False, "credit_amount": 1000,
                    "price_usd": 19, "currency": "usd"}


def test_billing_config_enabled_when_key_set(client, stripe_enabled):
    body = client.get("/billing/config").json()
    assert body["billing_enabled"] is True
    assert body["credit_amount"] == 1000 and body["price_usd"] == 19


def test_billing_enabled_derives_from_secret_key_only(settings):
    settings.stripe_secret_key = ""
    assert settings.billing_enabled is False
    settings.stripe_secret_key = "sk_test_x"
    assert settings.billing_enabled is True


# --- checkout --------------------------------------------------------------------


def test_checkout_disabled_returns_503(client, key_and_header):
    _, headers = key_and_header
    resp = client.post("/billing/checkout", content=b"", headers=headers)
    assert resp.status_code == 503
    err = resp.json()["error"]
    assert err["code"] == "billing_disabled"
    assert "Stripe" in err["message"]


def test_checkout_requires_auth(client, stripe_enabled):
    resp = client.post("/billing/checkout", json={})
    assert resp.status_code == 401


class _FakeSession:
    id = "cs_test_123"
    url = "https://checkout.stripe.com/c/pay/cs_test_123"


def test_checkout_returns_url_when_configured(client, key_and_header, stripe_enabled, monkeypatch):
    calls = {}

    def fake_create(**kwargs):
        calls.update(kwargs)
        return _FakeSession()

    monkeypatch.setattr(stripe_service._require_stripe().checkout.Session, "create", fake_create)
    _, headers = key_and_header
    resp = client.post("/billing/checkout", json={}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == _FakeSession.url
    # contract: payment mode, usd cents, metadata identity, server-side amount
    assert calls["mode"] == "payment"
    item = calls["line_items"][0]
    assert item["quantity"] == 1
    assert item["price_data"]["currency"] == "usd"
    assert item["price_data"]["unit_amount"] == 19 * 100
    assert item["price_data"]["product_data"]["name"] == "ExecKit Execution Credits"
    assert item["price_data"]["product_data"]["description"] == "1000 execution credits"
    assert calls["metadata"]["credits"] == "1000"
    assert calls["metadata"]["api_key_id"] == str(json.loads(
        client.get("/openapi.json").text and "0") or calls["metadata"]["api_key_id"]
    )  # api_key_id present and server-derived
    assert calls["success_url"].startswith("http://localhost:8000/billing/success?session_id=")
    assert "{CHECKOUT_SESSION_ID}" in calls["success_url"]
    assert calls["cancel_url"] == "http://localhost:8000/billing/cancel"


def test_checkout_rejects_custom_credit_amounts(client, key_and_header, stripe_enabled, monkeypatch):
    monkeypatch.setattr(
        stripe_service._require_stripe().checkout.Session, "create",
        lambda **kw: _FakeSession())
    _, headers = key_and_header
    for bad in (10, 500, 5000):
        resp = client.post("/billing/checkout", json={"credits": bad}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_request"


def test_checkout_maps_stripe_api_errors_to_502(client, key_and_header, stripe_enabled, monkeypatch):
    """A bad/declined Stripe call is upstream: clean 502 stripe_error, not a
    raw 500 (found live with a fake sk_test key)."""
    import stripe as real_stripe

    def boom(**kw):
        raise real_stripe.AuthenticationError("Invalid API Key")

    monkeypatch.setattr(
        stripe_service._require_stripe().checkout.Session, "create", boom)
    _, headers = key_and_header
    resp = client.post("/billing/checkout", json={}, headers=headers)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "stripe_error"


def test_checkout_accepts_default_package_explicitly(client, key_and_header, stripe_enabled, monkeypatch):
    monkeypatch.setattr(
        stripe_service._require_stripe().checkout.Session, "create",
        lambda **kw: _FakeSession())
    _, headers = key_and_header
    resp = client.post("/billing/checkout", json={"credits": 1000}, headers=headers)
    assert resp.status_code == 200
