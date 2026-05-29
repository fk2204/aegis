"""Auth dependencies.

Two surfaces:

1. ``require_bearer`` — constant-time compare against ``API_BEARER_TOKEN``.
   Used by the operator-facing API routes (/upload, /merchants, /deals,
   /funders, etc.).
2. ``require_close_callback_bearer`` — same constant-time bearer pattern
   but scoped to ``CLOSE_CALLBACK_TOKEN``. Used only by the
   ``/api/close-callback/*`` router so the Close-side trigger gets its
   own token that rotates independently from the operator API key.

/healthz is the only route that does NOT depend on either of these.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from aegis.config import get_settings
from aegis.logger import get_logger

_log = get_logger(__name__)

# Module-level latches so each unconfigured-token warning fires exactly
# once per process. Toggled by the corresponding warn_if_*_unconfigured
# helpers from the app startup hook; reading authenticated routes will
# still 503, but the operator gets a clear signal at boot instead of
# only on first request.
_unconfigured_warning_emitted = False
_close_callback_token_warning_emitted = False


def warn_if_bearer_unconfigured() -> bool:
    """Emit a one-time WARN if API_BEARER_TOKEN is unset.

    Returns True if a warning was emitted on this call, False otherwise.
    Safe to call multiple times — the latch ensures a single log line.
    """
    global _unconfigured_warning_emitted
    settings = get_settings()
    expected = (
        settings.api_bearer_token.get_secret_value()
        if settings.api_bearer_token
        else ""
    )
    if expected:
        return False
    if _unconfigured_warning_emitted:
        return False
    _unconfigured_warning_emitted = True
    _log.warning(
        "auth.bearer_token_unconfigured "
        "API_BEARER_TOKEN is not set; all authenticated routes will return 503"
    )
    return True


async def require_bearer(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency: reject unless `Authorization: Bearer <token>` matches."""
    settings = get_settings()
    expected = settings.api_bearer_token.get_secret_value() if settings.api_bearer_token else ""
    if not expected:
        # Refuse rather than silently allow if the token is unconfigured.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_BEARER_TOKEN is not configured",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Close → AEGIS callback router auth
# ---------------------------------------------------------------------------


def warn_if_close_callback_token_unconfigured() -> bool:
    """Emit a one-time WARN if CLOSE_CALLBACK_TOKEN is unset.

    Same shape as ``warn_if_bearer_unconfigured`` but scoped to the
    Close-callback router. Returns True iff a warning was emitted on
    this call (test-observable).
    """
    global _close_callback_token_warning_emitted
    settings = get_settings()
    expected = (
        settings.close_callback_token.get_secret_value()
        if settings.close_callback_token
        else ""
    )
    if expected:
        return False
    if _close_callback_token_warning_emitted:
        return False
    _close_callback_token_warning_emitted = True
    _log.warning(
        "auth.close_callback_token_unconfigured "
        "CLOSE_CALLBACK_TOKEN is not set; /api/close-callback/* will 503"
    )
    return True


async def require_close_callback_bearer(
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: reject unless ``Authorization: Bearer <token>``
    matches ``CLOSE_CALLBACK_TOKEN``.

    Same shape as ``require_bearer`` but a separate env var so the
    Close-callback surface and the operator API surface rotate
    independently.

    Fail-closed contract:
      * Token unset       → 503 (operator misconfiguration; integration not ready)
      * Header missing    → 401
      * Token mismatch    → 401 (constant-time compare)
    """
    settings = get_settings()
    expected = (
        settings.close_callback_token.get_secret_value()
        if settings.close_callback_token
        else ""
    )
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLOSE_CALLBACK_TOKEN is not configured",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _reset_close_callback_warning_latch() -> None:
    """Reset module-level warning latch — tests use this to re-exercise
    ``warn_if_close_callback_token_unconfigured`` between cases."""
    global _close_callback_token_warning_emitted
    _close_callback_token_warning_emitted = False


__all__ = [
    "require_bearer",
    "require_close_callback_bearer",
    "warn_if_bearer_unconfigured",
    "warn_if_close_callback_token_unconfigured",
]
