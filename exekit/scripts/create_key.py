"""Issue an ExecKit API key from the command line.

Usage:
    python scripts/create_key.py [--email you@example.com] [--credits 25] [--note "..."]

The raw key is printed exactly once; only its SHA-256 hash is stored.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session as DBSession

from app.db import create_all, engine
from app.services import api_keys as api_keys_service


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=None, help="optional owner email")
    parser.add_argument("--credits", type=int, default=None,
                        help="initial credits (default: FREE_CREDITS from config)")
    parser.add_argument("--note", default=None, help="optional free-text note")
    args = parser.parse_args()

    create_all()
    with DBSession(engine) as db:
        api_key, raw_key = api_keys_service.create_api_key(
            db, email=args.email, credits=args.credits, note=args.note
        )

    print(f"API key created (last4: {api_key.key_last4}, plan: {api_key.plan}, "
          f"credits: {api_key.credits})")
    if args.email:
        print(f"email: {args.email}")
    print()
    print(f"  {raw_key}")
    print()
    print("WARNING: this key will not be shown again. Store it now.")


if __name__ == "__main__":
    main()
