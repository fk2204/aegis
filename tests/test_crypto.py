"""Tests for ``aegis.crypto`` — AES-256-GCM PDF encryption module.

Chunk A of the PDF retention redesign — locks down the crypto
roundtrip + integrity contract. Worker callers (chunk B) and view
route callers (chunk C) rely on:

* roundtrip lossless: ``decrypt(encrypt(plaintext)) == plaintext``
* nonce randomness: same plaintext → distinct ciphertexts
* integrity rejection: tampered byte → ``CorruptCiphertextError``
* short blob rejection: ``< 28 bytes`` rejected immediately
* key-version mismatch: ``CorruptCiphertextError`` not silent garbage
* boot guard: misconfigured key → ``CryptoConfigError`` at first call
"""
from __future__ import annotations

import base64
import os

import pytest
from pydantic import SecretStr

from aegis.crypto import (
    CorruptCiphertextError,
    CryptoConfigError,
    current_key_version,
    decrypt_pdf,
    encrypt_pdf,
    validate_crypto_config_at_boot,
)


def _make_key() -> str:
    """Base64-encoded 32-byte random key — the format the env var
    is expected to carry."""
    return base64.b64encode(os.urandom(32)).decode("ascii")


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a deterministic crypto settings env for every test.

    Two keys configured (V1 + V2); V1 is current. Tests that need a
    different setup re-patch get_settings inside their body.
    """
    key_v1 = _make_key()
    key_v2 = _make_key()

    class _S:
        pdf_encryption_keys_current = 1
        pdf_encryption_key_v1 = SecretStr(key_v1)
        pdf_encryption_key_v2 = SecretStr(key_v2)
        # other versions intentionally missing — getattr returns None

    monkeypatch.setattr("aegis.crypto.get_settings", lambda: _S())


# ---------------------------------------------------------------------------
# Roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 16, 1024, 25 * 1024 * 1024])
def test_roundtrip_for_various_sizes(size: int) -> None:
    """Smallest payload (1 byte) up to the configured max upload size
    (25 MB). Locks down that AES-GCM handles real-world PDF byte
    ranges without truncation."""
    plaintext = os.urandom(size)
    sealed = encrypt_pdf(plaintext, key_version=1)
    assert decrypt_pdf(sealed, key_version=1) == plaintext


def test_same_plaintext_different_ciphertext() -> None:
    """Per-call ``os.urandom(12)`` nonce → two encryptions of the same
    plaintext produce distinct ciphertexts. Without this, AES-GCM
    leaks plaintext equality information; with this, an attacker who
    sees both blobs can't tell they hold the same data."""
    plaintext = b"PDF" + b"\x00" * 100
    sealed_a = encrypt_pdf(plaintext, key_version=1)
    sealed_b = encrypt_pdf(plaintext, key_version=1)
    assert sealed_a != sealed_b
    # But both decrypt to the same plaintext
    assert decrypt_pdf(sealed_a, key_version=1) == plaintext
    assert decrypt_pdf(sealed_b, key_version=1) == plaintext


# ---------------------------------------------------------------------------
# Integrity rejection
# ---------------------------------------------------------------------------


def test_tampered_ciphertext_raises() -> None:
    """Flip one byte mid-blob → CorruptCiphertextError (AES-GCM auth
    tag rejected). This is the integrity guarantee the view route
    relies on for the storage-corruption / substitute-blob threat."""
    plaintext = b"hello world"
    sealed = bytearray(encrypt_pdf(plaintext, key_version=1))
    # Flip a byte well past the nonce so we're tampering with
    # ciphertext or tag, not the nonce
    sealed[20] ^= 0x01
    with pytest.raises(CorruptCiphertextError):
        decrypt_pdf(bytes(sealed), key_version=1)


def test_tampered_nonce_raises() -> None:
    """Flipping a byte in the nonce region also fails decryption — the
    auth tag is computed over (nonce, ciphertext) so a nonce change
    invalidates the tag."""
    plaintext = b"hello world"
    sealed = bytearray(encrypt_pdf(plaintext, key_version=1))
    sealed[0] ^= 0x01
    with pytest.raises(CorruptCiphertextError):
        decrypt_pdf(bytes(sealed), key_version=1)


def test_blob_shorter_than_28_bytes_raises() -> None:
    """Anything below nonce(12)+tag(16) cannot be a valid AES-GCM
    output. The early-reject guard surfaces this with a clearer error
    than the underlying library would emit on a truncated read."""
    with pytest.raises(CorruptCiphertextError) as exc:
        decrypt_pdf(b"x" * 10, key_version=1)
    assert "28" in str(exc.value)


def test_blob_exactly_28_bytes_attempts_decrypt() -> None:
    """28 bytes = nonce(12) + tag(16) + ZERO bytes of ciphertext. The
    early-reject guard MUST NOT fire (the blob is structurally valid),
    but AES-GCM will reject the (random) tag — different error path."""
    with pytest.raises(CorruptCiphertextError) as exc:
        decrypt_pdf(b"\x00" * 28, key_version=1)
    # Not the early-reject message — must have been an AES-GCM rejection
    assert "shorter than nonce" not in str(exc.value)


