"""Invite token handling — the parts that need no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agent.auth import invites
from agent.auth.invites import InviteState
from agent.db.models import Invite


def make(**overrides: object) -> Invite:
    defaults: dict[str, object] = {
        "expires_at": datetime.now(UTC) + timedelta(days=7),
        "accepted_at": None,
    }
    return Invite(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_tokens_are_long_and_never_repeat() -> None:
    tokens = {invites.new_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(t) >= 40 for t in tokens)


def test_only_the_hash_is_ever_stored() -> None:
    token = invites.new_token()
    digest = invites.hash_token(token)
    assert token not in digest
    assert len(digest) == 64
    assert invites.tokens_match(token, digest)
    assert not invites.tokens_match(invites.new_token(), digest)


def test_expiry_is_seven_days() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert invites.expires_at(now=now) - now == timedelta(days=7)


def test_state_is_valid_while_open_and_unexpired() -> None:
    assert invites.state_of(make()) is InviteState.VALID


def test_state_distinguishes_the_three_failure_modes() -> None:
    """Each one is a different screen, so each one needs a different answer."""
    assert invites.state_of(None) is InviteState.INVALID
    assert invites.state_of(make(accepted_at=datetime.now(UTC))) is InviteState.ACCEPTED
    assert (
        invites.state_of(make(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        is InviteState.EXPIRED
    )


def test_an_accepted_invite_reads_as_accepted_even_after_it_expires() -> None:
    spent = make(
        accepted_at=datetime.now(UTC) - timedelta(days=30),
        expires_at=datetime.now(UTC) - timedelta(days=20),
    )
    assert invites.state_of(spent) is InviteState.ACCEPTED


# ---------------------------------------------------------------------------
# The placeholder name an invite starts with
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("jo.patel@example.com", "Jo Patel"),
        ("alex_moreno@example.com", "Alex Moreno"),
        ("mary-jane.watson@example.com", "Mary Jane Watson"),
        ("cfo@example.com", "Cfo"),
        # Already capitalised, and the middle of a word is left alone: "McRae"
        # must not come back as "Mcrae".
        ("Ada.McRae@example.com", "Ada McRae"),
        # Nothing usable in the local part — the address itself is better than
        # an empty name, which would render as a blank row.
        ("...@example.com", "...@example.com"),
    ],
)
def test_a_placeholder_name_is_readable(email: str, expected: str) -> None:
    """An admin should not have to invent a colleague's name to add them.

    Whatever this produces is replaced the moment they accept; until then it
    is what the member list shows, and a list of raw addresses is unreadable.
    """
    from agent.api.routes_users import default_name

    assert default_name(email) == expected
