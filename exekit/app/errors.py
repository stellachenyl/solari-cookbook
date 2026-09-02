"""ExecKit domain exceptions.

Every exception carries a stable `code`, a human-readable `message`, and the
`http_status` the API layer should answer with. main.py renders any
ExecKitError through the standard error envelope:

    {"error": {"code": ..., "message": ..., "details": {...}}}

Infrastructure errors raised by SolariRunner (app/services/errors.py) are
subclasses of the matching domain errors, so they flow through the same
handler without translation.
"""


class ExecKitError(Exception):
    """Base class for all ExecKit domain errors."""

    default_code = "exec_kit_error"
    default_http_status = 500

    def __init__(self, message: str, *, details: dict | None = None,
                 code: str | None = None, http_status: int | None = None):
        self.message = message
        self.details = details or {}
        self.code = code or self.default_code
        self.http_status = http_status or self.default_http_status
        super().__init__(message)


# --- request / auth -----------------------------------------------------------


class InvalidRequest(ExecKitError):
    default_code = "invalid_request"
    default_http_status = 400


class Unauthorized(ExecKitError):
    default_code = "unauthorized"
    default_http_status = 401


class ApiKeyInactive(ExecKitError):
    default_code = "api_key_inactive"
    default_http_status = 403


class InvalidAdminToken(ExecKitError):
    default_code = "invalid_admin_token"
    default_http_status = 401


class NotFound(ExecKitError):
    default_code = "not_found"
    default_http_status = 404


# --- billing -------------------------------------------------------------------


class InsufficientCredits(ExecKitError):
    default_code = "insufficient_credits"
    default_http_status = 402
    default_message = "You need more credits to run executions."

    def __init__(self, message: str | None = None, *, details: dict | None = None,
                 code: str | None = None, http_status: int | None = None):
        super().__init__(message or self.default_message, details=details,
                         code=code, http_status=http_status)


# --- sessions --------------------------------------------------------------------


class InvalidSession(ExecKitError):
    default_code = "invalid_session"
    default_http_status = 404


class SessionNotFound(ExecKitError):
    default_code = "session_not_found"
    default_http_status = 404


class SessionKilled(ExecKitError):
    default_code = "session_gone"
    default_http_status = 410


# --- rate limiting -----------------------------------------------------------------


class RateLimitExceeded(ExecKitError):
    default_code = "rate_limit_exceeded"
    default_http_status = 429


# --- billing -------------------------------------------------------------------


class BillingDisabled(ExecKitError):
    default_code = "billing_disabled"
    default_http_status = 503
    default_message = "Stripe is not configured."

    def __init__(self, message: str | None = None, *, details: dict | None = None,
                 code: str | None = None, http_status: int | None = None):
        super().__init__(message or self.default_message, details=details,
                         code=code, http_status=http_status)


# --- Solari infrastructure ------------------------------------------------------------


class SolariNotConfigured(ExecKitError):
    default_code = "solari_unconfigured"
    default_http_status = 503


class SolariUnavailable(ExecKitError):
    default_code = "solari_unavailable"
    default_http_status = 502


class ExecutionTimeout(ExecKitError):
    default_code = "execution_timeout"
    default_http_status = 504


class UnsupportedCapability(ExecKitError):
    default_code = "unsupported_capability"
    default_http_status = 501
