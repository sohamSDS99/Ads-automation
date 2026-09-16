"""Typed application configuration.

Every environment difference between local Compose and Railway is a VALUE in
here. There is no `if RAILWAY` branch anywhere in application code (PRD §15
NF8c) — the code reads the same names in both places.
"""

from __future__ import annotations

import base64
import binascii
import sys
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENCRYPTION_KEY_BYTES = 32


class ConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


class Settings(BaseSettings):
    """Application settings, read from the environment exactly once."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- core ---------------------------------------------------------------
    # Railway injects PORT. Never hardcode it (PRD §5.2 rule 3).
    port: int = 8000
    app_env: Literal["local", "production"] = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql://agent:agent@postgres:5432/agent"
    redis_url: str = "redis://redis:6379/0"

    # 32 bytes, base64-encoded. Never logged, never returned by any endpoint.
    app_encryption_key: SecretStr = SecretStr("")

    # --- sessions (consumed in P0b) ----------------------------------------
    session_cookie_name: str = "ara_session"
    session_ttl_days: int = 30
    cookie_secure: bool = False

    # --- bootstrap admin (consumed in P0b) ---------------------------------
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: SecretStr | None = None

    # --- llm ----------------------------------------------------------------
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # --- storage ------------------------------------------------------------
    storage_backend: Literal["local", "s3"] = "local"
    storage_dir: str = "/data"
    worker_internal_url: str = "http://worker:8081"
    file_token_secret: SecretStr = SecretStr("")

    # --- budget -------------------------------------------------------------
    max_run_cost_usd: Decimal = Decimal("15.00")

    # --- smtp (all optional; without it invites fall back to copyable links)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True

    # --- web ----------------------------------------------------------------
    # Only used by the local Compose mirror. In production the browser is
    # same-origin with `web`, so no CORS entry is needed at all.
    #
    # `NoDecode` matters: without it pydantic-settings tries to JSON-decode any
    # list-typed field coming from the environment, and a plain
    # `CORS_ORIGINS=http://localhost:3000` fails before the validator below ever
    # runs.
    cors_origins: Annotated[
        list[str],
        NoDecode,
        Field(default_factory=lambda: ["http://localhost:3000"]),
    ]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("app_encryption_key")
    @classmethod
    def _validate_encryption_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw:
            raise ValueError(
                "APP_ENCRYPTION_KEY is not set. Generate one with:\n"
                '  python -c "import base64,os;'
                'print(base64.b64encode(os.urandom(32)).decode())"'
            )
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"APP_ENCRYPTION_KEY is not valid base64 ({exc}). It must be "
                f"{ENCRYPTION_KEY_BYTES} random bytes, base64-encoded."
            ) from exc
        if len(decoded) != ENCRYPTION_KEY_BYTES:
            raise ValueError(
                f"APP_ENCRYPTION_KEY must decode to exactly {ENCRYPTION_KEY_BYTES} bytes, "
                f"got {len(decoded)}. Generate one with:\n"
                '  python -c "import base64,os;'
                'print(base64.b64encode(os.urandom(32)).decode())"'
            )
        return value

    @property
    def encryption_key(self) -> bytes:
        """The decoded AES-256 key. Never log this."""
        return base64.b64decode(self.app_encryption_key.get_secret_value(), validate=True)

    @property
    def async_database_url(self) -> str:
        """`DATABASE_URL` normalised to the asyncpg driver.

        Railway hands out `postgresql://…`; SQLAlchemy needs the driver named
        explicitly. Normalising here means one env var works in both places.
        """
        url = self.database_url
        if url.startswith("postgresql+"):
            return url
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        return url

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once, failing fast and legibly if the environment is wrong."""
    try:
        return Settings()
    except ValidationError as exc:
        lines = ["Invalid configuration — the process cannot start:"]
        for error in exc.errors():
            field = ".".join(str(part) for part in error["loc"]) or "(root)"
            lines.append(f"  - {field.upper()}: {error['msg']}")
        message = "\n".join(lines)
        print(message, file=sys.stderr)  # noqa: T201 — startup diagnostics
        raise ConfigError(message) from exc
