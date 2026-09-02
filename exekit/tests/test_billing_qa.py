"""QA pass on the Stripe billing change — defect demonstrations and gap tests.

Defects demonstrated here are marked xfail(strict=True) with a BUG-n reason:
they fail today, and the marker flips the suite red the moment production is
fixed (so the fix cannot land silently). Helpers are reused from
tests/test_billing.py; Stripe is signed locally, never called over network.
"""

import json
import threading

import pytest
from sqlmodel import Session as DBSession, select

import app.services.stripe_webhooks as webhooks_module
from app.errors import ExecKitError
from app.models import ApiKey, StripeWebhookEvent
from app.services import stripe_service
from tests.test_billing import (
    WEBHOOK_SECRET,
    make_event,
    post_webhook,
    signed_header,
)


@pytest.fixture()
def key_id(client, key_and_header, engine):
    _, _ = key_and_header
    with DBSession(engine) as db:
        return db.exec(select(ApiKey)).first().id


def grant_event(key_id: int, *, credits=1000, payment_status="paid", amount_total=1900):
    event = make_event(key_id, credits)
    event["data"]["object"]["payment_status"] = payment_status
    event["data"]["object"]["amount_total"] = amount_total
    return event


# --- payment_status gate (BUG-3, fixed) ------------------------------------------
#
# checkout.session.completed with payment_status="unpaid" happens with async
# payment methods (SEPA/ACH). Credits must only be granted for sessions where
# Stripe reports the money as collected.

def test_webhook_unpaid_session_is_not_granted(client, key_id, stripe_enabled, engine):
    resp = post_webhook(client, grant_event(key_id, payment_status="unpaid"))
    assert resp.status_code == 200
    assert resp.json() == {"ignored": True}
    with DBSession(engine) as db:
        key = db.get(ApiKey, key_id)
        assert key.credits == 25  # nothing granted
        assert db.exec(select(StripeWebhookEvent)).all() == []


def test_webhook_missing_payment_status_is_not_granted(client, key_id, stripe_enabled, engine):
    event = grant_event(key_id)
    del event["data"]["object"]["payment_status"]
    resp = post_webhook(client, event)
    assert resp.json() == {"ignored": True}
    with DBSession(engine) as db:
        assert db.get(ApiKey, key_id).credits == 25


# --- BUG-5 (suspected, medium): amount_total is never validated ------------------
#
# A correctly signed event with amount_total=1 cent but metadata credits=1000
# grants 1000 credits. Metadata is server-set, so this requires a server bug
# or account compromise — but validating amount_total against the package
# price is exactly the "do not trust amounts" defense the brief demands.

@pytest.mark.xfail(reason="BUG-5 (suspected): grant does not verify amount_total "
                          "matches the package price; needs product decision", strict=True)
def test_webhook_amount_mismatch_is_not_granted(client, key_id, stripe_enabled, engine):
    resp = post_webhook(client, grant_event(key_id, amount_total=1))  # paid $0.01
    assert resp.json() == {"ignored": True}
    with DBSession(engine) as db:
        assert db.get(ApiKey, key_id).credits == 25


# --- BUG-4 (low): malformed metadata type crashes the webhook ---------------------
#
# _extract_checkout_info calls metadata.get(...) on whatever the event carries.
# A signed event with a non-dict metadata is external input and must be
# ignored cleanly, not answered with a 500.

@pytest.mark.xfail(reason="BUG-4: non-dict metadata raises AttributeError -> 500 "
                          "instead of a clean ignore", strict=True)
def test_webhook_malformed_metadata_type_is_ignored(client, key_id, stripe_enabled):
    event = make_event(key_id, 1000, metadata="junk")
    resp = post_webhook(client, event)
    assert resp.status_code == 200
    assert resp.json() == {"ignored": True}


# --- exactly-once under concurrent duplicate deliveries (IntegrityError race) ----


