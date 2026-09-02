"""Route tests for execution history and artifact endpoints: listing,
pagination, status filtering, detail (include_code), artifact metadata,
download, and cross-user ownership."""

import base64

from sqlmodel import Session as DBSession, select

from app.models import ApiKey, ExecutionArtifact  # noqa: F401  (tables for standalone runs)
from app.services.solari_runner import SolariExecutionResult


def _art(filename, data, mime_type="text/plain"):
    return {"filename": filename, "path": f"/tmp/exekit/{filename}",
            "mime_type": mime_type, "encoding": "base64", "data": data}


def _b64_bytes(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _run(client, headers, fake_runner, *, artifacts=None, result=None, stdout=""):
    """Post one execution; `result` optionally scripts the full runner outcome."""
    if result is not None:
        fake_runner.next_result = result
    else:
        fake_runner.set_completed(stdout=stdout, artifacts=artifacts or [])
    resp = client.post("/executions", json={"code": "pass"}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _other_key_header(client):
    body = client.post("/keys/request", json={}).json()
    return {"X-API-Key": body["api_key"]}


# --- listing ---------------------------------------------------------------------


def test_history_requires_api_key(client):
    resp = client.get("/executions")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_api_key"


def test_list_executions_newest_first_with_artifact_counts(
    client, key_and_header, fake_runner
):
    _, headers = key_and_header
    _run(client, headers, fake_runner)
    _run(client, headers, fake_runner,
         artifacts=[_art("a.txt", _b64_bytes(b"one")), _art("b.txt", _b64_bytes(b"two"))])
    _run(client, headers, fake_runner)

    body = client.get("/executions", headers=headers).json()
    assert [e["id"] for e in body["executions"]] == [3, 2, 1]
    assert body["limit"] == 20 and body["offset"] == 0
    assert [e["artifact_count"] for e in body["executions"]] == [0, 2, 0]
    row = body["executions"][0]
    assert row["status"] == "completed" and row["exit_code"] == 0
    assert row["session_id"] is None
    assert row["created_at"] and row["finished_at"]
    assert "code" not in row and "stdout" not in row  # listing stays lean


def test_list_executions_pagination(client, key_and_header, fake_runner):
    _, headers = key_and_header
    for _ in range(3):
        _run(client, headers, fake_runner)

    page1 = client.get("/executions?limit=2", headers=headers).json()
    assert [e["id"] for e in page1["executions"]] == [3, 2]
    page2 = client.get("/executions?limit=2&offset=2", headers=headers).json()
    assert [e["id"] for e in page2["executions"]] == [1]


def test_list_executions_status_filter(client, key_and_header, fake_runner):
    _, headers = key_and_header
    _run(client, headers, fake_runner)
    _run(client, headers, fake_runner,
         result=SolariExecutionResult(status="timeout", error="too slow"))
    failed = SolariExecutionResult(stdout="", stderr="boom", exit_code=1,
                                   status="failed", error="boom")
    _run(client, headers, fake_runner, result=failed)

    timeouts = client.get("/executions?status=timeout", headers=headers).json()
    assert [e["id"] for e in timeouts["executions"]] == [2]
    assert timeouts["executions"][0]["status"] == "timeout"
    completed = client.get("/executions?status=completed", headers=headers).json()
    assert [e["id"] for e in completed["executions"]] == [1]
    assert client.get("/executions?status=failed", headers=headers).json()["executions"][0]["id"] == 3


def test_list_executions_rejects_bad_paging(client, key_and_header):
    _, headers = key_and_header
    for query in ("limit=0", "limit=101", "offset=-1"):
        resp = client.get(f"/executions?{query}", headers=headers)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_request"


# --- detail ------------------------------------------------------------------------


def test_detail_hides_code_by_default_includes_on_request(
    client, key_and_header, fake_runner
):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner, stdout="hi\n")["execution_id"]

    default = client.get(f"/executions/{exec_id}", headers=headers).json()
    assert default["id"] == exec_id
    assert default["code"] is None  # full code is opt-in, never leaked by default
    assert default["stdout"] == "hi\n"
    assert default["duration_ms"] is not None

    full = client.get(f"/executions/{exec_id}?include_code=true", headers=headers).json()
    assert full["code"] == "pass"


def test_detail_artifacts_and_duration(client, key_and_header, fake_runner):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner,
                   artifacts=[_art("out.txt", _b64_bytes(b"data"))])["execution_id"]
    detail = client.get(f"/executions/{exec_id}", headers=headers).json()
    assert detail["artifacts"][0]["filename"] == "out.txt"
    assert detail["artifacts"][0]["download_available"] is True
    assert "data" not in detail["artifacts"][0]


# --- artifacts listing ---------------------------------------------------------------


