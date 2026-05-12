"""Zoho REST client with auto-refreshing OAuth + retrying transport.

Tokens
------
Zoho's OAuth flow is refresh-token-based: a long-lived refresh_token
trades for a short-lived (1h) access_token. We hold a single
``ZohoTokenCache`` per process; it refreshes automatically when the
cached token has < 60s left, and re-fetches on a 401 response.

Retries
-------
``tenacity`` retries on transient HTTP errors (429, 5xx, network) with
exponential backoff. 4xx errors propagate immediately so a malformed
payload doesn't loop.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from aegis.config import get_settings
from aegis.logger import get_logger

_log = get_logger(__name__)

# httpx logs every request URL at INFO. The OAuth refresh URL would carry
# refresh_token/client_id/client_secret as form data (see _refresh_token),
# but logger configuration drift elsewhere could still surface request
# metadata. Belt + suspenders: silence httpx INFO logs so a future
# reintroduction of query-string params cannot leak credentials.
logging.getLogger("httpx").setLevel(logging.WARNING)

_TOKEN_GRACE_SECONDS = 60
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)


class ZohoError(RuntimeError):
    """Base class for Zoho client failures."""


class ZohoAuthError(ZohoError):
    """OAuth refresh failed or required credentials missing."""


class ZohoTokenCache:
    """Process-wide access-token cache. Refresh on first use + on expiry."""

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def get(self, *, force_refresh: bool = False) -> str:
        if (
            not force_refresh
            and self._access_token
            and self._expires_at - time.time() > _TOKEN_GRACE_SECONDS
        ):
            return self._access_token

        settings = get_settings()
        if settings.zoho_refresh_token is None or not settings.zoho_client_id:
            raise ZohoAuthError(
                "ZOHO_CLIENT_ID and ZOHO_REFRESH_TOKEN must be configured"
            )

        url = f"{settings.zoho_accounts_base}/oauth/v2/token"
        # Form body, NOT query params — query params land in any logged
        # request URL (httpx, traffic captures, error tracebacks). Body
        # values stay off the URL line.
        form_data = {
            "refresh_token": settings.zoho_refresh_token.get_secret_value(),
            "client_id": settings.zoho_client_id,
            "client_secret": (
                settings.zoho_client_secret.get_secret_value()
                if settings.zoho_client_secret
                else ""
            ),
            "grant_type": "refresh_token",
        }
        try:
            resp = httpx.post(url, data=form_data, timeout=_DEFAULT_TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ZohoAuthError(f"token refresh failed: {exc}") from exc

        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise ZohoAuthError(f"token refresh missing access_token: {body!r}")

        # Zoho returns expires_in in seconds; default 3600.
        self._access_token = str(token)
        self._expires_at = time.time() + float(body.get("expires_in", 3600))
        return self._access_token


class ZohoClient:
    """Thin Zoho CRM wrapper. Token cache is per-instance for testability."""

    def __init__(
        self,
        *,
        token_cache: ZohoTokenCache | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._tokens = token_cache or ZohoTokenCache()
        self._http = http_client or httpx.Client(timeout=_DEFAULT_TIMEOUT)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ZohoClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, ZohoError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call ``ZOHO_API_BASE/{path}`` with OAuth headers; refresh on 401."""
        settings = get_settings()
        url = f"{settings.zoho_api_base.rstrip('/')}{path}"
        headers = self._auth_headers()
        resp = self._http.request(method, url, json=json, params=params, headers=headers)

        if resp.status_code == 401:
            # Token may have been revoked / rotated; force refresh and retry once.
            headers = self._auth_headers(force_refresh=True)
            resp = self._http.request(
                method, url, json=json, params=params, headers=headers
            )

        if resp.status_code in (429, 500, 502, 503, 504):
            raise ZohoError(f"transient {resp.status_code}: {resp.text[:200]}")

        if resp.status_code >= 400:
            raise ZohoError(f"zoho {resp.status_code}: {resp.text[:200]}")

        if resp.status_code == 204 or not resp.content:
            return {}
        body = resp.json()
        if not isinstance(body, dict):
            raise ZohoError(f"zoho returned non-object body: {type(body).__name__}")
        return body

    def _auth_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        token = self._tokens.get(force_refresh=force_refresh)
        return {"Authorization": f"Zoho-oauthtoken {token}"}

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, ZohoError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    def upload_attachment(
        self,
        module: str,
        record_id: str,
        *,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """POST a file to ``/crm/v8/{module}/{record_id}/Attachments``.

        Used by the sync to land the AEGIS findings CSV on the Zoho Lead or
        Deal so the rep sees it inside Zoho. Returns the parsed response
        body. Same retry policy as :meth:`request`; same 401-refresh dance.
        """
        settings = get_settings()
        url = (
            f"{settings.zoho_api_base.rstrip('/')}"
            f"/crm/v8/{module}/{record_id}/Attachments"
        )
        files = {"file": (filename, content, content_type)}
        headers = self._auth_headers()
        resp = self._http.request("POST", url, files=files, headers=headers)
        if resp.status_code == 401:
            headers = self._auth_headers(force_refresh=True)
            resp = self._http.request("POST", url, files=files, headers=headers)
        if resp.status_code in (429, 500, 502, 503, 504):
            raise ZohoError(f"transient {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ZohoError(f"zoho {resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 204 or not resp.content:
            return {}
        body = resp.json()
        if not isinstance(body, dict):
            raise ZohoError(
                f"zoho returned non-object body: {type(body).__name__}"
            )
        return body


__all__ = ["ZohoAuthError", "ZohoClient", "ZohoError", "ZohoTokenCache"]
