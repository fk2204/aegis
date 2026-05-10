"""Bearer-token auth dependency.

Constant-time compare against the configured token. /healthz is the only
route that does NOT depend on this.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from aegis.config import get_settings
from aegis.logger import get_logger

_log = get_logger(__name__)

# Module-level latch so the unconfigured-token warning fires exactly once
# per process. Toggled by ``warn_if_bearer_unconfigured`` (called from the
# app startup hook); reading authenticated routes will still 503, but the
# operator gets a clear signal at boot instead of only on first request.
_unconfigured_warning_emitted = False


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
