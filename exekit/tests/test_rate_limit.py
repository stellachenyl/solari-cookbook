"""Rate limiting tests: sliding window, per-key isolation, bucket limits."""

import pytest

from app.errors import RateLimitExceeded
from app.services import rate_limit as rl


@pytest.fixture(autouse=True)
def _clean_limiter():
    rl.get_limiter().reset()
    yield
    rl.get_limiter().reset()


def test_window_allows_up_to_limit_then_429():
    limiter = rl.SlidingWindowLimiter()
    for i in range(3):
        limiter.check("executions", "42", 3, now=i)
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.check("executions", "42", 3, now=3)
    assert exc.value.http_status == 429
    assert exc.value.code == "rate_limit_exceeded"
    assert exc.value.details["limit"] == 3


def test_window_slides_old_events_expire():
    limiter = rl.SlidingWindowLimiter()
    base = 1000.0
    for i in range(5):
        limiter.check("executions", "42", 5, now=base + i)
    with pytest.raises(RateLimitExceeded):
        limiter.check("executions", "42", 5, now=base + 5)
    # 61s after the first event the window has slid: allowed again
    limiter.check("executions", "42", 5, now=base + 61.5)


def test_keys_are_isolated():
    limiter = rl.SlidingWindowLimiter()
    for _ in range(5):
        limiter.check("executions", "1", 5, now=1.0)
    limiter.check("executions", "2", 5, now=1.0)  # different key: fine


def test_buckets_are_isolated():
    limiter = rl.SlidingWindowLimiter()
    for _ in range(5):
        limiter.check("executions", "1", 5, now=1.0)
    limiter.check("sessions", "1", 5, now=1.0)
    limiter.check("requests", "1", 5, now=1.0)


def test_limit_zero_disables_bucket():
    rl.get_limiter().check("executions", "1", 0, now=1.0)  # no raise


def test_retry_after_is_positive():
    limiter = rl.SlidingWindowLimiter()
    limiter.check("executions", "1", 1, now=10.0)
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.check("executions", "1", 1, now=10.5)
    assert exc.value.details["retry_after_seconds"] >= 1


def test_bucket_limits_come_from_config(settings):
    settings.rate_limit_executions_per_minute = 30
    settings.rate_limit_sessions_per_minute = 10
    settings.rate_limit_requests_per_minute = 100
    assert rl.limit_for("executions")() == 30
    assert rl.limit_for("sessions")() == 10
    assert rl.limit_for("requests")() == 100


def test_rate_limited_end_to_end(client, key_and_header, fake_runner, settings):
    """Exhaust the executions bucket over HTTP; expect 429 with details and a
    request id in the response."""
    settings.rate_limit_executions_per_minute = 3
    _, headers = key_and_header
    statuses = []
    for _ in range(4):
        resp = client.post("/executions", json={"code": "print(1)"}, headers=headers)
        statuses.append(resp.status_code)
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429
    body = resp.json()
    assert body["error"]["code"] == "rate_limit_exceeded"
    assert body["error"]["details"]["limit"] == 3
    assert "X-Request-ID" in resp.headers


def test_rate_limit_is_per_key(client, settings):
    settings.rate_limit_requests_per_minute = 2
    first = client.post("/keys/request", json={}).json()["api_key"]
    second = client.post("/keys/request", json={}).json()["api_key"]
    h1, h2 = {"X-API-Key": first}, {"X-API-Key": second}
    assert client.get("/keys/me", headers=h1).status_code == 200
    assert client.get("/keys/me", headers=h1).status_code == 200
    assert client.get("/keys/me", headers=h1).status_code == 429
    assert client.get("/keys/me", headers=h2).status_code == 200  # other key unaffected


def test_rate_limit_does_not_apply_to_unauthenticated(client, settings):
    settings.rate_limit_requests_per_minute = 1
    for _ in range(5):
        assert client.get("/health").status_code == 200
