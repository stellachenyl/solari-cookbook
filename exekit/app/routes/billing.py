"""Billing routes: config, checkout, result pages, webhook, ledger."""

import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse
from sqlmodel import Session as DBSession, select

from app.config import get_settings
from app.dependencies import (
    get_current_api_key,
    get_db,
    require_request_rate_limit,
)
from app.errors import BillingDisabled
from app.models import ApiKey, CreditLedger
from app.schemas import BillingConfigResponse, CheckoutRequest, CheckoutResponse, LedgerResponse
from app.services import stripe_service

logger = logging.getLogger("exekit.billing")
router = APIRouter()


def _require_billing_enabled() -> None:
    if not stripe_service.is_enabled():
        raise BillingDisabled()


@router.get("/billing/config", response_model=BillingConfigResponse)
def billing_config() -> BillingConfigResponse:
    settings = get_settings()
    return BillingConfigResponse(
        billing_enabled=stripe_service.is_enabled(),
        credit_amount=settings.stripe_credit_amount,
        price_usd=settings.stripe_credit_price_usd,
        currency="usd",
    )


@router.post("/billing/checkout", response_model=CheckoutResponse)
def create_checkout(
    body: CheckoutRequest | None = None,
    api_key: ApiKey = Depends(get_current_api_key),
    _rl=Depends(require_request_rate_limit),
) -> CheckoutResponse:
    """Create a Stripe Checkout session for the default credit package.

    The amount is always the server-configured package — the client cannot
    name a price. Credits are granted only after webhook confirmation.
    """
    _require_billing_enabled()
    settings = get_settings()
    credits = settings.stripe_credit_amount
    if body is not None and body.credits is not None and body.credits != credits:
        from app.errors import InvalidRequest

        raise InvalidRequest(
            f"Only the default package of {credits} credits is available right now."
        )
    url = stripe_service.create_checkout_session(
        api_key, credits=credits, price_usd=settings.stripe_credit_price_usd
    )
    return CheckoutResponse(checkout_url=url)


_SUCCESS_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>ExecKit — Payment received</title>
<link rel="stylesheet" href="/static/styles.css"></head>
<body><main class="billing-page">
  <h1>Payment received.</h1>
  <p>Your ExecKit credits will appear after webhook confirmation.</p>
  <p><a href="/">Back to ExecKit</a></p>
</main></body></html>"""

_CANCEL_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>ExecKit — Payment canceled</title>
<link rel="stylesheet" href="/static/styles.css"></head>
<body><main class="billing-page">
  <h1>Payment canceled.</h1>
  <p><a href="/">Back to ExecKit</a></p>
</main></body></html>"""


@router.get("/billing/success", response_class=HTMLResponse)
def billing_success() -> HTMLResponse:
    return HTMLResponse(_SUCCESS_PAGE)


@router.get("/billing/cancel", response_class=HTMLResponse)
def billing_cancel() -> HTMLResponse:
    return HTMLResponse(_CANCEL_PAGE)


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: DBSession = Depends(get_db)) -> dict:
    """Stripe webhook receiver. Signature-verified; idempotent by event id."""
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    event = stripe_service.verify_webhook(payload, signature)

    if event.get("type") != "checkout.session.completed":
        logger.info("ignoring unhandled stripe event type %s", event.get("type"))
        return {"ignored": True}

    outcome = stripe_service.process_checkout_completed(event, db)
    logger.info("stripe webhook processed outcome=%s event=%s", outcome, event.get("id"))
    if outcome == "ignored":
        return {"ignored": True}
    return {"received": True}  # granted or duplicate: the delivery was handled


@router.get("/billing/ledger", response_model=LedgerResponse)
def billing_ledger(
    api_key: ApiKey = Depends(get_current_api_key),
    db: DBSession = Depends(get_db),
    _rl=Depends(require_request_rate_limit),
) -> LedgerResponse:
    """Latest 20 ledger entries for the calling key, newest first."""
    entries = db.exec(
        select(CreditLedger)
        .where(CreditLedger.api_key_id == api_key.id)
        .order_by(CreditLedger.id.desc())
        .limit(20)
    ).all()
    return LedgerResponse(
        key_last4=api_key.key_last4,
        credits=api_key.credits,
        ledger=[
            {
                "amount": e.amount,
                "balance_after": e.balance_after,
                "reason": e.reason,
                "created_at": e.created_at,
            }
            for e in entries
        ],
    )
