"""AES-256-GCM secret vault.

Every credential in the `credential` table is stored as (ciphertext, nonce)
produced here. The key comes from `APP_ENCRYPTION_KEY` and lives only in the
environment — never in the database, never in a log line, never in an API
response (PRD §15 NF5).
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent.config import get_settings

NONCE_BYTES = 12  # 96-bit nonce, the size AES-GCM is specified for.


class DecryptionError(Exception):
    """Ciphertext failed authentication — wrong key, or the data was tampered with."""


def _cipher(key: bytes | None = None) -> AESGCM:
    return AESGCM(key if key is not None else get_settings().encryption_key)


def encrypt(
    plaintext: str | bytes, *, aad: bytes | None = None, key: bytes | None = None
) -> tuple[bytes, bytes]:
    """Encrypt `plaintext`, returning `(ciphertext, nonce)`.

    A fresh random nonce is generated per call — never reuse one with the same
    key. `aad` is authenticated but not encrypted; pass stable context (for
    example the credential id) to bind a ciphertext to its row.
    """
    data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = _cipher(key).encrypt(nonce, data, aad)
    return ciphertext, nonce


def decrypt(
    ciphertext: bytes, nonce: bytes, *, aad: bytes | None = None, key: bytes | None = None
) -> bytes:
    """Decrypt and authenticate. Raises `DecryptionError` on any tampering."""
    try:
        return _cipher(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise DecryptionError("ciphertext failed authentication") from exc


def decrypt_str(
    ciphertext: bytes, nonce: bytes, *, aad: bytes | None = None, key: bytes | None = None
) -> str:
    """`decrypt`, decoded as UTF-8."""
    return decrypt(ciphertext, nonce, aad=aad, key=key).decode("utf-8")
