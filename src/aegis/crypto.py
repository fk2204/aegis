"""Client-side AES-256-GCM encryption for at-rest PDF storage.

Chunk A of the PDF retention redesign — see
``docs/PDF_RETENTION_DESIGN.md`` §6. No callers yet; the worker
(chunk B) and view route (chunk C) wire encrypt/decrypt into the
upload + view paths.

Keys live in ``/etc/aegis/aegis.env`` as ``PDF_ENCRYPTION_KEY_V{n}``
(base64-encoded, exactly 32 bytes each after decode).
``PDF_ENCRYPTION_KEYS_CURRENT`` points at the version used for new
writes. Old versions stay in the env file as long as any documents
row still references them.

Threat model boundary (per design doc §2): compromised Supabase
Storage = ciphertext only. Compromised box = full disclosure
(encryption keys + Supabase service_role creds both live in
``aegis.env``). Mitigating box compromise requires KMS or HSM —
deferred to a future migration.
"""
from __future__ import annotations

import base64
import os
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aegis.config import get_settings

_NONCE_BYTES: Final[int] = 12       # AES-GCM standard
_TAG_BYTES: Final[int] = 16         # AES-GCM auth tag length
_MIN_BLOB_BYTES: Final[int] = _NONCE_BYTES + _TAG_BYTES  # 28
_KEY_BYTES: Final[int] = 32         # 256-bit


class CryptoConfigError(RuntimeError):
    """Boot-time configuration problem. Raised when a key is missing,
    the wrong length, or PDF_ENCRYPTION_KEYS_CURRENT points at a
    version without a configured key. Refuses to let the worker /
    view route proceed against a misconfigured environment.
    """


class CorruptCiphertextError(RuntimeError):
    """Integrity failure at decrypt time. Raised when the blob is
    shorter than nonce+tag, or when AES-GCM rejects the auth tag
    (wrong key, tampered ciphertext, corrupted bytes). The view route
    maps this to HTTP 500 + a ``document.original_viewed_integrity_failed``
    audit row.
    """


def _decode_key(b64: str, version: int) -> bytes:
    """Decode a base64-encoded key and validate its length.

    Raised errors are framed for the boot-time guard: any
    ``CryptoConfigError`` here refuses startup, so a malformed
    ``PDF_ENCRYPTION_KEY_V{n}`` can never reach a runtime call.
    """
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception as exc:
        raise CryptoConfigError(
            f"PDF_ENCRYPTION_KEY_V{version} is not valid base64: {exc}"
        ) from exc
    if len(raw) != _KEY_BYTES:
        raise CryptoConfigError(
            f"PDF_ENCRYPTION_KEY_V{version} must decode to exactly "
            f"{_KEY_BYTES} bytes (got {len(raw)})"
        )
    return raw


def _key_for_version(version: int) -> bytes:
    """Look up the raw 32-byte key for the given version.

    Raises ``CryptoConfigError`` if the version is not configured in
    settings. Used by both ``encrypt_pdf`` (with the current version)
    and ``decrypt_pdf`` (with the per-document stored version).
    """
    settings = get_settings()
    env_attr = f"pdf_encryption_key_v{version}"
    secret = getattr(settings, env_attr, None)
    if secret is None:
        raise CryptoConfigError(
            f"PDF_ENCRYPTION_KEY_V{version} not configured "
            f"(referenced via encryption_key_version={version})"
        )
    return _decode_key(secret.get_secret_value(), version)


def current_key_version() -> int:
    """Return the key version new writes seal with.

    The boot guard in ``aegis.config`` validates that this version
    points at a configured key that decodes to exactly 32 bytes —
    runtime callers can rely on the lookup succeeding.
    """
    return get_settings().pdf_encryption_keys_current


def encrypt_pdf(plaintext: bytes, *, key_version: int) -> bytes:
    """Encrypt ``plaintext`` with the named key version.

    Returns ``nonce(12) || ciphertext || tag(16)``. The nonce is
    generated per call via ``os.urandom(12)`` — two encryptions of
    the same plaintext under the same key produce distinct
    ciphertexts (locked down by
    ``tests.test_crypto.test_same_plaintext_different_ciphertext``).

    Raises ``CryptoConfigError`` if the key version is missing
    (callers should always pass ``current_key_version()`` and never
    a stale/invented version).
    """
    key = _key_for_version(key_version)
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext, associated_data=None)
    return nonce + sealed


def decrypt_pdf(blob: bytes, *, key_version: int) -> bytes:
    """Inverse of ``encrypt_pdf``.

    Splits the leading 12 bytes as the nonce, the rest as
    ``ciphertext || tag``. AES-GCM verifies the tag and returns the
    plaintext; rejection raises ``CorruptCiphertextError``.

    Defensive guards:
      * Blob shorter than ``nonce+tag`` (28 bytes) cannot be a valid
        AES-GCM output and is rejected immediately with a clearer
        error than ``cryptography`` would emit on the truncated read.
      * Any exception from ``AESGCM.decrypt`` (including the
        ``InvalidTag`` that signals tampering or wrong key) is
        wrapped in ``CorruptCiphertextError`` so the view route's
        catch is type-stable.
    """
    if len(blob) < _MIN_BLOB_BYTES:
        raise CorruptCiphertextError(
            f"blob shorter than nonce+tag ({_MIN_BLOB_BYTES} bytes); "
            f"got {len(blob)}"
        )
    key = _key_for_version(key_version)
    nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, associated_data=None)
    except Exception as exc:  # cryptography.exceptions.InvalidTag is the typical case
        raise CorruptCiphertextError(str(exc) or type(exc).__name__) from exc


def validate_crypto_config_at_boot() -> None:
    """Boot-time guard.

    Confirms that ``PDF_ENCRYPTION_KEYS_CURRENT`` points at a configured
    key that decodes to exactly 32 bytes. Called by
    ``aegis.config.Settings`` validators so a misconfigured environment
    refuses to start instead of silently failing the first write.

    Skipped (no-op) when ``PDF_ENCRYPTION_KEYS_CURRENT`` is unset (zero
    or None) — chunk A ships before any caller depends on a populated
    key, so the worker / view route would no-op naturally. Once chunk B
    deploys, the systemd unit + ops runbook ensure the current-version
    key is configured.
    """
    settings = get_settings()
    current = settings.pdf_encryption_keys_current
    if current is None or current == 0:
        return  # not yet rotated in; no callers depend on it
    # Triggering _key_for_version raises CryptoConfigError on any failure
    _key_for_version(current)


__all__ = [
    "CorruptCiphertextError",
    "CryptoConfigError",
    "current_key_version",
    "decrypt_pdf",
    "encrypt_pdf",
    "validate_crypto_config_at_boot",
]
