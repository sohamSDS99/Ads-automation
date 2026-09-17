"""PII masking in the log pipeline, and the request context bound in front of it.

Secrets are *dropped* (`test_log_redaction.py`); addresses are *masked*. The
difference is deliberate and is what this file pins: a secret has no diagnostic
worth once it is in a log, but an email address is how an operator finds the
invite that never arrived, so it is reduced to the least that still identifies
it rather than removed.
"""

from __future__ import annotations

import pytest
import structlog

from agent.api.logging_middleware import bind_actor, bind_actor_role
from agent.logging_setup import REDACTED, mask_pii, redact_sensitive


def mask(**event: object) -> dict[str, object]:
    return dict(mask_pii(None, "info", dict(event)))


def pipeline(**event: object) -> dict[str, object]:
    """Both processors, in the order `configure_logging` installs them."""
    return dict(mask_pii(None, "info", redact_sensitive(None, "info", dict(event))))


# ---------------------------------------------------------------------------
# masking
# ---------------------------------------------------------------------------


def test_an_address_keeps_its_domain_and_loses_its_local_part() -> None:
    """The domain is what makes a delivery failure diagnosable and identifies nobody."""
    assert mask(to="someone@example.com") == {"to": "s***@example.com"}


def test_two_people_at_one_company_do_not_collapse_into_one_mask() -> None:
    assert mask(a="alice@corp.com")["a"] != mask(b="bob@corp.com")["b"]


def test_the_same_address_always_masks_the_same_way() -> None:
    """Two lines about one user stay correlatable without the address being written."""
    assert mask(to="someone@example.com") == mask(to="someone@example.com")


def test_an_address_inside_a_sentence_is_masked() -> None:
    """Where one is most likely to turn up: a mail server's own error string."""
    out = mask(error="550 5.1.1 <recipient@corp.example.org> user unknown")
    assert "recipient@corp.example.org" not in str(out["error"])
    assert "r***@corp.example.org" in str(out["error"])


def test_addresses_nested_in_structures_are_masked() -> None:
    out = mask(meta={"recipients": ["a@x.com", "b@y.org"], "count": 2})
    assert out["meta"] == {"recipients": ["a***@x.com", "b***@y.org"], "count": 2}


def test_a_plus_addressed_mailbox_is_masked() -> None:
    assert mask(to="soham+ads@sdsmanager.com") == {"to": "s***@sdsmanager.com"}


@pytest.mark.parametrize(
    "value",
    ["not an address", "a@b", "@example.com", "1.5@", "two words"],
)
def test_text_that_is_not_an_address_is_left_alone(value: str) -> None:
    assert mask(note=value) == {"note": value}


def test_a_very_long_string_is_not_scanned() -> None:
    """A rendered report has no business in a log line, and scanning one costs for nothing."""
    payload = "x" * 10_000 + " someone@example.com"
    assert mask(body=payload)["body"] == payload


def test_a_secret_is_dropped_before_it_is_ever_scanned_for_an_address() -> None:
    out = pipeline(password="hunter2@example.com", to="real@example.com")
    assert out["password"] == REDACTED
    assert out["to"] == "r***@example.com"


def test_non_string_values_pass_through_unharmed() -> None:
    assert mask(count=3, ok=True, ratio=0.5, nothing=None) == {
        "count": 3,
        "ok": True,
        "ratio": 0.5,
        "nothing": None,
    }


def test_both_processors_are_installed_and_ordered() -> None:
    from agent.config import Settings
    from agent.logging_setup import configure_logging

    # Built here rather than read from the environment: this asserts on the
    # processor chain, and it should hold whether or not the shell that ran it
    # happens to have APP_ENCRYPTION_KEY set.
    configure_logging(Settings(app_encryption_key="d29yZHdvcmR3b3Jkd29yZHdvcmR3b3Jkd29yZHdvcmQ="))
    names = [
        getattr(processor, "__name__", type(processor).__name__)
        for processor in structlog.get_config()["processors"]
    ]
    assert names.index("redact_sensitive") < names.index("mask_pii"), (
        "secrets must be dropped before the PII walk, or the walk spends time on them"
    )
    assert names.index("mask_pii") < len(names) - 1, "masking must precede the renderer"


# ---------------------------------------------------------------------------
# request context
# ---------------------------------------------------------------------------


def test_the_actor_is_bound_into_the_log_context() -> None:
    import uuid

    structlog.contextvars.clear_contextvars()
    identifier = uuid.uuid4()
    bind_actor(identifier)
    bind_actor_role("operator")
    bound = structlog.contextvars.get_contextvars()
    assert bound["actor_id"] == str(identifier)
    assert bound["actor_role"] == "operator"
    structlog.contextvars.clear_contextvars()


def test_the_health_path_is_quiet() -> None:
    """Polled every few seconds by the platform; it would be most of the log by volume."""
    from agent.api.logging_middleware import QUIET_PATHS

    assert "/api/v1/health" in QUIET_PATHS
