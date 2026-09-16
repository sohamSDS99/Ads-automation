"""Password policy and hashing."""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher
from argon2.low_level import Type

from agent.auth import passwords

GOOD = "quarry-lantern-98-fog"


def test_argon2id_parameters_are_the_ones_the_prd_pins() -> None:
    assert passwords.TIME_COST == 3
    assert passwords.MEMORY_COST == 65536
    assert passwords.PARALLELISM == 4
    encoded = passwords.hash_password(GOOD)
    assert encoded.startswith("$argon2id$")
    assert "m=65536,t=3,p=4" in encoded


def test_hash_then_verify_round_trips() -> None:
    encoded = passwords.hash_password(GOOD)
    assert passwords.verify(encoded, GOOD)
    assert not passwords.verify(encoded, GOOD + "!")


def test_the_same_password_hashes_differently_every_time() -> None:
    assert passwords.hash_password(GOOD) != passwords.hash_password(GOOD)


@pytest.mark.parametrize(
    "candidate",
    [
        "short",
        "elevenchars",  # 11 — one under the floor
    ],
)
def test_rejects_passwords_under_twelve_characters(candidate: str) -> None:
    with pytest.raises(passwords.PasswordPolicyError, match="at least 12"):
        passwords.validate(candidate)


def test_accepts_exactly_twelve_characters() -> None:
    passwords.validate("abcdefghijkm")


@pytest.mark.parametrize(
    "candidate",
    ["password1234", "PASSWORD1234", "  password1234  ", "administrator", "qwertyuiop123"],
)
def test_rejects_the_common_list_case_and_whitespace_insensitively(candidate: str) -> None:
    with pytest.raises(passwords.PasswordPolicyError, match="common"):
        passwords.validate(candidate)


@pytest.mark.parametrize("candidate", ["password!!!!", "letmein12345678", "iloveyou12349"])
def test_rejects_a_listed_password_with_filler_bolted_on(candidate: str) -> None:
    with pytest.raises(passwords.PasswordPolicyError, match="common"):
        passwords.validate(candidate)


def test_rejects_too_few_distinct_characters() -> None:
    with pytest.raises(passwords.PasswordPolicyError, match="different characters"):
        passwords.validate("ababababababab")


def test_every_listed_entry_would_be_rejected_on_its_own_merits() -> None:
    """The file must only hold entries the length rule does not already catch."""
    literal, _ = passwords._common_passwords()
    assert literal, "the common-password list is empty"
    assert all(len(entry) >= passwords.MIN_LENGTH for entry in literal)


def test_needs_rehash_is_true_for_weaker_parameters() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID).hash(GOOD)
    assert passwords.needs_rehash(weak)
    assert not passwords.needs_rehash(passwords.hash_password(GOOD))


def test_verify_never_raises_on_a_corrupt_hash() -> None:
    assert not passwords.verify("not-a-hash", GOOD)
    assert not passwords.verify("", GOOD)


def test_dummy_hash_is_real_argon2_and_matches_nothing() -> None:
    """Login verifies against this when the email is unknown, so it must cost the same."""
    dummy = passwords.dummy_hash()
    assert dummy.startswith("$argon2id$")
    assert "m=65536,t=3,p=4" in dummy
    assert not passwords.verify(dummy, GOOD)
    assert passwords.dummy_hash() is dummy, "must be computed once, or its cost is a timing signal"
