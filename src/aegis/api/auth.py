"""Bearer-token auth dependency.

Constant-time compare against the configured token. /healthz is the only
route that does NOT depend on this.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from aegis.config import get_settings


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
