"""Argon2id password hashing and the password policy (PRD §6.1.2).

Parameters are pinned here rather than in config: they are a security decision,
not an environment difference, and `needs_rehash` is what lets them be raised
later without a migration — the next successful login re-hashes transparently.
"""

from __future__ import annotations

import re
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from agent.auth.common_passwords import COMMON_PASSWORDS

MIN_LENGTH = 12
MIN_DISTINCT_CHARACTERS = 4

TIME_COST = 3
MEMORY_COST = 65536  # KiB — 64 MiB per hash
PARALLELISM = 4

_TRAILING_FILLER = re.compile(r"[\d!@#$%^&*._\-]+$")


class PasswordPolicyError(ValueError):
    """A password was rejected before it was ever hashed.

    The message is written to be shown to the person choosing the password.
    """


@lru_cache(maxsize=1)
def _hasher() -> PasswordHasher:
    return PasswordHasher(
        time_cost=TIME_COST,
        memory_cost=MEMORY_COST,
        parallelism=PARALLELISM,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


@lru_cache(maxsize=1)
def _common_passwords() -> tuple[frozenset[str], frozenset[str]]:
    """The literal list, and the same entries with trailing digits/punctuation removed.

    The second set is what turns a 133-entry list into a rule: `password1234` is
    listed, so `password!!` and `password99999` are rejected too.
    """
    literal = {entry.strip().casefold() for entry in COMMON_PASSWORDS}
    bases = {stripped for entry in literal if len(stripped := _TRAILING_FILLER.sub("", entry)) >= 5}
    return frozenset(literal), frozenset(bases)


def validate(password: str) -> None:
    """Raise `PasswordPolicyError` if `password` may not be used. Otherwise return."""
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"Use at least {MIN_LENGTH} characters.")

    normalised = password.strip().casefold()
    literal, bases = _common_passwords()

    if normalised in literal:
        raise PasswordPolicyError("This password is too common. Choose something less guessable.")

    base = _TRAILING_FILLER.sub("", normalised)
    if base and base in bases:
        raise PasswordPolicyError(
            "This is a variation on a common password. Choose something less guessable."
        )

    if len(set(normalised)) < MIN_DISTINCT_CHARACTERS:
        raise PasswordPolicyError(f"Use at least {MIN_DISTINCT_CHARACTERS} different characters.")


def hash_password(password: str) -> str:
    """Validate then hash. The plaintext never leaves this call."""
    validate(password)
    return _hasher().hash(password)


def verify(password_hash: str, password: str) -> bool:
    """Constant-time-ish verification. Any failure is `False`, never an exception."""
    try:
        return _hasher().verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, HashingError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when `password_hash` was made with weaker parameters than the current ones."""
    try:
        return _hasher().check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """A real Argon2id hash of a value nothing can equal.

    Login verifies against this when the email is unknown, so an attacker cannot
    tell a missing account from a wrong password by timing the response
    (PRD §6.1.5). Computed once, because computing it per request would itself
    be a timing signal.
    """
    return _hasher().hash("\x00unknown-account-constant-time-placeholder\x00")
