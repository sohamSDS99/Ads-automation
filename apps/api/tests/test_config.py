"""Configuration fails fast and legibly, and normalises what Railway hands it."""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from agent.config import ConfigError, Settings, get_settings


def test_missing_encryption_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="APP_ENCRYPTION_KEY is not set"):
        Settings(app_encryption_key="")


def test_non_base64_encryption_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not valid base64"):
        Settings(app_encryption_key="not base64 at all!!")


def test_wrong_length_encryption_key_is_rejected() -> None:
    short = base64.b64encode(b"only-16-bytes-ok").decode()
    with pytest.raises(ValidationError, match="exactly 32 bytes"):
        Settings(app_encryption_key=short)


def test_get_settings_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "too-short")
    get_settings.cache_clear()
    with pytest.raises(ConfigError, match="APP_ENCRYPTION_KEY"):
        get_settings()


def test_encryption_key_decodes_to_32_bytes() -> None:
    assert len(get_settings().encryption_key) == 32


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql+asyncpg://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
    ],
)
def test_database_url_is_normalised_to_asyncpg(given: str, expected: str) -> None:
    settings = Settings(database_url=given)
    assert settings.async_database_url == expected


def test_cors_origins_accepts_a_comma_list() -> None:
    settings = Settings(cors_origins="http://a.test, http://b.test")
    assert settings.cors_origins == ["http://a.test", "http://b.test"]


def test_cors_origins_parses_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env path, not the kwargs path — this is how the containers are configured."""
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000")
    assert Settings().cors_origins == ["http://localhost:3000"]

    monkeypatch.setenv("CORS_ORIGINS", "http://a.test,http://b.test")
    assert Settings().cors_origins == ["http://a.test", "http://b.test"]


def test_cors_origins_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Settings().cors_origins == ["http://localhost:3000"]


def test_storage_defaults_match_the_railway_volume() -> None:
    settings = Settings()
    assert settings.storage_backend == "local"
    assert settings.storage_dir == "/data"


def test_smtp_is_optional() -> None:
    assert Settings().smtp_configured is False
    assert Settings(smtp_host="smtp.test", smtp_from="a@b.test").smtp_configured is True
