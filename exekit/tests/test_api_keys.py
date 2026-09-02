"""Service-level tests: API key issuance/lookup and quota/ledger integrity.

These exercise public service functions against a real (in-memory) database —
no mocking of the units under test. The concurrency test uses real threads and
two independent connections to prove the debit's atomicity claim.
"""

import hashlib
import threading

import pytest
from sqlmodel import Session as DBSession, select

from app.models import ApiKey, CreditLedger
from app.services import api_keys as svc
from app.services.quota import InsufficientCredits, add_credits, consume_credit, get_balance, refund_credit


def ledger_for(db, key_id):
    return db.exec(
        select(CreditLedger).where(CreditLedger.api_key_id == key_id).order_by(CreditLedger.id)
    ).all()


# --- key generation / hashing -------------------------------------------------


def test_generate_api_key_format_and_uniqueness():
    keys = {svc.generate_api_key() for _ in range(50)}
    assert len(keys) == 50
    for key in keys:
        assert key.startswith("ek_live_")
        assert len(key) > len("ek_live_") + 30


def test_hash_is_sha256_and_deterministic():
    import hashlib

    raw = "ek_live_abc123"
    assert svc.hash_api_key(raw) == hashlib.sha256(b"ek_live_abc123").hexdigest()
    assert svc.hash_api_key(raw) == svc.hash_api_key(raw)
    assert svc.hash_api_key(raw) != svc.hash_api_key(raw + "x")


# --- create_api_key -------------------------------------------------------------


def test_create_api_key_grants_free_credits_and_ledger(db):
    api_key, raw = svc.create_api_key(db, email="dev@example.com")
    assert api_key.credits == 25
    assert api_key.plan == "free"
    assert api_key.key_last4 == raw[-4:]
    assert api_key.key_hash == hashlib.sha256(raw.encode()).hexdigest()
    entries = ledger_for(db, api_key.id)
    assert [(e.amount, e.balance_after, e.reason) for e in entries] == [(25, 25, "initial_free_credits")]
    # raw key must never be stored
    assert raw not in api_key.key_hash


def test_create_api_key_with_zero_credits_has_no_ledger_row(db):
    api_key, _ = svc.create_api_key(db, credits=0)
    assert api_key.credits == 0
    assert ledger_for(db, api_key.id) == []


def test_create_api_key_with_custom_credits(db):
    api_key, _ = svc.create_api_key(db, credits=10, note="partner")
    assert api_key.credits == 10 and api_key.note == "partner"
    assert [(e.amount, e.balance_after) for e in ledger_for(db, api_key.id)] == [(10, 10)]


# --- lookup ----------------------------------------------------------------------


def test_lookup_by_raw_key_roundtrip(db):
    api_key, raw = svc.create_api_key(db)
    found = svc.get_api_key_by_raw_key(db, raw)
    assert found is not None and found.id == api_key.id


def test_lookup_rejects_unknown_and_garbage(db):
    svc.create_api_key(db)
    assert svc.get_api_key_by_raw_key(db, "ek_live_doesnotexist") is None
    assert svc.get_api_key_by_raw_key(db, "") is None


def test_lookup_rejects_inactive_key(db):
    """Documented contract: inactive keys authenticate like unknown keys."""
    api_key, raw = svc.create_api_key(db)
    api_key.is_active = False
    db.add(api_key)
    db.commit()
    assert svc.get_api_key_by_raw_key(db, raw) is None


# --- quota -----------------------------------------------------------------------


def test_consume_credit_debits_and_writes_ledger(db):
    api_key, _ = svc.create_api_key(db)
    consume_credit(db, api_key)
    assert api_key.credits == 24
    entries = ledger_for(db, api_key.id)
    assert [(e.amount, e.balance_after, e.reason) for e in entries] == [
        (25, 25, "initial_free_credits"),
        (-1, 24, "execution"),
    ]


def test_consume_credit_raises_when_empty(db):
    api_key, _ = svc.create_api_key(db, credits=0)
    with pytest.raises(InsufficientCredits):
        consume_credit(db, api_key)
    assert api_key.credits == 0
    assert ledger_for(db, api_key.id) == []  # nothing written on failure


def test_consume_credit_drains_exactly_to_zero_then_raises(db):
    api_key, _ = svc.create_api_key(db, credits=2)
    consume_credit(db, api_key)
    consume_credit(db, api_key)
    assert api_key.credits == 0
    with pytest.raises(InsufficientCredits):
        consume_credit(db, api_key)
    assert api_key.credits == 0


def test_add_credits_rejects_non_positive(db):
    api_key, _ = svc.create_api_key(db)
    for bad in (0, -1):
        with pytest.raises(ValueError):
            add_credits(db, api_key, bad, "admin_grant")
    assert api_key.credits == 25


def test_add_and_refund_credit_write_positive_rows(db):
    api_key, _ = svc.create_api_key(db, credits=0)
    add_credits(db, api_key, 100, "admin_grant")
    refund_credit(db, api_key)
    assert api_key.credits == 101
    entries = ledger_for(db, api_key.id)
    assert [(e.amount, e.balance_after, e.reason) for e in entries] == [
        (100, 100, "admin_grant"),
        (1, 101, "execution_refund"),
    ]


def test_ledger_sum_matches_balance_after_arbitrary_mix(db):
    api_key, _ = svc.create_api_key(db)  # 25 credits
    for _ in range(3):
        consume_credit(db, api_key)  # 25 -> 22
    add_credits(db, api_key, 2, "admin_grant")  # -> 24
    consume_credit(db, api_key)  # -> 23
    refund_credit(db, api_key)  # -> 24
    entries = ledger_for(db, api_key.id)
    assert [(e.amount, e.balance_after) for e in entries] == [
        (25, 25), (-1, 24), (-1, 23), (-1, 22), (2, 24), (-1, 23), (1, 24),
    ]
    assert sum(e.amount for e in entries) == api_key.credits == entries[-1].balance_after


def test_concurrent_debit_is_atomic(engine):
    """Two threads race to spend the last credit: exactly one may win, and the
    ledger must reconcile. This is the core double-spend guard."""
    with DBSession(engine) as db:
        api_key, _ = svc.create_api_key(db, credits=1)
        key_id = api_key.id

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker():
        with DBSession(engine) as db:
            k = db.get(ApiKey, key_id)
            barrier.wait()
            try:
                consume_credit(db, k)
                outcomes.append("ok")
            except InsufficientCredits:
                outcomes.append("insufficient")

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1, outcomes
    assert outcomes.count("insufficient") == 1
    with DBSession(engine) as db:
        k = db.get(ApiKey, key_id)
        entries = ledger_for(db, key_id)
        assert k.credits == 0
        assert sum(e.amount for e in entries) == k.credits
        assert sum(1 for e in entries if e.reason == "execution") == 1


def test_get_balance_reads_committed_state(db):
    api_key, _ = svc.create_api_key(db, credits=7)
    assert get_balance(db, api_key) == 7
    consume_credit(db, api_key)
    assert get_balance(db, api_key) == 6
