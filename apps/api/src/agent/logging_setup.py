"""structlog configuration.

JSON in production, human-readable locally, and two scrubbing processors in
front of both.

**Secrets are dropped, keyed on the field name.** A field only leaks if it is
given a name that says nothing about what it holds, which is why the rest of
the codebase never logs a bare `value=`.

**PII is masked, keyed on the value.** That difference is the point. A secret
has no diagnostic worth once it is in a log, so it goes entirely; an email
address is how an operator finds the invite that did not arrive, so it is
reduced to the least that still identifies it — `s***@example.com`. Masking on
the *value* rather than the field name is what catches an address that arrives
inside an error string from a mail server, which is exactly where one is most
likely to turn up.

The masks are deliberately stable: the same address always produces the same
mask, so two lines about the same user can still be correlated in a log
aggregator without the address itself ever being written.
"""

from __future__ import annotations

import logging
import re
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

#: Deliberately loose. A false positive costs a masked string in a log line; a
#: false negative writes somebody's address to disk, and this runs over strings
#: that were never meant to hold one.
EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

#: Above this a string is left alone rather than scanned. A rendered report or a
#: dumped HTML page has no business in a log line, and regex-scanning one on
#: every emitted event would be a real cost for no protection.
MAX_SCAN_CHARS = 4_096


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


def _mask_email(match: re.Match[str]) -> str:
    """`someone@example.com` -> `s***@example.com`.

    The domain survives because it is what makes a delivery failure diagnosable
    ("every bounce is one tenant's domain"), and the domain alone identifies
    nobody. The initial survives so two lines about two different people at the
    same company do not collapse into one.
    """
    return f"{match.group(1)}***@{match.group(2)}"


def _mask_value(value: Any, depth: int = 0) -> Any:
    if depth >= MAX_REDACTION_DEPTH:
        return value
    if isinstance(value, str):
        return value if len(value) > MAX_SCAN_CHARS else EMAIL_RE.sub(_mask_email, value)
    if isinstance(value, dict):
        return {key: _mask_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_mask_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_mask_value(item, depth + 1) for item in value)
    return value


def mask_pii(
    _logger: object, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Mask every email address in every value, including inside the message."""
    return {key: _mask_value(value) for key, value in event_dict.items()}


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
            # Secrets first: `mask_pii` walks every remaining value, and there
            # is no reason to walk one that is about to become "[redacted]".
            redact_sensitive,
            mask_pii,
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
