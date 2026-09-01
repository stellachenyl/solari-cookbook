"""Exceptions raised by the Solari adapter (app/services/solari_runner.py).

Contract:
- Session/file operations raise these directly.
- run_python() reports execution-level outcomes as SolariExecutionResult
  statuses ("solari_unconfigured", "solari_unavailable", "timeout", ...) and
  only raises for caller errors (e.g. KeyError on an unknown session_id);
  the route layer maps those to HTTP responses.
"""


class SolariError(Exception):
    """Base class for all Solari adapter errors."""


class SolariNotConfigured(SolariError):
    """SOLARI_API_KEY is not set on the server."""


class SolariUnavailable(SolariError):
    """Solari infrastructure failed (auth rejected, capacity, gateway, transport)."""


class SolariTimeout(SolariError):
    """A Solari operation exceeded its deadline."""


class UnsupportedSolariCapability(SolariError):
    """The requested capability is not supported by the verified SDK surface.

    Raised instead of inventing SDK behavior; docs/SOLARI_SDK_SURFACE.md §K
    lists the capabilities that are explicitly unsupported.
    """