def test_artifacts_listing(client, key_and_header, fake_runner):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner, artifacts=[
        _art("log.txt", _b64_bytes(b"log line")),
        _art("big.bin", _b64_bytes(b"\xff\xfe\x00\x01"), mime_type="application/octet-stream"),
    ])["execution_id"]

    body = client.get(f"/executions/{exec_id}/artifacts", headers=headers).json()
    assert body["execution_id"] == exec_id
    assert [a["filename"] for a in body["artifacts"]] == ["log.txt", "big.bin"]
    assert [a["download_available"] for a in body["artifacts"]] == [True, False]


def test_artifacts_listing_404_for_missing_and_foreign(client, key_and_header, fake_runner):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner)["execution_id"]
    foreign = _other_key_header(client)

    missing = client.get("/executions/9999/artifacts", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    stolen = client.get(f"/executions/{exec_id}/artifacts", headers=foreign)
    # foreign and missing render identically, so ownership is not enumerable
    assert stolen.status_code == 404
    assert stolen.json()["error"]["code"] == missing.json()["error"]["code"]


# --- download ------------------------------------------------------------------------


def test_download_returns_stored_text(client, key_and_header, fake_runner):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner,
                   artifacts=[_art("report.txt", _b64_bytes(b"hello artifact"))])["execution_id"]
    art_id = client.get(f"/executions/{exec_id}/artifacts", headers=headers).json()["artifacts"][0]["id"]

    resp = client.get(f"/executions/{exec_id}/artifacts/{art_id}/download", headers=headers)
    assert resp.status_code == 200
    assert resp.text == "hello artifact"
    assert resp.headers["content-type"].startswith("text/plain")
    disposition = resp.headers["content-disposition"]
    assert "attachment" in disposition and 'filename="report.txt"' in disposition
    assert resp.headers["x-artifact-filename"] == "report.txt"


def test_download_forces_octet_stream_for_non_text_mime(client, key_and_header, fake_runner):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner,
                   artifacts=[_art("data.json", _b64_bytes(b'{"a":1}'), mime_type="application/json")])["execution_id"]
    art_id = client.get(f"/executions/{exec_id}/artifacts", headers=headers).json()["artifacts"][0]["id"]

    resp = client.get(f"/executions/{exec_id}/artifacts/{art_id}/download", headers=headers)
    assert resp.status_code == 200
    assert resp.text == '{"a":1}'
    assert resp.headers["content-type"].startswith("application/octet-stream")


def test_download_oversized_artifact_is_404_artifact_not_stored(
    client, key_and_header, fake_runner
):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner,
                   artifacts=[_art("huge.txt", _b64_bytes(b"x" * 100_001))])["execution_id"]
    meta = client.get(f"/executions/{exec_id}/artifacts", headers=headers).json()["artifacts"][0]
    assert meta["download_available"] is False
    assert meta["size_bytes"] == 100_001  # size is known even though content is not kept

    art_id = meta["id"]
    resp = client.get(f"/executions/{exec_id}/artifacts/{art_id}/download", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "artifact_not_stored"


def test_boundary_artifact_size_is_stored(client, key_and_header, fake_runner):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner,
                   artifacts=[_art("edge.txt", _b64_bytes(b"x" * 100_000))])["execution_id"]
    meta = client.get(f"/executions/{exec_id}/artifacts", headers=headers).json()["artifacts"][0]
    assert meta["download_available"] is True


def test_download_binary_artifact_is_404_artifact_not_stored(
    client, key_and_header, fake_runner
):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner,
                   artifacts=[_art("blob.bin", _b64_bytes(b"\xff\xfe\x00\x01"))])["execution_id"]
    meta = client.get(f"/executions/{exec_id}/artifacts", headers=headers).json()["artifacts"][0]
    assert meta["download_available"] is False

    resp = client.get(
        f"/executions/{exec_id}/artifacts/{meta['id']}/download", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "artifact_not_stored"


def test_undecodable_artifact_keeps_metadata_only(client, key_and_header, fake_runner):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner,
                   artifacts=[_art("junk.txt", "not-valid-base64!!!")])["execution_id"]
    meta = client.get(f"/executions/{exec_id}/artifacts", headers=headers).json()["artifacts"][0]
    assert meta["filename"] == "junk.txt"
    assert meta["size_bytes"] == 0
    assert meta["download_available"] is False


