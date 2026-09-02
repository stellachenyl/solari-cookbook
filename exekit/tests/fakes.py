"""FakeSolariRunner: the runner double used by route and wiring tests.

Implements exactly the surface the routes consume and returns
SolariExecutionResult objects built from verified SDK shapes. Scripted
behavior is explicit per test — no network, no randomness, fully
deterministic.
"""

import base64
from typing import Optional

from app.services.errors import UnsupportedSolariCapability
from app.services.solari_runner import SolariArtifact, SolariExecutionResult


class _Item:
    """Mirrors the verified CodeResultItem fields the runner joins."""

    def __init__(self, type: str, text: Optional[str] = None):
        self.type = type
        self.text = text
        self.png = self.jpeg = self.svg = self.html = None
        self.latex = self.markdown = None
        self.json = self.chart = None


class _RunResult:
    def __init__(self, results, error=None):
        self.results = results
        self.error = error
        self.charts = []


def b64(data: str) -> str:
    return base64.b64encode(data.encode()).decode()


class FakeSolariRunner:
    artifact_dir = "/tmp/exekit"

    def __init__(self):
        self.created: list[tuple[str, int]] = []
        self.killed: list[str] = []
        self.runs: list[tuple[str, Optional[str]]] = []
        self.files: dict[str, str] = {}
        self._counter = 0
        # scriptable behavior:
        self.next_result: Optional[SolariExecutionResult] = None
        self.next_run_exception: Optional[Exception] = None
        self.unsupported_files = False

    # -- scripting helpers used by tests -------------------------------------

    def set_completed(self, stdout: str = "", stderr: str = "", exit_code: int = 0,
                      artifacts: Optional[list] = None) -> None:
        self.next_result = SolariExecutionResult(
            stdout=stdout, stderr=stderr, exit_code=exit_code,
            status="completed", artifacts=artifacts or [],
            raw={"items": [], "charts": 0, "error": None},
        )

    def set_status(self, status: str, error: Optional[str] = None) -> None:
        self.next_result = SolariExecutionResult(status=status, error=error)

    # -- runner surface ---------------------------------------------------------

    async def create_session(self, api_key_id: int) -> str:
        self._counter += 1
        sid = f"sb-{self._counter}"
        self.created.append((sid, api_key_id))
        return sid

    async def kill_session(self, session_id: str) -> bool:
        self.killed.append(session_id)
        return any(sid == session_id for sid, _ in self.created)

    async def kill_by_sandbox_id(self, sandbox_id: str) -> None:
        self.killed.append(f"client:{sandbox_id}")

    async def run_python(self, code: str, session_id: Optional[str] = None) -> SolariExecutionResult:
        self.runs.append((code, session_id))
        if self.next_run_exception is not None:
            raise self.next_run_exception
        return self.next_result

    async def read_file(self, session_id: str, path: str) -> str:
        if self.unsupported_files:
            raise UnsupportedSolariCapability("file reads unsupported by this backend")
        if path not in self.files:
            raise KeyError(path)
        return self.files[path]

    # -- result builders for runner-level tests ---------------------------------


def code_result(stdout: str = "", stderr: str = "", error=None):
    """Build a RunCodeResult-shaped object from verified dataclass fields."""
    items: list[_Item] = []
    if stdout:
        items.append(_Item("stdout", stdout))
    if stderr:
        items.append(_Item("stderr", stderr))
    return _RunResult(items, error=error)
