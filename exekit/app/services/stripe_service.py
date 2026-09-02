"""Stripe credit purchases: checkout sessions and webhook-driven grants.

Trust rules enforced here:
- Credit amounts come from server config or the webhook's verified payload
  metadata — never from client-supplied numbers on the checkout request.
- Credits are granted ONLY on a signature-verified
  checkout.session.completed webhook, never at checkout creation.
- Exactly-once: every processed event id is stored in stripe_webhook_events
  (unique index) before the grant commits; concurrent duplicate deliveries
  lose the INSERT race and are treated as already-processed.

The stripe SDK is imported lazily so the app boots (and the suite runs)
without the package or any Stripe configuration. Webhook handling lives in
stripe_webhooks.py; this module owns configuration, gating, and checkout.
"""

import logging

from app.config import get_settings
from app.errors import BillingDisabled, ExecKitError
from app.models import ApiKey

logger = logging.getLogger("exekit.billing")

MAX_CREDITS_PER_CHECKOUT = 100_000  # sanity cap even for admin-set packages


class InvalidStripeWebhook(ExecKitError):
    """Signature verification failed — HTTP 400 per the webhook contract."""

    default_code = "invalid_stripe_signature"
    default_http_status = 400


__all__ = [
    "BillingDisabled",
    "create_checkout_session",
    "is_enabled",
]

# Webhook processing lives in stripe_webhooks.py; re-exported here so callers
# (and older imports) have one module name for the billing service.
from app.services.stripe_webhooks import (  # noqa: E402
    InvalidStripeWebhook,
    _require_stripe,
    _stripe,
    get_recent_stripe_events,
    process_checkout_completed,
    verify_webhook,
)


def is_enabled() -> bool:
    """Billing is on iff a Stripe secret key is configured (and importable)."""
    return get_settings().billing_enabled and _stripe() is not None


# --- checkout -------------------------------------------------------------------


def create_checkout_session(api_key: ApiKey, credits: int, price_usd: int) -> str:
    """Create a Stripe Checkout Session and return its hosted URL.

    Uses inline price_data (no pre-created Stripe Price required); metadata
    carries api_key_id and credits — the webhook later grants from metadata,
    never from anything the client says. v1 sells only the default package.
    """
    stripe = _require_stripe()
    settings = get_settings()
    if credits <= 0 or credits > MAX_CREDITS_PER_CHECKOUT:
        raise ExecKitError(
            f"credits must be between 1 and {MAX_CREDITS_PER_CHECKOUT}.",
            code="invalid_request", http_status=400,
        )
    if credits != settings.stripe_credit_amount:
        # v1: exactly the default package; custom amounts are a future phase.
        raise ExecKitError(
            f"Only the default package of {settings.stripe_credit_amount} credits "
            "is available right now.",
            code="invalid_request", http_status=400,
        )

    stripe.api_key = settings.stripe_secret_key
    try:
        session = _create_checkout_session(stripe, api_key, credits, price_usd)
    except Exception as exc:
        # Any Stripe API failure (bad key, declined, network) is upstream,
        # not an ExecKit bug: clean 502, never a raw 500/traceback.
        logger.warning("stripe checkout failed: %s: %s", type(exc).__name__, exc)
        raise ExecKitError(
            "Stripe rejected the checkout request. Try again shortly.",
            code="stripe_error", http_status=502,
        )
    logger.info(
        "checkout session created key_last4=%s credits=%d price_usd=%d session=%s",
        api_key.key_last4, credits, price_usd, session.id,
    )
    return session.url


def _create_checkout_session(stripe, api_key: ApiKey, credits: int, price_usd: int):
    settings = get_settings()
    return stripe.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": price_usd * 100,  # cents
                    "product_data": {
                        "name": "ExecKit Execution Credits",
                        "description": f"{credits} execution credits",
                    },
                },
            }
        ],
        metadata={
            "api_key_id": str(api_key.id),
            "credits": str(credits),
        },
        success_url=settings.app_base_url.rstrip("/")
        + "/billing/success?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=settings.app_base_url.rstrip("/") + "/billing/cancel",
    )
