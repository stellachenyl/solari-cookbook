"""Route tests for /sessions lifecycle and the file download endpoint."""

import pytest
from sqlmodel import Session as DBSession
from sqlmodel import select

import app.dependencies as deps
from app.models import SandboxSession
from app.services import sessions as sessions_service
from app.services.errors import (
    SolariNotConfigured,
    SolariTimeout,
    SolariUnavailable,
    UnsupportedSolariCapability,
)


@pytest.fixture()
def sess(client, key_and_header):
    _, headers = key_and_header
    resp = client.post("/sessions", headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"], headers


def _row(engine, sess_id):
    with DBSession(engine) as db:
        return db.get(SandboxSession, sess_id)


def _runner_raises(exc):
    class FailingRunner:
        async def create_session(self, api_key_id):
            raise exc

    return FailingRunner()


def test_create_session_persists_record_and_calls_runner(
    client, key_and_header, fake_runner, engine
):
    _, headers = key_and_header
    resp = client.post("/sessions", headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["session_id"].startswith("sess_") and body["status"] == "active"
    row = _row(engine, body["session_id"])
    assert row is not None and row.solari_session_id.startswith("sb-")
    assert fake_runner.created and fake_runner.created[0][1] == row.api_key_id


def test_create_session_maps_solari_unconfigured_to_503(client, key_and_header, monkeypatch):
    _, headers = key_and_header
    monkeypatch.setitem(client.app.dependency_overrides, deps.get_runner,
                        lambda: _runner_raises(SolariNotConfigured("no key")))
    resp = client.post("/sessions", headers=headers)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "solari_unconfigured"


def test_create_session_maps_infra_errors(client, key_and_header, monkeypatch):
    _, headers = key_and_header
    monkeypatch.setitem(client.app.dependency_overrides, deps.get_runner,
                        lambda: _runner_raises(SolariUnavailable("capacity")))
    resp = client.post("/sessions", headers=headers)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "solari_unavailable"

    monkeypatch.setitem(client.app.dependency_overrides, deps.get_runner,
                        lambda: _runner_raises(SolariTimeout("slow")))
    resp = client.post("/sessions", headers=headers)
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "solari_timeout"


def test_get_session_bumps_last_used(client, sess, engine):
    sess_id, headers = sess
    before = _row(engine, sess_id).last_used_at
    resp = client.get(f"/sessions/{sess_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"session_id": sess_id, "status": "active"}
    assert _row(engine, sess_id).last_used_at >= before


def test_get_foreign_or_unknown_session_is_404(client, sess):
    sess_id, _ = sess
    other = client.post("/keys/request", json={}).json()["api_key"]
    assert client.get(f"/sessions/{sess_id}", headers={"X-API-Key": other}).status_code == 404
    assert client.get("/sessions/sess_unknown", headers={"X-API-Key": other}).status_code == 404
    assert client.get(f"/sessions/{sess_id}").status_code == 401


def test_delete_kills_runner_and_marks_row(client, sess, fake_runner, engine):
    sess_id, headers = sess
    solari_id = _row(engine, sess_id).solari_session_id
    resp = client.delete(f"/sessions/{sess_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"session_id": sess_id, "status": "killed"}
    assert fake_runner.killed == [solari_id]
    row = _row(engine, sess_id)
    assert row.status == "killed" and row.killed_at is not None


def test_delete_is_not_idempotent_and_maps_to_410(client, sess):
    sess_id, headers = sess
    assert client.delete(f"/sessions/{sess_id}", headers=headers).status_code == 200
    assert client.delete(f"/sessions/{sess_id}", headers=headers).status_code == 410
    assert client.get(f"/sessions/{sess_id}", headers=headers).status_code == 410


def test_delete_falls_back_to_client_kill_when_handle_lost(client, sess, fake_runner, engine):
    sess_id, headers = sess
    solari_id = _row(engine, sess_id).solari_session_id
    fake_runner.created = []  # simulate handle loss across a process restart
    resp = client.delete(f"/sessions/{sess_id}", headers=headers)
    assert resp.status_code == 200
    assert f"client:{solari_id}" in fake_runner.killed


def test_delete_marks_killed_even_when_client_kill_fails(client, sess, fake_runner, engine):
    sess_id, headers = sess
    fake_runner.created = []

    async def boom(sandbox_id):
        fake_runner.killed.append(f"failed:{sandbox_id}")

    fake_runner.kill_by_sandbox_id = boom  # type: ignore[method-assign]
    resp = client.delete(f"/sessions/{sess_id}", headers=headers)
    assert resp.status_code == 200  # DB row still transitions to killed
    assert _row(engine, sess_id).status == "killed"
    assert fake_runner.killed[-1].startswith("failed:")


def test_file_download_round_trip(client, sess, fake_runner):
    sess_id, headers = sess
    solari_id = _row(engine_placeholder := None, sess_id).solari_session_id if False else None
    # (engine comes from the client fixture's overrides; fetch via the route's view)
    fake_runner.files["/tmp/exekit/report.csv"] = "a,b\n1,2\n"
    resp = client.get(f"/sessions/{sess_id}/files/report.csv", headers=headers)
    assert resp.status_code == 200
    assert resp.content == b"a,b\n1,2\n"
    assert "attachment" in resp.headers["Content-Disposition"]
    assert "report.csv" in resp.headers["Content-Disposition"]


def test_file_download_rejects_traversal(client, sess):
    """Percent-encoded separators reach the route untouched through httpx, so
    they must be rejected by the route's own guard; '..' and '.' are normalized
    away by the HTTP client before routing, so they are not asserted here."""
    sess_id, headers = sess
    for name in ("a%2Fb.txt", "a%5Cb.txt", "..%2Fetc%2Fpasswd"):
        resp = client.get(f"/sessions/{sess_id}/files/{name}", headers=headers)
        assert resp.status_code in (400, 404), (name, resp.status_code)


def test_file_download_missing_is_404(client, sess):
    sess_id, headers = sess
    resp = client.get(f"/sessions/{sess_id}/files/nope.txt", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_file_download_unsupported_capability_is_501(client, sess, fake_runner):
    sess_id, headers = sess
    fake_runner.unsupported_files = True
    resp = client.get(f"/sessions/{sess_id}/files/whatever.txt", headers=headers)
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "unsupported_capability"


def test_file_download_on_killed_session_is_410(client, sess):
    sess_id, headers = sess
    client.delete(f"/sessions/{sess_id}", headers=headers)
    resp = client.get(f"/sessions/{sess_id}/files/x.txt", headers=headers)
    assert resp.status_code == 410


def test_orphan_sandbox_killed_when_record_write_fails(
    client, key_and_header, fake_runner, monkeypatch
):
    _, headers = key_and_header

    def explode(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(sessions_service, "create_session_record", explode)
    resp = client.post("/sessions", headers=headers)
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal"
    assert fake_runner.killed, "created sandbox must be destroyed after record failure"