def test_concurrent_duplicate_delivery_grants_once(client, key_id, stripe_enabled, engine):
    """Two threads deliver the same event simultaneously: exactly one grant.
    The loser must lose the stripe_webhook_events unique-index race and roll
    back its credit grant."""
    event = make_event(key_id, 1000)
    payload = json.dumps(event).encode()
    header = signed_header(payload)
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker():
        # each thread gets its own session over the shared engine
        with DBSession(engine) as db:
            barrier.wait()
            outcomes.append(stripe_service.process_checkout_completed(event, db))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["duplicate", "granted"], outcomes
    with DBSession(engine) as db:
        key = db.get(ApiKey, key_id)
        assert key.credits == 25 + 1000  # granted exactly once
        assert len(db.exec(select(StripeWebhookEvent)).all()) == 1


# --- malformed / edge webhooks (clean-ignore contract) ----------------------------


def test_webhook_non_integer_metadata_values_ignored(client, key_id, stripe_enabled, engine):
    event = make_event(key_id, 1000, metadata={"api_key_id": "abc", "credits": "abc"})
    resp = post_webhook(client, event)
    assert resp.status_code == 200
    assert resp.json() == {"ignored": True}
    with DBSession(engine) as db:
        assert db.get(ApiKey, key_id).credits == 25


def test_webhook_event_without_id_is_ignored(client, key_id, stripe_enabled):
    event = make_event(key_id, 1000)
    del event["id"]
    resp = post_webhook(client, event)
    assert resp.status_code == 200
    assert resp.json() == {"ignored": True}


def test_verify_webhook_rejects_non_json_payload(client, stripe_enabled):
    payload = b"this is not json"
    resp = client.post("/stripe/webhook", content=payload,
                       headers={"Stripe-Signature": signed_header(payload)})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_stripe_signature"


def test_is_enabled_false_when_sdk_missing(settings, monkeypatch):
    settings.stripe_secret_key = "sk_test_x"
    monkeypatch.setattr(stripe_service, "_stripe", lambda: None)
    assert stripe_service.is_enabled() is False


def test_verify_webhook_when_sdk_missing_returns_400(client, settings, monkeypatch):
    settings.stripe_secret_key = "sk_test_x"
    settings.stripe_webhook_secret = WEBHOOK_SECRET
    monkeypatch.setattr(webhooks_module, "_stripe", lambda: None)
    payload = json.dumps(make_event(1, 1000)).encode()
    resp = client.post("/stripe/webhook", content=payload,
                       headers={"Stripe-Signature": signed_header(payload)})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_stripe_signature"


# --- checkout boundaries -----------------------------------------------------------


def test_checkout_service_rejects_absurd_credit_counts(db, key_id, stripe_enabled, monkeypatch):
    """The MAX_CREDITS_PER_CHECKOUT branch: above it, service-level
    invalid_request regardless of the configured package."""
    from app.services.stripe_service import create_checkout_session

    key = db.get(ApiKey, key_id)
    monkeypatch.setattr(stripe_service._stripe(), "checkout", pytest_fail_stripe())
    with pytest.raises(ExecKitError) as exc:
        create_checkout_session(key, credits=200_000, price_usd=19)
    assert exc.value.http_status == 400


def pytest_fail_stripe():
    """No SDK call may happen while validating absurd amounts."""
    class _NoCalls:
        def __getattr__(self, name):
            raise AssertionError("Stripe SDK must not be called during validation")

    return _NoCalls()


def test_checkout_urls_survive_trailing_slash_base_url(client, key_and_header,
                                                       stripe_enabled, monkeypatch):
    stripe_enabled.app_base_url = "http://localhost:8000/"  # trailing slash
    captured = {}

    class FakeSession:
        id = "cs_test_slash"
        url = "https://checkout.stripe.com/c/pay/cs_test_slash"

    def fake_create(**kwargs):
        captured.update(kwargs)
        return FakeSession()

    import stripe as real_stripe

    monkeypatch.setattr(real_stripe.checkout.Session, "create", fake_create)
    _, headers = key_and_header
    resp = client.post("/billing/checkout", json={}, headers=headers)
    assert resp.status_code == 200
    assert captured["success_url"] == (
        "http://localhost:8000/billing/success?session_id={CHECKOUT_SESSION_ID}"
    )
    assert captured["cancel_url"] == "http://localhost:8000/billing/cancel"


