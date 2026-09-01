"""Introspect the installed Solari SDK and write docs/SOLARI_INTROSPECTION.txt.

Offline by default (no API key needed): imports the SDK, dumps public exports,
key class signatures, and result/error shapes to the report file and stdout.

With --live (and SOLARI_API_KEY set), additionally runs a real sandbox round
trip that answers the two UNVERIFIED items from docs/SOLARI_SDK_SURFACE.md §K:
whether a kernel context survives close() + client.connect() re-attach, and
whether files work right after connect().
"""

import argparse
import asyncio
import inspect
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "docs" / "SOLARI_INTROSPECTION.txt"

RESULT_TYPES = ("RunCodeResult", "CodeResultItem", "CommandResult", "ExecResult",
                "FsEntry", "FsStat", "SandboxView", "CreateSandboxResponse")
HELPER_CLASSES = ("_Files", "_Commands", "_Pty", "_Git", "_Volumes")


def _sdk():
    try:
        import solari_sandbox as sdk
        return sdk, None
    except ImportError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _sdk_version() -> str:
    try:
        from importlib.metadata import version
        return version("solari-sandbox")
    except Exception:
        return "unknown"


def _class_report(cls: type, include_private: bool = False) -> list[str]:
    lines: list[str] = []
    doc = inspect.getdoc(cls) or ""
    lines.append(f"class {cls.__module__}.{cls.__qualname__}: {doc.splitlines()[0] if doc else ''}")
    try:
        lines.append(f"  __init__{inspect.signature(cls.__init__)}")
    except (ValueError, TypeError):
        pass
    for name, member in inspect.getmembers(cls):
        if name.startswith("__"):
            continue
        if name.startswith("_") and not include_private:
            continue
        if isinstance(member, property):
            lines.append(f"  @property {name}")
        elif inspect.isfunction(member):
            try:
                sig = f"{name}{inspect.signature(member)}"
            except (ValueError, TypeError):
                sig = f"{name}(...)"
            first_doc = (inspect.getdoc(member) or "").splitlines()[0:1]
            suffix = f"  # {first_doc[0]}" if first_doc else ""
            lines.append(f"  {sig}{suffix}")
    return lines


def build_report() -> str:
    lines: list[str] = []
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines.append("SOLARI SDK INTROSPECTION")
    lines.append(f"generated: {stamp}")
    lines.append(f"python:    {sys.version.split()[0]}")
    lines.append("source:    live introspection of the installed package "
                 "(cross-check against docs/SOLARI_SDK_SURFACE.md)")
    lines.append("")

    sdk, import_error = _sdk()
    if sdk is None:
        lines.append(f"SDK IMPORT FAILED: {import_error}")
        lines.append("ExecKit is designed to boot without the SDK; health_check()")
        lines.append("reports this as sdk_imported=false.")
        return "\n".join(lines) + "\n"

    lines.append(f"sdk imported: yes (solari-sandbox {_sdk_version()})")
    lines.append("")

    for module_name in ("solari_sandbox", "solari_core"):
        module = __import__(module_name)
        lines.append(f"=== {module_name} exports ===")
        for name in sorted(n for n in dir(module) if not n.startswith("_")):
            member = getattr(module, name)
            kind = type(member).__name__
            lines.append(f"  {name} ({kind})")
        lines.append("")

    lines.append("=== literal types ===")
    for name in ("CodeLanguage", "SandboxState", "SandboxKind"):
        member = getattr(sdk, name, None)
        if member is not None:
            lines.append(f"  {name} = {member!r}")
    lines.append("")

    lines.append("=== client classes ===")
    for cls_name in ("SandboxClient", "SyncSandboxClient"):
        cls = getattr(sdk, cls_name, None)
        if cls is not None:
            lines.extend(_class_report(cls))
            lines.append("")

    lines.append("=== session handle (Sandbox / shared base) ===")
    lines.extend(_class_report(sdk.Sandbox, include_private=True))
    lines.append("")

    lines.append("=== method namespaces (instance attributes of SessionHandle) ===")
    from solari_core import handle as handle_mod
    for cls_name in HELPER_CLASSES:
        cls = getattr(handle_mod, cls_name, None)
        if cls is not None:
            lines.extend(_class_report(cls))
            lines.append("")

    lines.append("=== result / data types ===")
    from dataclasses import is_dataclass
    for type_name in RESULT_TYPES:
        obj = getattr(sdk, type_name, None)
        if obj is None:
            continue
        if is_dataclass(obj):
            hints = getattr(obj, "__annotations__", {})
            fields = ", ".join(f"{k}: {v}" for k, v in hints.items())
            lines.append(f"@dataclass {type_name}({fields})")
            doc = (inspect.getdoc(obj) or "").splitlines()[0:1]
            if doc:
                lines.append(f"  # {doc[0]}")
        else:
            lines.append(f"{type_name} = {obj!r}")
    lines.append("")

    lines.append("=== error classes ===")
    for name in sorted(n for n in dir(sdk) if n.endswith("Error")):
        cls = getattr(sdk, name)
        doc = (inspect.getdoc(cls) or "").splitlines()[0:1]
        lines.append(f"  {name}: {doc[0] if doc else ''}")
    lines.append("")

    return "\n".join(lines)