def test_download_rejects_artifact_from_other_execution(client, key_and_header, fake_runner):
    _, headers = key_and_header
    exec1 = _run(client, headers, fake_runner,
                 artifacts=[_art("one.txt", _b64_bytes(b"one"))])["execution_id"]
    exec2 = _run(client, headers, fake_runner,
                 artifacts=[_art("two.txt", _b64_bytes(b"two"))])["execution_id"]
    art2 = client.get(f"/executions/{exec2}/artifacts", headers=headers).json()["artifacts"][0]["id"]

    resp = client.get(f"/executions/{exec1}/artifacts/{art2}/download", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert client.get(f"/executions/{exec1}/artifacts/9999/download",
                      headers=headers).status_code == 404


# --- ownership -----------------------------------------------------------------------


def test_user_cannot_access_other_users_executions(client, key_and_header, fake_runner):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner,
                   artifacts=[_art("mine.txt", _b64_bytes(b"secret"))])["execution_id"]
    art_id = client.get(f"/executions/{exec_id}/artifacts", headers=headers).json()["artifacts"][0]["id"]
    foreign = _other_key_header(client)

    paths = [
        f"/executions/{exec_id}",
        f"/executions/{exec_id}/artifacts",
        f"/executions/{exec_id}/artifacts/{art_id}/download",
    ]
    for path in paths:
        resp = client.get(path, headers=foreign)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    # owner still sees everything
    assert client.get(f"/executions/{exec_id}", headers=headers).status_code == 200
    assert client.get(f"/executions/{exec_id}/artifacts/{art_id}/download",
                      headers=headers).text == "secret"


def test_history_list_is_scoped_to_caller(client, key_and_header, fake_runner):
    _, headers = key_and_header
    _run(client, headers, fake_runner)
    foreign = _other_key_header(client)
    body = client.get("/executions", headers=foreign).json()
    assert body["executions"] == []


# --- QA additions: auth, encoding paths, abuse cases, observability -----------------


def test_history_endpoints_reject_invalid_and_inactive_keys(
    client, key_and_header, engine
):
    _, headers = key_and_header
    exec_id = 1  # no execution needed: auth is checked before any lookup
    paths = [
        "/executions",
        f"/executions/{exec_id}",
        f"/executions/{exec_id}/artifacts",
        f"/executions/{exec_id}/artifacts/1/download",
    ]
    for path in paths:
        resp = client.get(path, headers={"X-API-Key": "ek_live_totallywrongkey"})
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_api_key"

    # deactivate the fixture's key and prove deactivation beats data access
    with DBSession(engine) as db:
        key = db.exec(select(ApiKey).where(ApiKey.email == "qa@example.com")).first()
        key.is_active = False
        db.add(key)
        db.commit()
    for path in paths:
        resp = client.get(path, headers=headers)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "api_key_inactive"


def test_non_base64_artifact_encoding_is_stored_verbatim(
    client, key_and_header, fake_runner
):
    """encoding != base64 takes the utf-8 encode path; size matches the bytes."""
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner, artifacts=[{
        "filename": "plain.txt", "path": "/tmp/exekit/plain.txt",
        "mime_type": "text/plain", "encoding": "identity", "data": "raw text",
    }])["execution_id"]
    meta = client.get(f"/executions/{exec_id}/artifacts", headers=headers).json()["artifacts"][0]
    assert meta["size_bytes"] == len("raw text".encode())
    assert meta["download_available"] is True
    art_id = meta["id"]
    resp = client.get(f"/executions/{exec_id}/artifacts/{art_id}/download", headers=headers)
    assert resp.status_code == 200
    assert resp.text == "raw text"


def test_download_filename_with_quotes_and_newline_is_sanitized(
    client, key_and_header, fake_runner
):
    """Header-injection attempts fall back to artifact.txt in the header while
    X-Artifact-Filename keeps the quoted original for the client."""
    _, headers = key_and_header
    evil = 'bad"\r\nX-Evil: injected.txt'
    exec_id = _run(client, headers, fake_runner,
                   artifacts=[{"filename": evil, "path": None,
                               "mime_type": "text/plain", "encoding": "base64",
                               "data": _b64_bytes(b"x")}])["execution_id"]
    art_id = client.get(f"/executions/{exec_id}/artifacts", headers=headers).json()["artifacts"][0]["id"]
    resp = client.get(f"/executions/{exec_id}/artifacts/{art_id}/download", headers=headers)
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert "X-Evil" not in disposition
    assert "\r" not in disposition and "\n" not in disposition
    assert 'filename="artifact.txt"' in disposition


def test_infra_failed_executions_appear_in_history(client, key_and_header, fake_runner):
    """503 solari_unconfigured rows are recorded and listed like any other."""
    fake_runner.set_status("solari_unconfigured",
                           error="SOLARI_API_KEY is not set on the server")
    _, headers = key_and_header
    resp = client.post("/executions", json={"code": "print(1)"}, headers=headers)
    assert resp.status_code == 503
    body = client.get("/executions", headers=headers).json()
    assert len(body["executions"]) == 1
    row = body["executions"][0]
    assert row["status"] == "solari_unconfigured"
    assert row["exit_code"] is None and row["artifact_count"] == 0
    detail = client.get(f"/executions/{row['id']}", headers=headers).json()
    assert detail["error"] is not None
    assert detail["code"] is None  # include_code not requested


