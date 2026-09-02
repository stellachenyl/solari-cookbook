"""Errors raised by SolariRunner.

These are subclasses of the ExecKit domain errors (app/errors.py), so an
exception escaping the runner is already a well-formed domain error with a
stable code and HTTP status — the API layer never has to translate.

Runner-specific semantics preserved:
- SolariTimeout is an ExecutionTimeout (504).
- UnsupportedSolariCapability is UnsupportedCapability (501): raised instead
  of inventing SDK behavior; docs/SOLARI_SDK_SURFACE.md §K lists the
  capabilities that are explicitly unsupported.
"""

from app.errors import ExecutionTimeout, SolariNotConfigured, SolariUnavailable, UnsupportedCapability


class SolariError(Exception):
    """Base class for errors raised inside the Solari adapter.

    Deliberately NOT a domain error: the runner converts everything it can
    into domain errors or result statuses; anything still carrying this base
    is an adapter-internal bug.
    """


class AuthError(SolariError):
    """401 from Solari — the server's own key is broken."""


class GatewayError(SolariError):
    """Any non-2xx gateway response not otherwise specialized (has .status)."""


class NoCapacityError(SolariError):
    """503 — no host available."""


class ConcurrencyLimitError(SolariError):
    """429 — too many live sandboxes on the Solari account."""


class ActionError(SolariError):
    """A control-channel RPC returned {ok: false}."""


# Domain-error subclasses: safe to propagate to the API layer as-is.
class SolariNotConfigured(SolariNotConfigured):
    """SOLARI_API_KEY is not set on the server."""


class SolariUnavailable(SolariUnavailable):
    """Solari infrastructure failed (auth rejected, capacity, gateway, transport)."""


class SolariTimeout(ExecutionTimeout):
    """A Solari operation exceeded its deadline."""


class UnsupportedSolariCapability(UnsupportedCapability):
    """The requested capability is not supported by the verified SDK surface."""
