"""AES-256-GCM vault: round-trip, tamper detection, key isolation."""

from __future__ import annotations

import pytest

from agent.crypto import NONCE_BYTES, DecryptionError, decrypt, decrypt_str, encrypt

OTHER_KEY = b"another-32-byte-key-for-testing!"
assert len(OTHER_KEY) == 32


def test_round_trip_str() -> None:
    ciphertext, nonce = encrypt("sk-or-v1-secret")
    assert decrypt_str(ciphertext, nonce) == "sk-or-v1-secret"


def test_round_trip_bytes() -> None:
    payload = b"\x00\x01\x02 binary \xff"
    ciphertext, nonce = encrypt(payload)
    assert decrypt(ciphertext, nonce) == payload


def test_ciphertext_is_not_plaintext() -> None:
    ciphertext, _ = encrypt("sk-or-v1-secret")
    assert b"sk-or-v1-secret" not in ciphertext


def test_nonce_is_fresh_per_call() -> None:
    nonces = {encrypt("same input")[1] for _ in range(50)}
    assert len(nonces) == 50
    assert all(len(nonce) == NONCE_BYTES for nonce in nonces)


def test_tampered_ciphertext_is_rejected() -> None:
    ciphertext, nonce = encrypt("sk-or-v1-secret")
    tampered = bytearray(ciphertext)
    tampered[0] ^= 0x01
    with pytest.raises(DecryptionError):
        decrypt(bytes(tampered), nonce)


def test_tampered_nonce_is_rejected() -> None:
    ciphertext, nonce = encrypt("sk-or-v1-secret")
    tampered = bytearray(nonce)
    tampered[-1] ^= 0x01
    with pytest.raises(DecryptionError):
        decrypt(ciphertext, bytes(tampered))


def test_truncated_ciphertext_is_rejected() -> None:
    ciphertext, nonce = encrypt("sk-or-v1-secret")
    with pytest.raises(DecryptionError):
        decrypt(ciphertext[:-1], nonce)


def test_wrong_key_is_rejected() -> None:
    ciphertext, nonce = encrypt("sk-or-v1-secret")
    with pytest.raises(DecryptionError):
        decrypt(ciphertext, nonce, key=OTHER_KEY)


def test_aad_binds_ciphertext_to_context() -> None:
    ciphertext, nonce = encrypt("sk-or-v1-secret", aad=b"credential:1")
    assert decrypt_str(ciphertext, nonce, aad=b"credential:1") == "sk-or-v1-secret"
    with pytest.raises(DecryptionError):
        decrypt(ciphertext, nonce, aad=b"credential:2")
    with pytest.raises(DecryptionError):
        decrypt(ciphertext, nonce)
