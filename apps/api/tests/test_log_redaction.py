"""The structlog redaction processor. Acceptance: a known secret never reaches a log line."""

from __future__ import annotations

import pytest

from agent.logging_setup import REDACTED, redact_sensitive

SECRET = "hunter2-hunter2-hunter2"


def process(**event: object) -> dict[str, object]:
    return dict(redact_sensitive(None, "info", dict(event)))


@pytest.mark.parametrize(
    "field",
    [
        "password",
        "new_password",
        "current_password",
        "smtp_password",
        "secret",
        "file_token_secret",
        "token",
        "csrf_token",
        "token_hash",
        "authorization",
        "ciphertext",
        "cookie",
        "set_cookie",
        "api_key",
        "openrouter_apikey",
        "PASSWORD",
        "Authorization",
    ],
)
def test_sensitive_field_names_are_redacted(field: str) -> None:
    assert process(**{field: SECRET}) == {field: REDACTED}


def test_ordinary_fields_survive() -> None:
    out = process(event="auth.login", user_id="abc", role="admin", count=3)
    assert out == {"event": "auth.login", "user_id": "abc", "role": "admin", "count": 3}


def test_redaction_reaches_into_nested_structures() -> None:
    out = process(meta={"email": "a@b.co", "password": SECRET, "nested": [{"token": SECRET}]})
    assert out["meta"] == {
        "email": "a@b.co",
        "password": REDACTED,
        "nested": [{"token": REDACTED}],
    }


def test_an_action_name_that_mentions_a_password_is_not_a_password() -> None:
    """Redaction keys on the field name, so values stay legible."""
    assert process(action="user.password_changed") == {"action": "user.password_changed"}


def test_deeply_nested_payloads_are_bounded_not_walked_forever() -> None:
    payload: dict[str, object] = {"k": "v"}
    for _ in range(20):
        payload = {"k": payload}
    out = process(meta=payload)
    assert REDACTED in repr(out)


def test_the_processor_is_installed_before_any_renderer() -> None:
    """A processor after the renderer would redact nothing."""
    import structlog

    from agent.config import get_settings
    from agent.logging_setup import configure_logging

    configure_logging(get_settings())
    processors = structlog.get_config()["processors"]
    names = [getattr(p, "__name__", type(p).__name__) for p in processors]
    assert "redact_sensitive" in names
    assert names.index("redact_sensitive") < len(names) - 1, "redaction must precede the renderer"