def test_checkout_without_body_uses_default_package(client, key_and_header,
                                                    stripe_enabled, monkeypatch):
    class FakeSession:
        id = "cs_test_nobody"
        url = "https://checkout.stripe.com/c/pay/cs_test_nobody"

    monkeypatch.setattr(
        stripe_service._require_stripe().checkout.Session, "create",
        lambda **kw: FakeSession())
    _, headers = key_and_header
    resp = client.post("/billing/checkout", headers=headers)  # no body at all
    assert resp.status_code == 200
    assert resp.json()["checkout_url"].startswith("https://checkout.stripe.com/")


# --- result pages + empty ledger ----------------------------------------------------


def test_success_and_cancel_pages(client):
    success = client.get("/billing/success")
    assert success.status_code == 200
    assert "Payment received." in success.text
    assert "webhook confirmation" in success.text
    assert 'href="/"' in success.text

    cancel = client.get("/billing/cancel")
    assert cancel.status_code == 200
    assert "Payment canceled." in cancel.text
    assert 'href="/"' in cancel.text


def test_ledger_for_fresh_key_shows_signup_grant(client, key_and_header):
    """A fresh key has exactly its initial_free_credits row."""
    _, headers = key_and_header
    body = client.get("/billing/ledger", headers=headers).json()
    assert body["credits"] == 25
    assert [(e["amount"], e["reason"]) for e in body["ledger"]] == [(25, "initial_free_credits")]


# --- branch coverage: SDK-missing paths, gates, and the insert race -----------------


def test_billing_disables_cleanly_when_stripe_package_missing(settings, monkeypatch):
    """`import stripe` failing (not installed) must disable billing, not crash."""
    import sys

    monkeypatch.setitem(sys.modules, "stripe", None)  # import stripe -> ImportError
    assert stripe_service.is_enabled() is False
    assert stripe_service._stripe() is None


def test_require_stripe_gates_on_config_and_sdk(settings, monkeypatch):
    from app.services.stripe_webhooks import _require_stripe

    settings.stripe_secret_key = ""
    with pytest.raises(ExecKitError) as no_key:
        _require_stripe()
    assert no_key.value.code == "billing_disabled"

    settings.stripe_secret_key = "sk_test_x"
    monkeypatch.setattr("app.services.stripe_webhooks._stripe", lambda: None)
    with pytest.raises(ExecKitError) as no_sdk:
        _require_stripe()
    assert no_sdk.value.code == "billing_disabled"


def test_webhook_non_positive_metadata_ignored(client, key_id, stripe_enabled, engine):
    event = make_event(key_id, 1000, metadata={"api_key_id": str(key_id), "credits": "0"})
    resp = post_webhook(client, event)
    assert resp.status_code == 200
    assert resp.json() == {"ignored": True}
    with DBSession(engine) as db:
        assert db.get(ApiKey, key_id).credits == 25


def test_process_ignores_unhandled_types_directly(db, key_id, stripe_enabled):
    """The service-level type check backs the route's (defensive in depth)."""
    event = make_event(key_id, 1000, event_type="invoice.paid")
    assert stripe_service.process_checkout_completed(event, db) == "ignored"


def test_duplicate_insert_race_is_handled_deterministically(
    client, key_id, stripe_enabled, engine, monkeypatch
):
    """Deterministically simulate the race window: the marker row appears after
    process_checkout_completed's own duplicate check but before its INSERT.
    The IntegrityError must be absorbed -> 'duplicate', no second grant."""
    import time as time_module

    event = make_event(key_id, 1000)
    payload = json.dumps(event).encode()
    post_webhook(client, event)  # first delivery: granted, marker stored

    with DBSession(engine) as db:
        key = db.get(ApiKey, key_id)
        assert key.credits == 25 + 1000

    # simulate the concurrent delivery having already stored the marker while
    # our own duplicate check still ran before it
    monkeypatch.setattr(webhooks_module, "_event_already_processed", lambda db, eid: False)
    with DBSession(engine) as db:
        outcome = stripe_service.process_checkout_completed(event, db)
    assert outcome == "duplicate"
    with DBSession(engine) as db:
        key = db.get(ApiKey, key_id)
        assert key.credits == 25 + 1000  # still exactly one grant
        assert len(db.exec(select(StripeWebhookEvent)).all()) == 1
        _ = time_module.time()  # no wall-clock dependency


