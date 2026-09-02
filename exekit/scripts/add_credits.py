"""Add credits to an existing ExecKit API key.

Usage:
    python scripts/add_credits.py --key ek_live_... --amount 100 [--reason admin_grant]
    python scripts/add_credits.py --key-id 3 --amount 100 [--reason admin_grant]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session as DBSession
from sqlmodel import select

from app.db import create_all, engine
from app.models import ApiKey
from app.services import api_keys as api_keys_service
from app.services.quota import add_credits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--key", help="raw API key (ek_live_...)")
    target.add_argument("--key-id", type=int, help="api_keys.id from the database")
    parser.add_argument("--amount", type=int, required=True, help="credits to add (> 0)")
    parser.add_argument("--reason", default="admin_grant", help="ledger reason")
    args = parser.parse_args()

    if args.amount <= 0:
        parser.error("--amount must be positive")

    create_all()
    with DBSession(engine) as db:
        if args.key is not None:
            api_key = api_keys_service.get_api_key_by_raw_key(db, args.key)
            if api_key is None:
                print("error: key not found or inactive", file=sys.stderr)
                return 1
        else:
            api_key = db.get(ApiKey, args.key_id)
            if api_key is None:
                print(f"error: no api key with id {args.key_id}", file=sys.stderr)
                return 1

        before = api_key.credits
        add_credits(db, api_key, args.amount, args.reason)
        db.commit()
        db.refresh(api_key)
        print(f"api key {api_key.key_last4}: {before} -> {api_key.credits} credits "
              f"(+{args.amount}, reason={args.reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
