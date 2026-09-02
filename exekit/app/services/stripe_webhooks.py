"""Stripe webhook processing: signature verification and credit grants.

Trust rules enforced here:
- Events are only accepted after Stripe-Signature verification.
- Credit amounts must match the server-configured package (metadata is
  written by our own checkout; a signed event claiming anything else is not
  a purchase we made).
- Exactly-once: every processed event id is stored in stripe_webhook_events
  (unique index) before the grant commits; concurrent duplicate deliveries
  lose the INSERT race and roll back to "duplicate" without granting.
"""

import json as _json
import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session as DBSession
from sqlmodel import select

from app.config import get_settings
from app.errors import ExecKitError
from app.models import ApiKey, StripeWebhookEvent
from app.services.quota import add_credits

logger = logging.getLogger("exekit.billing")


def _stripe():
    """The stripe module or None when the package is not installed."""
    try:
        import stripe

        return stripe
    except ImportError:
        return None


def _require_stripe():
    """The stripe module when billing is configured; BillingDisabled otherwise."""
    stripe = _stripe()
    if not get_settings().stripe_secret_key.strip():
        raise BillingDisabled("Stripe is not configured.")
    if stripe is None:
        raise BillingDisabled("Stripe is not configured.")
    return stripe


class InvalidStripeWebhook(ExecKitError):
    """Signature verification failed — HTTP 400 per the webhook contract."""

    default_code = "invalid_stripe_signature"
    default_http_status = 400


def get_recent_stripe_events(db: DBSession, limit: int = 20) -> list[StripeWebhookEvent]:
    """Newest-first processed webhook deliveries (admin visibility helper)."""
    statement = (
        select(StripeWebhookEvent)
        .order_by(StripeWebhookEvent.processed_at.desc(), StripeWebhookEvent.id.desc())
        .limit(limit)
    )
    return list(db.exec(statement).all())


def verify_webhook(payload: bytes, signature_header: str | None) -> dict:
    """Verify the Stripe-Signature header and return the event dict.

    Raises InvalidStripeWebhook (HTTP 400) on missing/invalid signatures or
    unparseable payloads.
    """
    stripe = _stripe()
    settings = get_settings()
    if stripe is None:
        raise InvalidStripeWebhook("Stripe is not available on this server.")
    if not signature_header:
        raise InvalidStripeWebhook("Missing Stripe-Signature header.")
    if not settings.stripe_webhook_secret.strip():
        logger.warning("webhook received but STRIPE_WEBHOOK_SECRET is not set")
        raise InvalidStripeWebhook("Webhook signing secret is not configured.")

    try:
        event = stripe.Webhook.construct_event(
            payload, signature_header, settings.stripe_webhook_secret
        )
    except Exception as exc:  # SignatureVerificationError / json errors
        logger.warning("webhook signature verification failed: %s", type(exc).__name__)
        raise InvalidStripeWebhook("Webhook signature verification failed.")
    return _event_to_dict(event)


def _event_to_dict(event) -> dict:
    """stripe.Webhook.construct_event returns a Stripe Event object; normalize
    to a plain dict so tests and handlers work with one shape."""
    if isinstance(event, dict):
        return event
    try:
        return event.to_dict()
    except AttributeError:
        return _json.loads(_json.dumps(event, default=str))


def _extract_checkout_info(event: dict) -> dict | None:
    """(api_key_id, credits, summary) from a checkout.session.completed event,
    or None when the payload is not usable (logged and ignored by the caller)."""
    data_object = (event.get("data") or {}).get("object") or {}
    metadata = data_object.get("metadata") or {}
    try:
        api_key_id = int(metadata.get("api_key_id", ""))
        credits = int(metadata.get("credits", ""))
    except (TypeError, ValueError):
        logger.warning(
            "checkout event %s missing usable metadata (api_key_id/credits); ignoring",
            event.get("id", "?"),
        )
        return None
    if api_key_id <= 0 or credits <= 0:
        logger.warning(
            "checkout event %s has non-positive metadata values; ignoring", event.get("id", "?")
        )
        return None
    if credits != get_settings().stripe_credit_amount:
        # Defense-in-depth: metadata is written by our own checkout with the
        # server-configured package. A signed event claiming any other amount
        # is not a purchase we made — refuse to grant it.
        logger.warning(
            "checkout event %s claims credits=%d which is not the configured "
            "package (%d); ignoring", event.get("id", "?"), credits,
            get_settings().stripe_credit_amount,
        )
        return None
    amount_total = data_object.get("amount_total")
    payment_status = data_object.get("payment_status")
    return {
        "api_key_id": api_key_id,
        "credits": credits,
        "payment_status": payment_status,
        "summary": (
            f"amount_total={amount_total} payment_status={payment_status} "
            f"credits={credits} api_key_id={api_key_id}"
        ),
    }


def _event_already_processed(db: DBSession, stripe_event_id: str) -> bool:
    statement = select(StripeWebhookEvent).where(
        StripeWebhookEvent.stripe_event_id == stripe_event_id
    )
    return db.exec(statement).first() is not None


def process_checkout_completed(event: dict, db: DBSession) -> str:
    """Grant credits for a verified checkout.session.completed event.

    Returns "granted" | "duplicate" | "ignored". Exactly-once is enforced by
    the unique stripe_event_id: the marker row is inserted first, so a
    concurrent duplicate loses the INSERT and rolls back to "duplicate"
    without double-granting.
    """
    stripe_event_id = event.get("id")
    if not stripe_event_id:
        logger.warning("webhook event without an id; ignoring")
        return "ignored"

    if _event_already_processed(db, stripe_event_id):
        logger.info("stripe event %s already processed; ignoring duplicate", stripe_event_id)
        return "duplicate"

    if event.get("type") != "checkout.session.completed":
        logger.info("ignoring unhandled event type %s", event.get("type"))
        return "ignored"

    info = _extract_checkout_info(event)
    if info is None:
        return "ignored"

    api_key = db.get(ApiKey, info["api_key_id"])
    if api_key is None:
        # Key deleted between checkout and webhook: record the event so
        # Stripe retries stop, but there is nothing to credit.
        logger.warning("checkout event %s references missing api_key %s; recording only",
                       stripe_event_id, info["api_key_id"])
        db.add(StripeWebhookEvent(
            stripe_event_id=stripe_event_id,
            event_type=event.get("type", "checkout.session.completed"),
            api_key_id=None,
            credits=None,
            processed_at=datetime.now(timezone.utc),
            payload_summary=info["summary"] + " (api key not found)",
        ))
        db.commit()
        return "ignored"

    try:
        db.add(StripeWebhookEvent(
            stripe_event_id=stripe_event_id,
            event_type=event.get("type", "checkout.session.completed"),
            api_key_id=api_key.id,
            credits=info["credits"],
            processed_at=datetime.now(timezone.utc),
            payload_summary=info["summary"],
        ))
        db.flush()  # take the unique-index race NOW, before granting
        add_credits(db, api_key, info["credits"], reason="stripe_purchase")
        if api_key.plan == "free":
            api_key.plan = "paid"
            db.add(api_key)
        db.commit()
    except IntegrityError:
        # A concurrent delivery of the same event won the race and committed.
        db.rollback()
        logger.info("stripe event %s granted concurrently; ignoring duplicate", stripe_event_id)
        return "duplicate"

    logger.info(
        "stripe purchase granted key_last4=%s credits=%d event=%s",
        api_key.key_last4, info["credits"], stripe_event_id,
    )
    return "granted"
