"""Simulate a Stripe checkout.session.completed webhook against a running
ExecKit — for local testing without the Stripe CLI.

Usage:
    python scripts/simulate_stripe_webhook.py --base-url http://localhost:8000 \
        --key-id 1 [--credits 1000]

Behavior:
- Builds a checkout.session.completed event whose metadata carries api_key_id
  and credits (the same shape Stripe sends for our Checkout Sessions).
- If STRIPE_WEBHOOK_SECRET is set in the environment, signs the payload the
  same way Stripe does (HMAC-SHA256 over "<timestamp>.<payload>") using the
  stripe SDK's own WebhookSignature helper, and sends the Stripe-Signature
  header. The server must therefore be running with the SAME secret.
- If no secret is set, prints the exact Stripe CLI command to run instead
  (which forwards properly-signed test events).

Requires the `stripe` package (already in requirements.txt) for signing.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_event(api_key_id: int, credits: int, price_usd: int) -> dict:
    return {
        "id": "evt_sim_" + uuid.uuid4().hex,
        "object": "event",
        "api_version": "2024-06-20",
        "created": int(time.time()),
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {
            "object": {
                "id": "cs_sim_" + uuid.uuid4().hex,
                "object": "checkout.session",
                "mode": "payment",
                "payment_status": "paid",
                "amount_total": price_usd * 100,  # cents, matching the package
                "metadata": {
                    "api_key_id": str(api_key_id),
                    "credits": str(credits),
                },
            }
        },
    }


def sign_payload(payload: bytes, secret: str) -> str:
    """Mirror Stripe's signature scheme using the SDK's own signer."""
    import stripe

    timestamp = int(time.time())
    signature = stripe.WebhookSignature._compute_signature(
        f"{timestamp}.{payload.decode('utf-8')}", secret
    )
    return f"t={timestamp},v1={signature}"


def post(base_url: str, payload: bytes, signature: str | None) -> tuple[int, dict | None]:
    headers = {"Content-Type": "application/json"}
    if signature:
        headers["Stripe-Signature"] = signature
    req = urllib.request.Request(
        base_url.rstrip("/") + "/stripe/webhook", data=payload, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--key-id", type=int, required=True, help="api_keys.id to credit")
    parser.add_argument("--credits", type=int, default=None,
                        help="credits to grant (default: the server's configured package)")
    args = parser.parse_args()

    from app.config import get_settings

    settings = get_settings()
    credits = args.credits if args.credits is not None else settings.stripe_credit_amount
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
    event = build_event(args.key_id, credits, settings.stripe_credit_price_usd)

    payload = json.dumps(event).encode("utf-8")

    print(f"event id: {event['id']}")
    print(f"target:   {args.base_url}/stripe/webhook (api_key_id={args.key_id})")

    if not secret:
        print("\nSTRIPE_WEBHOOK_SECRET is not set in this shell — cannot sign locally.")
        print("Two options:")
        print("  1. export STRIPE_WEBHOOK_SECRET=whsec_... and re-run this script")
        print("     (the server must run with the SAME secret)")
        print("  2. Use the Stripe CLI to send a properly signed test event:")
        print("        stripe trigger checkout.session.completed")
        print("        # or forward real test-mode events:")
        print(f"        stripe listen --forward-to {args.base_url}/stripe/webhook")
        return 1

    signature = sign_payload(payload, secret)
    status, body = post(args.base_url, payload, signature)
    print(f"response: HTTP {status} {json.dumps(body)}")
    if status == 200 and body.get("received"):
        print("OK: webhook accepted; credits granted if metadata referenced a live key.")
        return 0
    print("Webhook not accepted as granted — inspect the response above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
