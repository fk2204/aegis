"""Auth dependencies.

Three surfaces:

1. ``require_bearer`` — constant-time compare against ``API_BEARER_TOKEN``.
   Used by the operator-facing API routes (/upload, /merchants, /deals,
   /funders, etc.).
2. ``verify_close_callback_signature`` — HMAC over the request body, same
   shape as ``/webhooks/close``, against ``CLOSE_CALLBACK_HMAC_SECRET``.
   Used by the new Close → AEGIS callback router. The HMAC secret is
   distinct from CLOSE_WEBHOOK_SECRET so the two Close subscriptions
   can be rotated independently.
3. ``verify_close_callback_bearer_if_configured`` — optional defense-in-
   depth layer for the callback router. When ``CLOSE_CALLBACK_TOKEN`` is
   set in env, requires ``Authorization: Bearer <token>``; when unset,
   the bearer check is a no-op and HMAC alone protects. Lets the router
   ship before we verify whether Close webhooks can carry a custom
   Authorization header.

/healthz is the only route that does NOT depend on any of these.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

from fastapi import Header, HTTPException, Request, status

from aegis.config import get_settings
from aegis.logger import get_logger

_log = get_logger(__name__)

# Module-level latch so the unconfigured-token warning fires exactly once
# per process. Toggled by ``warn_if_bearer_unconfigured`` (called from the
# app startup hook); reading authenticated routes will still 503, but the
# operator gets a clear signal at boot instead of only on first request.
_unconfigured_warning_emitted = False
_close_callback_hmac_warning_emitted = False

CLOSE_CALLBACK_FRESHNESS_SECONDS = 5 * 60


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


def warn_if_close_callback_hmac_unconfigured() -> bool:
    """Emit a one-time WARN if CLOSE_CALLBACK_HMAC_SECRET is unset.

    Same shape as ``warn_if_bearer_unconfigured`` but scoped to the
    Close-callback router. Returns True iff a warning was emitted on
    this call (test-observable).
    """
    global _close_callback_hmac_warning_emitted
    settings = get_settings()
    expected = (
        settings.close_callback_hmac_secret.get_secret_value()
        if settings.close_callback_hmac_secret
        else ""
    )
    if expected:
        return False
    if _close_callback_hmac_warning_emitted:
        return False
    _close_callback_hmac_warning_emitted = True
    _log.warning(
        "auth.close_callback_hmac_unconfigured "
        "CLOSE_CALLBACK_HMAC_SECRET is not set; /api/close-callback/* will 503"
    )
    return True


def _parse_close_timestamp(value: str) -> datetime:
    """Accept either Unix-epoch-seconds or ISO 8601. Raises ValueError.

    Matches the parser in ``webhooks_close._parse_timestamp`` — Close
    has emitted both formats over time and we want the new callback
    router to accept the same envelope as the existing webhook.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("empty timestamp")
    try:
        epoch = int(stripped)
    except ValueError:
        pass
    else:
        return datetime.fromtimestamp(epoch, tz=UTC)
    return datetime.fromisoformat(stripped.replace("Z", "+00:00"))


async def verify_close_callback_signature(request: Request) -> None:
    """FastAPI dependency: validate HMAC signature on the request body.

    Hard-required. Same envelope as ``webhooks_close._verify_signature``:
    ``close-sig-hash`` header carries the hex HMAC-SHA256 of
    (close-sig-timestamp + raw_body) using the secret returned by Close
    on subscription creation. 5-minute freshness window.

    Fail-closed contract:
      * Secret unset       → 503 (operator misconfiguration; integration not ready)
      * Secret malformed   → 503 (operator copied wrong value; not auth failure)
      * Missing headers    → 401 (generic — don't leak which check failed)
      * Stale timestamp    → 401
      * HMAC mismatch      → 401
    """
    settings = get_settings()
    if settings.close_callback_hmac_secret is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLOSE_CALLBACK_HMAC_SECRET is not configured",
        )

    presented_sig = request.headers.get("close-sig-hash", "")
    timestamp_str = request.headers.get("close-sig-timestamp", "")
    if not presented_sig or not timestamp_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )

    try:
        ts_dt = _parse_close_timestamp(timestamp_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        ) from None

    age = abs((datetime.now(UTC) - ts_dt).total_seconds())
    if age > CLOSE_CALLBACK_FRESHNESS_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )

    secret_hex = settings.close_callback_hmac_secret.get_secret_value()
    try:
        secret_bytes = bytes.fromhex(secret_hex)
    except ValueError:
        _log.error("close_callback_hmac_secret_not_valid_hex")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="CLOSE_CALLBACK_HMAC_SECRET is not valid hex",
        ) from None

    raw_body = await request.body()
    data = timestamp_str.encode("utf-8") + raw_body
    expected = hmac.new(secret_bytes, data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(presented_sig, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )


async def verify_close_callback_bearer_if_configured(
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: optional bearer check for the close-callback router.

    Defense-in-depth layer on top of the HMAC signature. Behavior:
      * ``CLOSE_CALLBACK_TOKEN`` unset → skip the bearer check entirely
        (HMAC alone gates the route). Logged at DEBUG once per process
        through the boot-guard path; not a warning, because unset is a
        valid configuration.
      * ``CLOSE_CALLBACK_TOKEN`` set:
          - Missing or malformed Authorization header → 401
          - Token mismatch → 401 (constant-time compare)
          - Token match → pass

    The asymmetry between HMAC (always required) and bearer (only when
    configured) is the explicit design tradeoff documented in the
    Settings docstring: HMAC is the established envelope, bearer is a
    bonus when Close's webhook subscription supports custom headers.
    """
    settings = get_settings()
    expected_secret = settings.close_callback_token
    if expected_secret is None:
        return

    expected = expected_secret.get_secret_value()
    if not expected:
        return

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
    ``warn_if_close_callback_hmac_unconfigured`` between cases."""
    global _close_callback_hmac_warning_emitted
    _close_callback_hmac_warning_emitted = False


__all__ = [
    "CLOSE_CALLBACK_FRESHNESS_SECONDS",
    "require_bearer",
    "verify_close_callback_bearer_if_configured",
    "verify_close_callback_signature",
    "warn_if_bearer_unconfigured",
    "warn_if_close_callback_hmac_unconfigured",
]
