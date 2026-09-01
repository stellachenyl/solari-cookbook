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

    @model_validator(mode="after")
    def _check_admin_token(self) -> "Settings":
        # Fail fast on a missing production admin token; DEBUG builds may omit it.
        if not self.debug and not self.admin_token.strip():
            raise ValueError(
                "ADMIN_TOKEN must not be empty in production "
                "(or set DEBUG=true for local development)."
            )
        return self

    def solari_configured(self) -> bool:
        return bool(self.solari_api_key.strip())

    def database_backend(self) -> str:
        # "sqlite:///./exekit.db" -> "sqlite"
        return self.database_url.split(":", 1)[0].lstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
