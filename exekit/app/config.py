"""ExecKit application settings, loaded from environment / .env via pydantic-settings."""

from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # May be empty — the app must still boot without it (health reports it).
    solari_api_key: str = ""

    app_base_url: str = "http://localhost:8000"
    debug: bool = False
    admin_token: str = "change-me"

    # Only sqlite is used in v1; the URL is kept configurable for the future
    # Postgres migration (docs/EXEKIT_SPEC.md §I).
    database_url: str = "sqlite:///./exekit.db"

    execution_timeout_seconds: int = 15
    free_credits: int = 25
    max_output_chars: int = 100_000

    # Rate limits (per API key, per minute; <= 0 disables a bucket).
    rate_limit_executions_per_minute: int = 30
    rate_limit_sessions_per_minute: int = 10
    rate_limit_requests_per_minute: int = 100

    # Idle-session cleanup.
    session_idle_timeout_minutes: int = 30
    session_cleanup_interval_seconds: int = 300

    # Stripe credit purchases. BILLING_ENABLED is not an env var: it is
    # derived automatically (see the property below) — true iff a secret key
    # is configured.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_credit_price_usd: int = 19
    stripe_credit_amount: int = 1000

    @model_validator(mode="after")
    def _check_admin_token(self) -> "Settings":
        # Fail fast on a missing production admin token; DEBUG builds may omit it.
        if not self.debug and not self.admin_token.strip():
            raise ValueError(
                "ADMIN_TOKEN must not be empty in production "
                "(or set DEBUG=true for local development)."
            )
        return self

    @property
    def billing_enabled(self) -> bool:
        """True iff Stripe is configured (a secret key exists). Not an env
        var on purpose: billing can never be toggled on without credentials."""
        return bool(self.stripe_secret_key.strip())

    def solari_configured(self) -> bool:
        return bool(self.solari_api_key.strip())

    def database_backend(self) -> str:
        # "sqlite:///./exekit.db" -> "sqlite"
        return self.database_url.split(":", 1)[0].lstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
