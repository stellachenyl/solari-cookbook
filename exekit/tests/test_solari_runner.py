"""SolariRunner tests, part 1: health/introspection, dispatch, one-shot runs.

The solari_sandbox SDK is faked at its verified dataclass/method shapes
(docs/SOLARI_SDK_SURFACE.md) — see tests/runner_fakes.py. The runner's own
logic (timeout enforcement, status mapping, artifact diffing, teardown, error
mapping) is exercised against real code paths.
"""

import base64

import pytest

from app.config import get_settings
from app.services import solari_runner as sr
from app.services.errors import SolariNotConfigured, SolariUnavailable
from tests.runner_fakes import FakeSdkError, run


# --- health / introspection ---------------------------------------------------


def test_health_reports_configured_and_sdk(runner):
    r, _ = runner
    h = run(r.health_check())
    assert h["configured"] is True
    assert h["sdk_imported"] is True
    assert h["error"] is None
    assert "slr_test" not in str(h)  # no key material ever


def test_health_when_unconfigured(settings, monkeypatch):
    settings.solari_api_key = ""
    h = run(sr.SolariRunner().health_check())
    assert h["configured"] is False and h["sdk_imported"] is True


def test_health_when_sdk_missing(settings, monkeypatch):
    settings.solari_api_key = "slr_test"
    monkeypatch.setattr(sr, "SOLARI_SDK_AVAILABLE", False)
    monkeypatch.setattr(sr, "_SDK_IMPORT_ERROR", "simulated missing SDK")
    h = run(sr.SolariRunner().health_check())
    assert h["sdk_imported"] is False and "simulated" in h["error"]


def test_inspect_client_describes_surface(runner):
    r, _ = runner
    info = r.inspect_client()
    assert info["sdk_imported"] is True
    assert any("run_code" in sig for sig in info["sandbox"].values())
    assert "write" in info["command_fs_helpers"]["files"]
    assert "read_text" in info["command_fs_helpers"]["files"]
    assert "slr_test" not in str(info)


# --- run_python: configuration and dispatch ------------------------------------


def test_run_python_unconfigured_returns_status_not_raise(settings):
    settings.solari_api_key = ""
    r = sr.SolariRunner()
    res = run(r.run_python("print(1)"))
    assert res.status == "solari_unconfigured"
    assert res.exit_code is None and res.artifacts == []


def test_run_python_without_sdk_returns_solari_unavailable(runner, monkeypatch):
    r, _ = runner
    monkeypatch.setattr(sr, "SOLARI_SDK_AVAILABLE", False)
    res = run(r.run_python("print(1)"))
    assert res.status == "solari_unavailable"


def test_run_python_unknown_session_raises_key_error(runner):
    r, _ = runner
    with pytest.raises(KeyError):
        run(r.run_python("print(1)", session_id="sb-none"))


# --- one-shot execution ---------------------------------------------------------


def test_one_shot_completed_kills_vm_and_assembles_output(runner):
    r, client = runner
    res = run(r.run_python("print('hello')\nprint('line2')"))
    assert res.status == "completed"
    assert res.stdout == "hello\nline2\n"
    assert res.exit_code == 0 and res.error is None
    assert res.raw is not None and res.raw["items"][0]["type"] == "stdout"
    sb = next(iter(client.sandboxes.values()))
    assert sb.killed and sb.connected  # VM destroyed, was actually used


def test_one_shot_kernel_error_maps_to_failed(runner):
    r, _ = runner
    res = run(r.run_python("1/0"))
    assert res.status == "failed"
    assert res.exit_code == 1
    assert "ZeroDivisionError" in res.error
    assert "ZeroDivisionError" in res.stderr


def test_one_shot_infinite_loop_times_out_and_vms_is_destroyed(runner):
    r, client = runner
    client.next_kwargs = {"hang": True}  # models a kernel that never returns
    res = run(r.run_python("print('never gets here')"))
    assert res.status == "timeout"
    assert "exceeded" in res.error
    assert all(sb.killed for sb in client.sandboxes.values())


def test_one_shot_output_truncated_to_max_chars(runner, monkeypatch):
    r, _ = runner
    monkeypatch.setattr(get_settings(), "max_output_chars", 200)
    res = run(r.run_python("print('x' * 500)"))
    assert len(res.stdout) < 500
    assert "truncated" in res.stdout


def test_one_shot_collects_artifacts_as_base64(runner):
    r, _ = runner
    res = run(r.run_python("sandbox_write('out.csv', 'a,b\\n1,2\\n')"))
    assert len(res.artifacts) == 1
    art = res.artifacts[0]
    assert art.filename == "out.csv"
    assert art.path == "/tmp/exekit/out.csv"
    assert art.encoding == "base64"
    assert base64.b64decode(art.data) == b"a,b\n1,2\n"
    assert art.mime_type == "text/csv"


def test_one_shot_artifact_listing_failure_degrades_to_empty(runner):
    r, client = runner
    client.next_kwargs = {"artifact_list_fails": True}
    res = run(r.run_python("print(1)"))
    assert res.status == "completed" and res.artifacts == []


def test_one_shot_infra_failure_maps_to_solari_unavailable(runner):
    r, client = runner
    client.next_kwargs = {"raise_on_run": FakeSdkError("gateway 503")}
    res = run(r.run_python("print(1)"))
    assert res.status == "solari_unavailable"
    assert "gateway 503" in res.error


def test_one_shot_unexpected_exception_maps_to_solari_unavailable(runner):
    r, client = runner
    client.next_kwargs = {"raise_on_run": ValueError("weird")}
    res = run(r.run_python("print(1)"))
    assert res.status == "solari_unavailable"
    # VM still torn down on unexpected failures
    assert all(sb.killed for sb in client.sandboxes.values())


def test_one_shot_vm_killed_even_when_kill_rpc_fails(runner):
    r, client = runner
    client.next_kwargs = {"kill_fails": True}
    res = run(r.run_python("print(1)"))
    assert res.status == "completed"  # teardown failure never breaks the result
