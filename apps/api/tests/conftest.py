"""Test environment. No database or Redis is required for the P0 suite."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator

import pytest

TEST_KEY = base64.b64encode(b"unit-test-key-32-bytes-exactly!!").decode()


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test a known environment, independent of the developer's shell."""
    for name in list(os.environ):
        if name.startswith(
            ("APP_", "DATABASE_", "REDIS_", "STORAGE_", "SMTP_", "BOOTSTRAP_", "CORS_")
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    from agent.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
