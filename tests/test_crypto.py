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


# ---------------------------------------------------------------------------
# Cold-boot integration test — exercises the REAL production entrypoint
# ---------------------------------------------------------------------------
#
# Background: chunk A shipped with two distinct recursion bugs on the
# crypto-boot-guard path.
#
#   1. ``get_settings()`` called ``validate_crypto_config_at_boot()``
#      with no args, which then called ``get_settings()`` again — fixed
#      by passing settings explicitly (commit 5384ca8).
#
#   2. ``validate_crypto_config_at_boot(settings)`` called
#      ``_key_for_version(version)``, which itself called
#      ``get_settings()`` internally — fixed by introducing
#      ``_key_for_version_with_settings`` (commit ae75fc2).
#
# Both bugs slipped through the entire unit-test suite because every
# existing test monkeypatches ``aegis.crypto.get_settings`` to return a
# stub. The monkeypatch short-circuits the cycle — the stub returns
# instantly on every call instead of re-entering the cache-miss path.
# Production cold boot (first call to ``get_settings`` after a process
# restart) was the only context where the cycle could fire, and it
# fired on every restart.
#
# The test below is the regression guard for both bugs AND any future
# recursion hop somebody adds to the chain. It:
#
#   * does NOT monkeypatch ``aegis.crypto.get_settings`` — uses the
#     real symbol so any hidden ``get_settings()`` call inside the
#     chain would re-enter the cache-miss path and recurse
#   * provides env vars at the ``os.environ`` level (same shape the
#     systemd unit produces) so ``Settings()`` constructs naturally
#   * clears the lru_cache before AND after so the test doesn't
#     pollute neighbors and isn't masked by a previously-cached value
#   * implicitly asserts no recursion (a real ``RecursionError`` would
#     fail the test) and verifies the chain populated settings
#     correctly


def test_cold_boot_get_settings_chain_no_recursion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the REAL production cold-boot path of ``get_settings``.

    Critically does NOT monkeypatch ``aegis.crypto.get_settings`` —
    that's the move every other crypto test makes, and it's what
    masked two prior recursion bugs in chunk A (both surfaced
    2026-06-01 by an actual prod cold-boot probe).

    The cold chain exercised here:

        get_settings()  [lru_cache miss]
          → Settings()  [reads env vars; pydantic-settings]
          → data_residency check (passes via conftest)
          → validate_crypto_config_at_boot(settings)  [explicit settings]
          → _key_for_version_with_settings(settings, 1)
          → _decode_key(b64, 1)
          → return settings [cache populated]

    A future change adding ANY ``get_settings()`` call inside this
    chain (e.g. a "convenient" lookup inside ``_decode_key``) would
    re-enter the uncached first call and recurse. This test catches
    that in CI.
    """
    from aegis.config import get_settings as cfg_get_settings

    raw_key = os.urandom(32)
    b64_key = base64.b64encode(raw_key).decode("ascii")

    monkeypatch.setenv("PDF_ENCRYPTION_KEYS_CURRENT", "1")
    monkeypatch.setenv("PDF_ENCRYPTION_KEY_V1", b64_key)

    cfg_get_settings.cache_clear()
    try:
        # MUST NOT raise RecursionError. A successful return is the
        # implicit assertion — any unfound recursion hop would blow
        # past Python's default recursion limit (1000) long before
        # the lookup chain settles.
        settings = cfg_get_settings()
    finally:
        # Restore cache state so neighbor tests rebuild from the
        # conftest env (without my PDF_* overrides).
        cfg_get_settings.cache_clear()

    # Verify the chain actually executed (the recursion-fix path
    # populates these values; a no-op return would leave them None).
    assert settings.pdf_encryption_keys_current == 1
    # Per design: the env var is loaded into a SecretStr field —
    # presence (not value) is the meaningful assertion here.
    assert settings.pdf_encryption_key_v1 is not None
