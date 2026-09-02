"""Hardening tests: request-ID middleware, structured errors, health fields."""

import logging

from fastapi.testclient import TestClient

import app.middleware.request_id as rid_module
from app.errors import ExecKitError, InsufficientCredits, RateLimitExceeded, SolariUnavailable


def test_request_id_generated_and_returned(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    rid = resp.headers["X-Request-ID"]
    assert 8 <= len(rid) <= 64


def test_request_id_is_unique_per_request(client):
    r1 = client.get("/health").headers["X-Request-ID"]
    r2 = client.get("/health").headers["X-Request-ID"]
    assert r1 != r2


def test_client_request_id_is_honored_and_sanitized(client):
    resp = client.get("/health", headers={"X-Request-ID": "my-trace-123"})
    assert resp.headers["X-Request-ID"] == "my-trace-123"
    # malicious / absurd ids are replaced, not echoed
    resp = client.get("/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert "\n" not in resp.headers["X-Request-ID"]
    resp = client.get("/health", headers={"X-Request-ID": "x" * 500})
    assert len(resp.headers["X-Request-ID"]) == 64


def test_request_id_present_on_error_responses(client):
    resp = client.get("/keys/me")  # 401
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "missing_api_key"
    assert body["error"]["details"]["request_id"] == resp.headers["X-Request-ID"]


def test_domain_error_carries_code_and_status():
    err = InsufficientCredits(details={"credits_remaining": 0})
    assert err.code == "insufficient_credits" and err.http_status == 402
    assert RateLimitExceeded("x").http_status == 429
    assert SolariUnavailable("x").http_status == 502


def test_http_404_uses_the_envelope(client):
    resp = client.get("/definitely-not-a-route")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    assert "request_id" in body["error"]["details"]


def test_health_reports_new_fields(client, engine):
    resp = client.get("/health")
    body = resp.json()
    assert body["status"] == "ok"
    assert body["billing_enabled"] is False  # pre-Stripe
    assert body["database"] == "sqlite"
    assert body["active_sessions"] == 0
    assert body["time"].endswith("Z") or "+" in body["time"] or "T" in body["time"]


def test_health_counts_active_sessions(client, key_and_header, fake_runner):
    _, headers = key_and_header
    client.post("/sessions", headers=headers)
    assert client.get("/health").json()["active_sessions"] == 1
    sess_id = client.post("/sessions", headers=headers).json()["session_id"]
    assert client.get("/health").json()["active_sessions"] == 2
    client.delete(f"/sessions/{sess_id}", headers=headers)
    assert client.get("/health").json()["active_sessions"] == 1


class _RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_request_id_logging_context(client):
    """Request/finish logs carry the request id (record attribute), proving
    the middleware's contextvar reaches the log records even though TestClient
    runs the app in a worker thread."""
    handler = _RecordingHandler()
    logging.getLogger().addHandler(handler)
    try:
        client.get("/health", headers={"X-Request-ID": "trace-me-42"})
    finally:
        logging.getLogger().removeHandler(handler)
    relevant = [r for r in handler.records
                if r.name == "exekit.request" and "request started" in r.message]
    finished = [r for r in handler.records
                if r.name == "exekit.request" and "request finished" in r.message]
    assert relevant and finished
    assert all(getattr(r, "request_id", None) == "trace-me-42" for r in relevant + finished)
    assert all(getattr(r, "route", None) == "-" or r.route for r in relevant)


def test_sanitize_rejects_garbage():
    assert rid_module._sanitize(None) is None
    assert rid_module._sanitize("") is None
    assert rid_module._sanitize("  ") is None
    assert rid_module._sanitize("ok-id_1.2") == "ok-id_1.2"
    assert rid_module._sanitize("<script>") is None