# --- last uncovered branches: SDK-absent checkout, success-log line, dict events ----


def test_checkout_still_gates_when_sdk_package_missing(client, key_and_header, settings, monkeypatch):
    """Key configured but the SDK import fails: checkout answers 503
    billing_disabled (is_enabled() requires an importable SDK)."""
    import sys

    settings.stripe_secret_key = "sk_test_x"
    monkeypatch.setitem(sys.modules, "stripe", None)
    _, headers = key_and_header
    resp = client.post("/billing/checkout", headers=headers)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "billing_disabled"


def test_process_accepts_plain_dict_events(client, key_id, stripe_enabled, engine):
    """The route normalizes StripeObjects to dicts; process_checkout_completed
    also accepts plain dicts directly (scripts/simulate path)."""
    event = make_event(key_id, 1000)
    with DBSession(engine) as db:
        assert stripe_service.process_checkout_completed(event, db) == "granted"
        with DBSession(engine) as db2:
            key = db2.get(ApiKey, key_id)
            assert key.credits == 25 + 1000


def test_success_log_line_fires_on_grant(client, key_id, stripe_enabled, engine, caplog):
    """The 'stripe purchase granted' observability line carries key last4 and
    the event id — ops relies on it to reconcile with Stripe's dashboard."""
    import logging as _logging

    with caplog.at_level(_logging.INFO, logger="exekit.billing"):
        event = make_event(key_id, 1000)
        with DBSession(engine) as db:
            stripe_service.process_checkout_completed(event, db)
    granted = [r for r in caplog.records if "stripe purchase granted" in r.message]
    assert granted and event["id"] in granted[-1].getMessage()
    assert "slr_test" not in granted[-1].getMessage()  # no secrets in logs


# --- final branch coverage: service-level package check, dict + fallback paths ------


def test_checkout_service_rejects_non_default_package(db, key_id, stripe_enabled):
    """Service-level guard (the route only passes the default; this is the
    belt-and-braces branch for direct service callers)."""
    from app.services.stripe_service import create_checkout_session

    key = db.get(ApiKey, key_id)
    with pytest.raises(ExecKitError) as exc:
        create_checkout_session(key, credits=500, price_usd=19)
    assert exc.value.http_status == 400
    assert "default package" in exc.value.message


def test_event_to_dict_fallback_for_duck_typed_objects():
    """_event_to_dict: dict passes through; an object without to_dict() goes
    through the JSON fallback. (Private helper justified: it is the
    StripeObject normalization boundary and both branches are contract.)"""
    plain = {"id": "evt_1", "type": "checkout.session.completed"}

    class Weird:
        pass

    assert webhooks_module._event_to_dict(plain) is plain
    # the JSON fallback for opaque objects is a last-resort guard: the only
    # contract is that it never crashes the webhook route
    fallback = webhooks_module._event_to_dict(Weird())
    assert isinstance(fallback, (dict, str))


def test_route_accepts_real_stripe_object_shape(client, key_id, stripe_enabled, engine, monkeypatch):
    """End-to-end with a StripeObject-shaped event: construct_event returns a
    non-dict; to_dict() normalization must keep the route working."""
    import stripe as real_stripe

    event = make_event(key_id, 1000)
    payload = json.dumps(event).encode()

    class FakeEvent:
        def to_dict(self):
            return event

    monkeypatch.setattr(
        real_stripe.Webhook, "construct_event",
        lambda payload, sig_header, secret: FakeEvent())
    resp = client.post("/stripe/webhook", content=payload,
                       headers={"Stripe-Signature": "t=1,v1=fake"})
    assert resp.status_code == 200
    assert resp.json() == {"received": True}
    with DBSession(engine) as db:
        assert db.get(ApiKey, key_id).credits == 25 + 1000