def test_history_is_read_only_and_repeatable(client, key_and_header, fake_runner):
    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner,
                   artifacts=[_art("r.txt", _b64_bytes(b"data"))])["execution_id"]
    first = client.get(f"/executions/{exec_id}", headers=headers).json()
    second = client.get(f"/executions/{exec_id}", headers=headers).json()
    assert first == second  # no state mutated by reads
    # and downloads do not consume or change anything
    art_id = first["artifacts"][0]["id"]
    for _ in range(2):
        assert client.get(
            f"/executions/{exec_id}/artifacts/{art_id}/download",
            headers=headers).text == "data"


def test_artifact_download_is_logged_without_content(
    client, key_and_header, fake_runner, caplog
):
    """The audit log line carries ids and byte size, never artifact content."""
    import logging as _logging

    _, headers = key_and_header
    exec_id = _run(client, headers, fake_runner,
                   artifacts=[_art("audit.txt", _b64_bytes(b"secret-content"))])["execution_id"]
    art_id = client.get(f"/executions/{exec_id}/artifacts", headers=headers).json()["artifacts"][0]["id"]
    with caplog.at_level(_logging.INFO, logger="exekit.history"):
        client.get(f"/executions/{exec_id}/artifacts/{art_id}/download", headers=headers)
    lines = [r.getMessage() for r in caplog.records if r.name == "exekit.history"]
    assert any("artifact downloaded" in m for m in lines)
    assert all("secret-content" not in m for m in lines)


# --- QA additions: remaining-risk tests (fault injection, concurrency) --------------

import pytest


def test_crash_between_execution_and_artifact_commit_degrades_cleanly(
    client, key_and_header, fake_runner, engine, monkeypatch
):
    """A crash after the execution row commits but before artifacts persist
    leaves a completed execution with zero artifacts: no corruption, no
    orphaned rows, and history degrades to download_available=false."""
    import app.services.executions as svc

    _, headers = key_and_header
    original = svc.persist_artifacts

    def exploding_persist(db, execution_id, artifacts):
        if artifacts:
            raise RuntimeError("simulated crash before artifact commit")
        return original(db, execution_id, artifacts)

    monkeypatch.setattr(svc, "persist_artifacts", exploding_persist)
    fake_runner.set_completed(stdout="hi\n",
                              artifacts=[_art("out.txt", _b64_bytes(b"data"))])
    # TestClient re-raises the server error (a real server would render a 500);
    # the point is that the failure surfaces instead of silently dropping data.
    with pytest.raises(RuntimeError, match="simulated crash"):
        client.post("/executions", json={"code": "pass"}, headers=headers)

    # the execution row itself survived and lists cleanly with no artifacts
    body = client.get("/executions", headers=headers).json()
    assert len(body["executions"]) == 1
    assert body["executions"][0]["artifact_count"] == 0
    detail = client.get(f"/executions/{body['executions'][0]['id']}",
                        headers=headers).json()
    assert detail["status"] == "completed"
    assert detail["artifacts"] == []


def test_concurrent_history_writes_and_reads_do_not_conflict(
    client, key_and_header, fake_runner, engine
):
    """Parallel runs against one key while another reader lists history:
    every run persists exactly one execution and every read sees a
    consistent (possibly partial) snapshot — no crashes, no torn rows."""
    import threading

    _, headers = key_and_header
    reader_stop = threading.Event()
    reader_errors: list[str] = []
    run_errors: list[str] = []

    def reader():
        while not reader_stop.is_set():
            resp = client.get("/executions?limit=100", headers=headers)
            if resp.status_code != 200:
                reader_errors.append(f"{resp.status_code}: {resp.text[:100]}")
                continue
            rows = resp.json()["executions"]
            # every visible row must be internally consistent
            for r in rows:
                if r["status"] not in ("completed", "failed", "timeout",
                                       "solari_unconfigured", "solari_unavailable"):
                    reader_errors.append(f"torn row: {r}")

    barrier = threading.Barrier(4)

    def runner(n):
        barrier.wait()
        resp = client.post("/executions", json={"code": f"print({n})"},
                           headers=headers)
        if resp.status_code != 200:
            run_errors.append(f"{resp.status_code}: {resp.text[:100]}")

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    run_threads = [threading.Thread(target=runner, args=(n,)) for n in range(4)]
    for t in run_threads:
        t.start()
    for t in run_threads:
        t.join()
    reader_stop.set()
    reader_thread.join()

    assert run_errors == [] and reader_errors == []
    body = client.get("/executions?limit=100", headers=headers).json()
    assert len(body["executions"]) == 4  # all runs persisted, none lost
    assert all(r["status"] == "completed" for r in body["executions"])
