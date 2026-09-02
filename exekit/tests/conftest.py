"""Shared fixtures for the ExecKit test suite.

Isolation model:
- Every test gets a fresh in-memory SQLite database (function-scoped engine),
  so tests can never observe each other's rows and no filesystem cleanup is
  needed. StaticPool keeps the :memory: DB alive across connections in the
  engine's single thread; check_same_thread=False lets FastAPI's TestClient
  call dependency handlers from its portal thread.
- Settings are real Settings but with the lru_cache reset, so env leakage
  between tests cannot change behavior. Tests that need specific settings
  monkeypatch attributes on the cached instance (no production code runs
  inside the test process beyond what the app itself imports).
- SolariRunner is replaced by FakeSolariRunner (below) implementing exactly
  the surface the routes consume; see tests/fakes.py for its spec.
"""

import os

# Test env BEFORE app.config is first imported anywhere in the suite.
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("SOLARI_API_KEY", "")
os.environ.setdefault("FREE_CREDITS", "25")

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session as DBSession, SQLModel, create_engine

from app.config import get_settings


@pytest.fixture()
def engine(tmp_path):
    # File-backed so each connection gets its own sqlite handle: required for
    # the concurrency test (StaticPool shares one connection across threads).
    eng = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    SQLModel.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    """A raw session for direct service-level tests."""
    with DBSession(engine) as session:
        yield session


@pytest.fixture()
def settings():
    """The cached Settings instance; attribute-level monkeypatching allowed."""
    get_settings.cache_clear()
    s = get_settings()
    yield s
    get_settings.cache_clear()


@pytest.fixture()
def runner(monkeypatch, settings):
    """A SolariRunner wired to the fake SDK (tests/runner_fakes.py).

    Returns (runner, FakeClient) so tests can script sandbox behavior and
    assert on created/killed sandboxes."""
    from tests.runner_fakes import make_runner

    return make_runner(monkeypatch, settings)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The limiter is process-global; tests create keys with colliding ids on
    fresh databases, so its window must be cleared around every test."""
    from app.services import rate_limit as rl

    rl.get_limiter().reset()
    yield
    rl.get_limiter().reset()


@pytest.fixture()
def fake_runner():
    from tests.fakes import FakeSolariRunner

    runner = FakeSolariRunner()
    runner.set_completed(stdout="hello\n")
    return runner


@pytest.fixture()
def client(engine, fake_runner, settings):
    """TestClient with dependency overrides and tables present."""
    from app.dependencies import get_db, get_runner
    from app.main import app

    def override_db():
        with DBSession(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_runner] = lambda: fake_runner
    with TestClient(app) as c:
        c.headers.update({"X-Test-Runner": "fake"})
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def stripe_enabled(settings):
    """Stripe configured in test mode with a known webhook secret."""
    settings.stripe_secret_key = "sk_test_fake"
    settings.stripe_webhook_secret = "whsec_test_secret"
    settings.stripe_credit_amount = 1000
    settings.stripe_credit_price_usd = 19
    settings.app_base_url = "http://localhost:8000"
    return settings


@pytest.fixture()
def key_and_header(client):
    """A fresh API key (25 credits) plus its X-API-Key header."""
    resp = client.post("/keys/request", json={"email": "qa@example.com"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body, {"X-API-Key": body["api_key"]}
