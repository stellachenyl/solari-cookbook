"""Route tests for POST /executions: validation, quota, persistence, refunds,
session routing, and the lost-session path."""


from sqlmodel import Session as DBSession, select

from app.models import CreditLedger, Execution, ExecutionArtifact, SandboxSession
from app.services.errors import SolariUnavailable


def _ledger(db, key_id):
    return db.exec(
        select(CreditLedger).where(CreditLedger.api_key_id == key_id).order_by(CreditLedger.id)
    ).all()


def test_requires_api_key(client):
    resp = client.post("/executions", json={"code": "print(1)"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_api_key"


def test_empty_code_rejected_before_any_side_effect(client, key_and_header, fake_runner):
    _, headers = key_and_header
    for code in ("", "   \n  "):
        resp = client.post("/executions", json={"code": code}, headers=headers)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_request"
    assert fake_runner.runs == []  # nothing was dispatched


def test_oversized_code_rejected(client, key_and_header, fake_runner):
    _, headers = key_and_header
    resp = client.post("/executions", json={"code": "x" * 50001}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
    assert fake_runner.runs == []


def test_boundary_code_length_is_admitted(client, key_and_header, fake_runner):
    _, headers = key_and_header
    resp = client.post("/executions", json={"code": "x" * 50000}, headers=headers)
    assert resp.status_code == 200
    assert fake_runner.runs[-1][0] == "x" * 50000


def test_non_string_code_maps_to_invalid_request(client, key_and_header):
    _, headers = key_and_header
    resp = client.post("/executions", json={"code": 5}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


def test_insufficient_credits_is_402_and_free(client, key_and_header, engine):
    from app.services import api_keys as svc

    _, raw = svc.create_api_key(DBSession(engine), credits=0)
    headers = {"X-API-Key": raw}
    resp = client.post("/executions", json={"code": "print(1)"}, headers=headers)
    assert resp.status_code == 402
    err = resp.json()["error"]
    assert err["code"] == "insufficient_credits"
    assert err["details"]["credits_remaining"] == 0
    with DBSession(engine) as db:
        rows = db.exec(select(Execution)).all()
        assert rows == []  # no execution row for a refused run


def test_happy_path_debits_persists_and_reports(client, key_and_header, fake_runner, engine):
    from tests.fakes import b64

    fake_runner.set_completed(
        stdout="hi\n",
        artifacts=[{"filename": "out.txt", "path": "/tmp/exekit/out.txt",
                    "mime_type": "text/plain", "encoding": "base64", "data": b64("data")}],
    )
    _, headers = key_and_header
    resp = client.post("/executions", json={"code": "print('hi')"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed" and body["stdout"] == "hi\n"
    assert body["exit_code"] == 0 and body["error"] is None
    assert body["execution_id"] == 1 and body["session_id"] is None
    assert body["credits_remaining"] == 24
    assert body["artifacts"][0]["filename"] == "out.txt"
    meta = body["artifacts"][0]
    assert meta["mime_type"] == "text/plain"
    assert meta["encoding"] == "base64"
    assert meta["size_bytes"] == 4
    assert meta["download_available"] is True
    assert "data" not in meta  # base64 wire data is never echoed back

    with DBSession(engine) as db:
        row = db.get(Execution, body["execution_id"])
        stored = db.exec(
            select(ExecutionArtifact).where(ExecutionArtifact.execution_id == row.id)
        ).one()
        assert stored.filename == "out.txt" and stored.size_bytes == 4
        assert stored.data_text == "data"  # small text artifact kept inline
    assert fake_runner.runs == [("print('hi')", None)]

    with DBSession(engine) as db:
        row = db.get(Execution, body["execution_id"])
        assert row is not None
        assert row.status == "completed" and row.stdout == "hi\n" and row.code == "print('hi')"
        assert row.finished_at is not None and row.api_key_id is not None
        entries = _ledger(db, row.api_key_id)
        assert [(e.amount, e.balance_after) for e in entries] == [(25, 25), (-1, 24)]


def test_failed_user_code_is_200_with_stderr(client, key_and_header, fake_runner):
    fake_runner.next_result = None
    fake_runner.set_completed()
    from app.services.solari_runner import SolariExecutionResult

    fake_runner.next_result = SolariExecutionResult(
        stdout="", stderr="ZeroDivisionError", exit_code=1, status="failed",
        error="ZeroDivisionError: division by zero",
    )
    _, headers = key_and_header
    resp = client.post("/executions", json={"code": "1/0"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed" and body["exit_code"] == 1
    assert body["stderr"] == "ZeroDivisionError"
    assert body["credits_remaining"] == 24  # user-code failures are not refunded


def test_timeout_is_200_and_not_refunded(client, key_and_header, fake_runner):
    fake_runner.set_status("timeout", error="execution exceeded the 1s limit")
    _, headers = key_and_header
    resp = client.post("/executions", json={"code": "while True: pass"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "timeout"
    assert resp.json()["credits_remaining"] == 24


def test_solari_unavailable_is_502_with_refund(client, key_and_header, fake_runner, engine):
    fake_runner.set_status("solari_unavailable", error="gateway down")
    _, headers = key_and_header
    resp = client.post("/executions", json={"code": "print(1)"}, headers=headers)
    assert resp.status_code == 502
    err = resp.json()["error"]
    assert err["code"] == "solari_unavailable"
    assert err["details"]["credits_remaining"] == 25
    assert err["details"]["execution_id"] == 1
    with DBSession(engine) as db:
        row = db.get(Execution, 1)
        assert row.status == "solari_unavailable"  # recorded for observability
        entries = _ledger(db, row.api_key_id)
        assert [(e.amount, e.balance_after, e.reason) for e in entries] == [
            (25, 25, "initial_free_credits"),
            (-1, 24, "execution"),
            (1, 25, "execution_refund"),
        ]
        assert sum(e.amount for e in entries) == 25


def test_solari_unconfigured_is_503_with_refund(client, key_and_header, fake_runner):
    fake_runner.set_status("solari_unconfigured", error="SOLARI_API_KEY is not set on the server")
    _, headers = key_and_header
    resp = client.post("/executions", json={"code": "print(1)"}, headers=headers)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "solari_unconfigured"


def test_session_execution_passes_solari_id_and_echoes_public_id(
    client, key_and_header, fake_runner, engine
):
    _, headers = key_and_header
    sess = client.post("/sessions", headers=headers).json()["session_id"]
    with DBSession(engine) as db:
        solari_id = db.get(SandboxSession, sess).solari_session_id
    resp = client.post("/executions", json={"code": "print(1)", "session_id": sess},
                       headers=headers)
    assert resp.status_code == 200
    assert resp.json()["session_id"] == sess
    assert fake_runner.runs[-1] == ("print(1)", solari_id)


def test_foreign_session_is_404_before_debit(client, key_and_header, engine):
    _, headers = key_and_header
    sess = client.post("/sessions", headers=headers).json()["session_id"]
    # a second key tries to use it
    other = client.post("/keys/request", json={}).json()["api_key"]
    resp = client.post("/executions", json={"code": "print(1)", "session_id": sess},
                       headers={"X-API-Key": other})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "invalid_session"
    with DBSession(engine) as db:
        rows = db.exec(select(Execution)).all()
        assert rows == []  # foreign-session attempt must not cost a credit


def test_killed_session_execution_is_410(client, key_and_header):
    _, headers = key_and_header
    sess = client.post("/sessions", headers=headers).json()["session_id"]
    client.delete(f"/sessions/{sess}", headers=headers)
    resp = client.post("/executions", json={"code": "print(1)", "session_id": sess},
                       headers=headers)
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "session_gone"


def test_lost_runner_handle_marks_session_lost_and_refunds(
    client, key_and_header, fake_runner, engine
):
    _, headers = key_and_header
    sess = client.post("/sessions", headers=headers).json()["session_id"]
    fake_runner.next_run_exception = KeyError("unknown session: sb-1")
    resp = client.post("/executions", json={"code": "print(1)", "session_id": sess},
                       headers=headers)
    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "session_gone"
    with DBSession(engine) as db:
        row = db.get(SandboxSession, sess)
        assert row.status == "lost" and row.killed_at is not None
        key_ledger = _ledger(db, row.api_key_id)
        assert sum(e.amount for e in key_ledger) == 25  # debit refunded
        # a lost-session attempt is not billed as a completed execution row
        assert db.exec(select(Execution)).all() == []
