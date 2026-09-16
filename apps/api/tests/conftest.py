"""Test environment. No database or Redis is required for the unit suite."""

from __future__ import annotations

import base64
import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest

TEST_KEY = base64.b64encode(b"unit-test-key-32-bytes-exactly!!").decode()

FIXTURES = Path(__file__).parent / "fixtures"
CASSETTES = Path(__file__).parent / "cassettes"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test a known environment, independent of the developer's shell."""
    for name in list(os.environ):
        if name.startswith(
            (
                "APP_",
                "DATABASE_",
                "REDIS_",
                "STORAGE_",
                "SMTP_",
                "BOOTSTRAP_",
                "CORS_",
                "EMBEDDING_",
                "CONNECTOR_",
            )
        ):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    # Never load an ONNX model in the unit suite. `hash` is deterministic and
    # needs no download, so CI stays offline and fast; the fastembed path is
    # covered by `test_embedding.py::test_fastembed_*`, which skips when the
    # model is not present.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hash")

    from agent.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fixture_text() -> Callable[[str], str]:
    """Read a file from `tests/fixtures/` by name."""

    def read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return read


@pytest.fixture(scope="session")
def cassette() -> Callable[[str], AbstractContextManager[None]]:
    """Replay one named cassette from `tests/cassettes/`.

    Deliberately explicit rather than `@pytest.mark.vcr`, which derives the
    cassette name from the test name — so renaming a test would silently orphan
    its recording, and several tests could not share one cassette.

    `record_mode="none"` is the load-bearing setting: a request the cassette
    does not hold raises instead of reaching the network, so a developer with
    live credentials in their shell gets exactly the result CI gets.
    """
    import vcr

    recorder = vcr.VCR(
        record_mode="none",
        match_on=["method", "scheme", "host", "path"],
        filter_headers=["authorization", "developer-token", "login-customer-id", "cookie"],
        filter_post_data_parameters=["client_secret", "refresh_token", "client_id"],
        cassette_library_dir=str(CASSETTES),
    )

    @contextmanager
    def use(name: str) -> Iterator[None]:
        with recorder.use_cassette(name):
            yield

    return use
