"""App-level wiring tests: /health, root page, envelope handlers, DEBUG mode,
and the admin-token dependency."""

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import get_settings
from app.dependencies import get_admin_token, get_current_api_key


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["solari_configured"], bool)
    assert body["database"] == "sqlite"
    assert "time" in body


def test_root_serves_playground(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    for marker in ("ExecKit", "Get free API key", "Keep session alive", "artifacts",
                   "Execute AI-generated Python code safely"):
        assert marker in html, marker
    assert 'src="/static/app.js"' in html and 'href="/static/styles.css"' in html


def test_validation_errors_use_the_envelope(client, key_and_header):
    _, headers = key_and_header
    resp = client.post("/executions", json={"code": 5}, headers=headers)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "invalid_request"
    assert isinstance(body["error"]["details"], dict)


def _raise_in_auth(client):
    def boom():
        raise RuntimeError("secret internals")

    client.app.dependency_overrides[get_current_api_key] = boom
    # raise_server_exceptions=False is a constructor flag: a dedicated client
    # must be used so the 500 envelope (not the exception) is observed.
    raw_client = TestClient(client.app, raise_server_exceptions=False)
    try:
        return raw_client.get("/keys/me")
    finally:
        client.app.dependency_overrides.pop(get_current_api_key, None)


def test_unhandled_exception_returns_envelope_without_traceback(client):
    resp = _raise_in_auth(client)
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "internal"
    assert body["error"]["message"] == "Internal server error"
    assert "RuntimeError" not in body["error"]["message"]


def test_debug_mode_includes_exception_type(client, monkeypatch):
    # main.py reads debug via get_settings(); patch the live instance.
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "debug", True)
    resp = _raise_in_auth(client)
    assert resp.status_code == 500
    assert "RuntimeError" in resp.json()["error"]["message"]


def test_admin_token_dependency(client):
    from app.config import get_settings

    settings = get_settings()
    # valid token passes (no exception)
    get_admin_token(x_admin_token=settings.admin_token)
    # wrong/missing token raises the envelope error
    with pytest.raises(Exception) as exc:
        get_admin_token(x_admin_token="wrong")
    assert getattr(exc.value, "code", None) == "invalid_admin_token"
    with pytest.raises(Exception) as exc:
        get_admin_token(x_admin_token=None)
    assert getattr(exc.value, "code", None) == "invalid_admin_token"


def test_all_documented_routes_are_registered(client):
    # OpenAPI is the version-stable view of registered routes ("/" is excluded
    # from the schema, so it is asserted by request).
    schema = client.get("/openapi.json").json()
    paths = set(schema["paths"])
    for expected in ("/health", "/keys/request", "/keys/me", "/executions",
                     "/sessions", "/sessions/{session_id}",
                     "/sessions/{session_id}/files/{filename}"):
        assert expected in paths, expected
    assert client.get("/").status_code == 200
