"""structlog configuration.

JSON in production, human-readable locally, and a redaction processor in front
of both. Redaction is keyed on the field name rather than on the value, so a
field only leaks if it is given a name that says nothing about what it holds —
which is why the rest of the codebase never logs a bare `value=`.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from agent.config import Settings

REDACTED = "[redacted]"

#: A field is redacted when its name contains any of these. Substring matching
#: is what makes it hold as the codebase grows: `csrf_token`, `smtp_password`,
#: `authorization_header` and `secret_ref` are all caught without being listed.
SENSITIVE_FIELD_PARTS = (
    "password",
    "secret",
    "token",
    "authorization",
    "ciphertext",
    "cookie",
    "api_key",
    "apikey",
)

#: How deep to walk nested structures before giving up and redacting wholesale.
#: Bounded so a cyclic or pathological payload cannot spin the logger.
MAX_REDACTION_DEPTH = 6


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_FIELD_PARTS)


def _redact(value: Any, depth: int = 0) -> Any:
    if depth >= MAX_REDACTION_DEPTH:
        return REDACTED
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_sensitive(str(key)) else _redact(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item, depth + 1) for item in value)
    return value


def redact_sensitive(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Drop the value of any field whose name marks it as a secret."""
    return {
        key: REDACTED if _is_sensitive(str(key)) else _redact(value)
        for key, value in event_dict.items()
    }


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer()
        if settings.app_env == "local"
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            redact_sensitive,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