async def live_report() -> str:
    """Real sandbox round trip. Requires SOLARI_API_KEY in the environment."""
    lines = ["=== LIVE ROUND TRIP (--live) ==="]
    api_key = os.environ.get("SOLARI_API_KEY", "").strip()
    if not api_key:
        lines.append("SKIPPED: SOLARI_API_KEY is not set in the environment.")
        return "\n".join(lines) + "\n"

    sdk, _ = _sdk()
    assert sdk is not None
    from solari_sandbox import SandboxClient

    client = SandboxClient(api_key=api_key, base_url="https://api.getsolari.com")
    sandbox = None
    reattached = None

    def step(label: str) -> None:
        lines.append(f"-- {label}")

    try:
        step("1. client.create(template='base', timeout_ms=5*60_000)")
        sandbox = await client.create(template="base", timeout_ms=5 * 60_000)
        lines.append(f"   OK sandboxId={sandbox.sandboxId}")

        step("2. sandbox.connect()")
        await sandbox.connect()
        lines.append("   OK control channel open")

        step("3. create_code_context('python')")
        ctx = await sandbox.create_code_context("python")
        lines.append(f"   OK contextId={ctx}")

        step("4. run_code stateful #1: 'exekit_probe = 40'")
        await sandbox.run_code("exekit_probe = 40", context_id=ctx)
        lines.append("   OK")

        step("5. run_code stateful #2: 'print(exekit_probe + 2)' (same context)")
        r = await sandbox.run_code("print(exekit_probe + 2)", context_id=ctx)
        out = "".join(i.text or "" for i in r.results if i.type == "stdout")
        lines.append(f"   OK stdout={out.strip()!r} error={r.error!r}")
        lines.append(f"   -> stateful kernel works across calls: {out.strip() == '42'}")

        step("6. UNVERIFIED ITEM A: context survival across re-attach")
        await sandbox.close()
        reattached = await client.connect(sandbox.sandboxId)
        await reattached.connect()
        r2 = await reattached.run_code("print(exekit_probe)", context_id=ctx)
        if r2.error:
            lines.append(f"   CONTEXT LOST after re-attach (error={r2.error!r}); "
                         "ExecKit must fall back to a fresh context")
        else:
            out2 = "".join(i.text or "" for i in r2.results if i.type == "stdout")
            lines.append(f"   stdout={out2.strip()!r}")
            lines.append(f"   -> context survives close()+client.connect() "
                         f"re-attach (same process): {out2.strip() == '40'}")

        step("7. UNVERIFIED ITEM B: files right after connect")
        await reattached.files.write("/tmp/exekit_probe.txt", "probe\n")
        text = await reattached.files.read_text("/tmp/exekit_probe.txt")
        listing = await reattached.files.list("/tmp")
        names = [e.name for e in listing if e.name.startswith("exekit_probe")]
        lines.append(f"   write/read round trip: {text!r}; visible in list: {names}")

        step("8. kill")
    except Exception as exc:
        lines.append(f"   FAILED: {type(exc).__name__}: {exc}")
    finally:
        for sb in (reattached, sandbox):
            if sb is not None:
                try:
                    await sb.kill()
                    lines.append(f"   killed {sb.sandboxId}")
                except Exception as exc:
                    lines.append(f"   kill failed for {sb.sandboxId}: {exc}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="also run a real sandbox round trip (requires SOLARI_API_KEY)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help=f"report path (default: {DEFAULT_OUT})")
    args = parser.parse_args()

    report = build_report()
    if args.live:
        report += "\n" + asyncio.run(live_report())

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nreport written to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