# ---------------------------------------------------------------------------
# Key version handling
# ---------------------------------------------------------------------------


def test_decrypt_with_wrong_key_version_raises() -> None:
    """A blob sealed with V1 cannot be decrypted with V2. Maps to the
    "wrong key" / "corrupted blob" outcome — same error type as
    tampering so the view route's catch is uniform."""
    sealed_v1 = encrypt_pdf(b"hello", key_version=1)
    with pytest.raises(CorruptCiphertextError):
        decrypt_pdf(sealed_v1, key_version=2)


def test_current_key_version_reads_settings() -> None:
    assert current_key_version() == 1


def test_current_key_version_raises_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker call to ``current_key_version()`` before any key is
    configured must fail loud rather than silently return None and skip
    the storage step. Boot guard prevents this state in production, but
    the runtime guard belongs here too."""
    class _S:
        pdf_encryption_keys_current = None
        pdf_encryption_key_v1 = None

    monkeypatch.setattr("aegis.crypto.get_settings", lambda: _S())

    with pytest.raises(CryptoConfigError) as exc:
        current_key_version()
    assert "PDF_ENCRYPTION_KEYS_CURRENT" in str(exc.value)


def test_encrypt_with_missing_key_version_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller asking for an unconfigured key version → CryptoConfigError
    at lookup time. Distinct from CorruptCiphertextError so the worker
    can audit it as an environment problem, not an integrity failure."""
    key_v1 = _make_key()

    class _S:
        pdf_encryption_keys_current = 1
        pdf_encryption_key_v1 = SecretStr(key_v1)
        # v3 intentionally missing

    monkeypatch.setattr("aegis.crypto.get_settings", lambda: _S())

    with pytest.raises(CryptoConfigError) as exc:
        encrypt_pdf(b"x", key_version=3)
    assert "V3" in str(exc.value)


# ---------------------------------------------------------------------------
# Boot guard
# ---------------------------------------------------------------------------


def test_boot_guard_passes_with_valid_config() -> None:
    """Default fixture has current=1 + V1 set + V1 is 32 bytes. Boot
    guard must succeed."""
    validate_crypto_config_at_boot()  # raises on failure


def test_boot_guard_skips_when_current_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunk A ships without a current-version key set; the worker
    (chunk B) starts using it later. The boot guard must NOT refuse
    startup when current is None — it's a "not yet rotated in" state."""
    class _S:
        pdf_encryption_keys_current = None
        pdf_encryption_key_v1 = None

    monkeypatch.setattr("aegis.crypto.get_settings", lambda: _S())
    validate_crypto_config_at_boot()  # no raise


def test_boot_guard_skips_when_current_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PDF_ENCRYPTION_KEYS_CURRENT=0`` is the env-var equivalent of
    None (pydantic coerces). Same skip semantics."""
    class _S:
        pdf_encryption_keys_current = 0
        pdf_encryption_key_v1 = None

    monkeypatch.setattr("aegis.crypto.get_settings", lambda: _S())
    validate_crypto_config_at_boot()  # no raise


def test_boot_guard_refuses_when_current_points_at_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PDF_ENCRYPTION_KEYS_CURRENT=3`` with no V3 configured → refuses
    to boot. The runtime worker would fail every encrypt call against
    this misconfiguration; the boot guard catches it at startup
    instead."""
    class _S:
        pdf_encryption_keys_current = 3
        pdf_encryption_key_v1 = SecretStr(_make_key())
        # v3 missing

    monkeypatch.setattr("aegis.crypto.get_settings", lambda: _S())

    with pytest.raises(CryptoConfigError) as exc:
        validate_crypto_config_at_boot()
    assert "V3" in str(exc.value)


def test_key_must_decode_to_32_bytes_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key that decodes to 31 or 33 bytes → CryptoConfigError. The
    AES-256-GCM spec requires exactly 256-bit keys; under-/over-length
    keys would either silently extend or truncate downstream."""
    short_key = base64.b64encode(os.urandom(31)).decode("ascii")

    class _S:
        pdf_encryption_keys_current = 1
        pdf_encryption_key_v1 = SecretStr(short_key)

    monkeypatch.setattr("aegis.crypto.get_settings", lambda: _S())

    with pytest.raises(CryptoConfigError) as exc:
        validate_crypto_config_at_boot()
    assert "32 bytes" in str(exc.value)


def test_key_must_be_valid_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-base64 input → CryptoConfigError at boot."""
    class _S:
        pdf_encryption_keys_current = 1
        pdf_encryption_key_v1 = SecretStr("not_base64_!@#")

    monkeypatch.setattr("aegis.crypto.get_settings", lambda: _S())

    with pytest.raises(CryptoConfigError):
        validate_crypto_config_at_boot()
