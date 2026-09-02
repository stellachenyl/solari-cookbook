"""Fake solari_sandbox SDK shared by the SolariRunner test files.

Mirrors only the VERIFIED surface (docs/SOLARI_SDK_SURFACE.md): the
RunCodeResult/CodeResultItem field sets, the SessionHandle methods the
runner calls, and the two SDK exception bases it catches. No invented
behavior; the runner's own logic is what is under test.

Async helper `run()` and the `make_runner` fixture factory live here."""

import asyncio
import io
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field

import pytest

from app.config import get_settings
from app.services import solari_runner as sr


class FakeSdkError(Exception):
    """Stands in for solari_sandbox.SolariError (the SDK's base error)."""


class FakeChannelLost(Exception):
    """Stands in for solari_sandbox.ConnectionError."""


@dataclass
class FakeItem:
    """Field set mirrors the verified CodeResultItem dataclass."""

    type: str
    text: str | None = None
    png: str | None = None
    jpeg: str | None = None
    svg: str | None = None
    html: str | None = None
    latex: str | None = None
    json: object = None
    markdown: str | None = None
    chart: object = None


@dataclass
class FakeRunResult:
    results: list
    error: object = None
    charts: list = field(default_factory=list)


@dataclass
class FakeEntry:
    name: str
    dir: bool
    size: int = 0


class FakeFiles:
    def __init__(self, sb):
        self._sb = sb

    async def list(self, path):
        if self._sb.artifact_list_fails:
            raise FakeSdkError("listing unavailable")
        if self._sb.files_fail is not None:
            raise self._sb.files_fail
        return [
            FakeEntry(n.rsplit("/", 1)[-1], False, len(v))
            for n, v in self._sb.fs.items()
            if n.startswith(path + "/")
        ]

    async def read(self, path):
        if self._sb.files_fail is not None:
            raise self._sb.files_fail
        return self._sb.fs[path]

    async def read_text(self, path):
        if self._sb.files_fail is not None:
            raise self._sb.files_fail
        return self._sb.fs[path].decode()

    async def write(self, path, data, mode=None):
        if self._sb.fail_first_file and not self._sb.fs.get("_wrote_once"):
            self._sb.fs["_wrote_once"] = b"1"
            raise FakeChannelLost("channel dropped mid-write")
        if self._sb.files_fail is not None:
            raise self._sb.files_fail
        self._sb.fs[path] = data.encode() if isinstance(data, str) else data

    async def mkdir(self, path):
        pass


class FakeSandbox:
    """Mirrors the verified Sandbox/SessionHandle surface the runner uses:
    connect/reconnect/kill/create_code_context/run_code/files."""

    def __init__(self, sid, hang=False, raise_on_run=None, fail_first_code=False,
                 artifact_list_fails=False, kill_fails=False):
        self.sandboxId = sid
        self.id = sid
        self.connected = False
        self.killed = False
        self.hang = hang
        self.raise_on_run = raise_on_run
        self.fail_first_code = fail_first_code
        self.artifact_list_fails = artifact_list_fails
        self.files_fail = None
        self.fail_first_file = False
        self.kill_fails = kill_fails
        self.kernel: dict = {}
        self.fs: dict[str, bytes] = {}
        self.run_calls: list = []
        self._ctx = 0
        self.files = FakeFiles(self)

    async def connect(self):
        self.connected = True

    async def reconnect(self):
        self.connected = True

    async def kill(self):
        if self.kill_fails:
            raise FakeSdkError("already gone")
        self.killed = True

    async def create_code_context(self, language="python"):
        self._ctx += 1
        return f"ctx-{self._ctx}"

    async def run_code(self, code, *, language=None, context_id=None,
                       on_stdout=None, on_stderr=None):
        self.run_calls.append((code, context_id))
        if self.raise_on_run is not None:
            raise self.raise_on_run
        if self.hang:
            await asyncio.sleep(999)  # models a remote kernel; cancellable
        if self.fail_first_code and not self.kernel.get("_dropped_once"):
            self.kernel["_dropped_once"] = True
            raise FakeChannelLost("control channel dropped")
        # the exec'd code must not touch the host filesystem
        def _sandbox_write(name, content):
            self.fs[f"/tmp/exekit/{name}"] = content.encode()
        self.kernel["sandbox_write"] = _sandbox_write
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                exec(compile(code, "<kernel>", "exec"), self.kernel)
        except Exception as e:  # kernel error surface
            return FakeRunResult(results=[FakeItem("stderr", traceback.format_exc())],
                                 error=f"{type(e).__name__}: {e}")
        return FakeRunResult(results=[FakeItem("stdout", out.getvalue()),
                                      FakeItem("stderr", err.getvalue())])


class FakeClient:
    def __init__(self):
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.next_kwargs: dict = {}

    async def create(self, **kwargs):
        sid = f"sb-{len(self.sandboxes) + 1}"
        sb = FakeSandbox(sid, **self.next_kwargs)
        self.sandboxes[sid] = sb
        return sb

    async def kill(self, sid):
        self.sandboxes[sid].killed = True


@pytest.fixture()
def runner(monkeypatch, settings):
    """A SolariRunner wired to fakes, with the configured/test settings."""
    monkeypatch.setattr(sr, "SdkSolariError", FakeSdkError)
    monkeypatch.setattr(sr, "SdkConnectionError", FakeChannelLost)
    settings.solari_api_key = "slr_test_fake"
    settings.execution_timeout_seconds = 1
    r = sr.SolariRunner()
    fake_client = FakeClient()
    r._client = fake_client
    return r, fake_client


def run(coro):
    return asyncio.run(coro)


def make_runner(monkeypatch, settings):
    """SolariRunner wired to fakes with configured/test settings."""
    monkeypatch.setattr(sr, "SdkSolariError", FakeSdkError)
    monkeypatch.setattr(sr, "SdkConnectionError", FakeChannelLost)
    settings.solari_api_key = "slr_test_fake"
    settings.execution_timeout_seconds = 1
    r = sr.SolariRunner()
    fake_client = FakeClient()
    r._client = fake_client
    return r, fake_client


def run(coro):
    return asyncio.run(coro)
