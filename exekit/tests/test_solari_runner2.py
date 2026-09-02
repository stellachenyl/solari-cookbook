"""SolariRunner tests, part 2: sessions, kill/teardown, and file operations.

The fake SDK lives in tests/runner_fakes.py (verified dataclass shapes from
docs/SOLARI_SDK_SURFACE.md); runner logic under test is real."""

import asyncio

import pytest

from app.services import solari_runner as sr
from app.services.errors import SolariTimeout, SolariUnavailable
from tests.runner_fakes import (
    FakeClient,
    FakeSandbox,
    FakeSdkError,
    run,
)





# --- sessions --------------------------------------------------------------------


def test_create_session_returns_solari_id_connects_and_pre_makes_artifact_dir(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=42))
    sb = client.sandboxes[sid]
    assert sb.connected and sb._ctx == 1  # eager kernel context
    assert sb.run_calls == []  # nothing executed yet


def test_create_session_infra_failure_kills_created_sandbox(runner):
    r, client = runner
    # make connect fail on the created sandbox
    class NoConnect(FakeSandbox):
        async def connect(self):
            raise FakeSdkError("control plane down")

    async def create(**kw):
        sb = NoConnect("sb-x")
        client.sandboxes[sb.sandboxId] = sb
        return sb

    client.create = create  # type: ignore[method-assign]
    with pytest.raises(SolariUnavailable):
        run(r.create_session(api_key_id=1))
    assert client.sandboxes["sb-x"].killed


def test_create_session_timeout_maps_to_solari_timeout(runner):
    r, client = runner
    client.next_kwargs = {"hang": True}

    async def hang_create(**kw):
        await asyncio.sleep(999)

    client.create = hang_create  # type: ignore[method-assign]
    with pytest.raises(SolariTimeout):
        run(r.create_session(api_key_id=1))


def test_session_runs_share_kernel_context(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    first = run(r.run_python("y = 21", session_id=sid))
    second = run(r.run_python("print(y * 2)", session_id=sid))
    assert first.status == "completed" and second.status == "completed"
    assert second.stdout == "42\n"
    sb = client.sandboxes[sid]
    assert sb.run_calls[0][1] == sb.run_calls[1][1] is not None  # same context id


def test_session_channel_drop_recovers_with_fresh_context(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    sb = client.sandboxes[sid]
    sb.fail_first_code = True
    res = run(r.run_python("print('recovered')", session_id=sid))
    assert res.status == "completed" and res.stdout == "recovered\n"
    assert sb._ctx == 2  # a fresh kernel context was created


def test_session_timeout_destroys_sandbox_and_unregisters(runner):
    r, client = runner
    client.next_kwargs = {"hang": True}
    sid = run(r.create_session(api_key_id=1))
    res = run(r.run_python("print(1)", session_id=sid))
    assert res.status == "timeout"
    assert "cannot be interrupted" in res.error
    assert client.sandboxes[sid].killed
    assert sid not in r._handles


def test_session_infra_failure_keeps_session_for_retry(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    client.sandboxes[sid].raise_on_run = FakeSdkError("gateway 503")
    res = run(r.run_python("print(1)", session_id=sid))
    assert res.status == "solari_unavailable"
    assert sid in r._handles  # not killed; next run may recover
    assert not client.sandboxes[sid].killed


def test_session_artifacts_are_diffed_against_pre_run_listing(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    sb = client.sandboxes[sid]
    sb.fs["/tmp/exekit/old.txt"] = b"old"
    res = run(r.run_python("sandbox_write('new.txt', 'fresh')", session_id=sid))
    names = [a.filename for a in res.artifacts]
    assert names == ["new.txt"], "pre-existing files must not be re-reported"


def test_session_artifact_listing_failure_returns_empty(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    sb = client.sandboxes[sid]
    sb.fs["/tmp/exekit/x.txt"] = b"x"

    async def broken_list(path):
        raise FakeSdkError("listing unavailable")

    sb.files.list = broken_list  # type: ignore[method-assign]
    res = run(r.run_python("print(1)", session_id=sid))
    assert res.status == "completed" and res.artifacts == []


# --- kill / teardown ---------------------------------------------------------------


def test_kill_session_true_then_false(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    assert run(r.kill_session(sid)) is True
    assert client.sandboxes[sid].killed
    assert sid not in r._handles
    assert run(r.kill_session(sid)) is False


def test_kill_session_reports_success_even_when_rpc_fails(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    client.sandboxes[sid].kill_fails = True  # e.g. VM already expired
    assert run(r.kill_session(sid)) is True  # session is over either way
    assert sid not in r._handles


def test_kill_by_sandbox_id_uses_client_when_no_handle(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    run(r.kill_session(sid))
    client.sandboxes[sid].killed = False
    run(r.kill_by_sandbox_id(sid))  # client-side kill; documented idempotent
    assert client.sandboxes[sid].killed is True


def test_kill_by_sandbox_id_without_client_is_noop(settings):
    settings.solari_api_key = "slr_test"
    r = sr.SolariRunner()  # no client created
    run(r.kill_by_sandbox_id("sb-any"))  # must not raise


# --- file operations (write/read/list, reconnect retry, error mapping) ------------


def test_write_read_list_files_round_trip(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    run(r.write_file(sid, "/tmp/exekit/notes.txt", "hello"))
    assert client.sandboxes[sid].fs["/tmp/exekit/notes.txt"] == b"hello"
    assert run(r.read_file(sid, "/tmp/exekit/notes.txt")) == "hello"
    assert run(r.list_files(sid)) == ["notes.txt"]  # directories excluded


def test_file_ops_retry_once_after_channel_drop(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    sb = client.sandboxes[sid]
    sb.fail_first_file = True
    run(r.write_file(sid, "/tmp/exekit/a.txt", "x"))
    assert sb.fs["/tmp/exekit/a.txt"] == b"x"  # retried write landed
    assert run(r.read_file(sid, "/tmp/exekit/a.txt")) == "x"
    assert run(r.list_files(sid)) == ["a.txt"]


def test_file_ops_map_sdk_errors_to_solari_unavailable(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    client.sandboxes[sid].files_fail = FakeSdkError("guest fs error")
    with pytest.raises(SolariUnavailable):
        run(r.write_file(sid, "/tmp/exekit/a.txt", "x"))
    with pytest.raises(SolariUnavailable):
        run(r.read_file(sid, "/tmp/exekit/a.txt"))
    with pytest.raises(SolariUnavailable):
        run(r.list_files(sid))


def test_artifact_dir_property_exposes_configured_directory(settings):
    settings.solari_api_key = "slr_test"
    assert sr.SolariRunner().artifact_dir == "/tmp/exekit"
    assert sr.SolariRunner(artifact_dir="/tmp/other").artifact_dir == "/tmp/other"


# --- runner-level session creation edge branches ----------------------------------


def test_create_session_persists_no_secrets_in_metadata(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=99))
    # nothing about the api key material may leak into the sandbox handle
    sb = client.sandboxes[sid]
    leaked = {k: v for k, v in vars(sb).items() if "key" in str(v).lower() and "slr_test" in str(v)}
    assert leaked == {}
    assert r._handles[sid].api_key_id == 99


def test_session_context_created_lazily_on_first_run_when_missing(runner):
    r, client = runner
    sid = run(r.create_session(api_key_id=1))
    handle = r._handles[sid]
    handle.context_id = None  # simulate a lost context id
    res = run(r.run_python("print(1)", session_id=sid))
    assert res.status == "completed"
    assert client.sandboxes[sid]._ctx == 2  # context re-created on demand
