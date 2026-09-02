"""Route tests for execution history and artifact endpoints: listing,
pagination, status filtering, detail (include_code), artifact metadata,
download, and cross-user ownership."""

import base64

from app.models import ExecutionArtifact  # noqa: F401  (registers tables for standalone runs)
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
