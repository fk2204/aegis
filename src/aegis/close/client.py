"""Close CRM REST client — thin httpx wrapper with retry + rate-limit awareness.

Scope (deliberately narrow per the integration design step 2):

* HTTP transport + auth only. No business logic, no field mapping, no
  sync orchestration. Those live in sibling modules (`field_map.py`,
  `sync.py`) added in later steps of this branch.
* Methods covered: `get_lead`, `update_lead_custom_fields`,
  `get_opportunity`, `download_attachment`, and the generic `request`
  the typed methods delegate to.

Auth
----
HTTP Basic with the API key as the username and a blank password
(``Authorization: Basic base64(api_key:)``). Close's standard pattern
per https://developer.close.com/api/overview/api-key-authentication/.
The key is treated as opaque — no format validation. Single key for
v1; no rotation logic. Configured via ``CLOSE_API_KEY`` and loaded
through ``aegis.config.get_settings()``.

Retries
-------
``tenacity`` retries on 429, 5xx, and httpx transport errors with
exponential backoff (3 attempts total). 4xx other than 429 fails fast
— in particular 401 means the key is wrong and retrying just wastes
the rate budget. The final error always carries the response status
code and a truncated, non-sensitive body excerpt.

Rate limits
-----------
Close publishes a ``RateLimit`` header on every response in the format
``limit=N, remaining=M, reset=S``. On 429 the client reads ``reset``
(preferred per Close docs) or ``retry-after`` and sleeps that long
inside the retry loop. There is no client-side proactive throttle —
Close doesn't publish exact per-key numbers, so we trust the server
signal. 429 hits are logged at WARNING and (when an ``AuditLog`` is
injected) also written to ``audit_log`` as ``close.rate_limit_hit``.

httpx INFO log silenced
-----------------------
Mirrors the precaution used in older AEGIS HTTP clients: httpx logs request
URLs at INFO. We push it to WARNING so any future query-string drift
cannot leak credentials by accident.
"""

from __future__ import annotations

import logging
import ssl
import time
from email.message import Message
from typing import TYPE_CHECKING, Any

import httpx
import truststore
from pydantic import BaseModel, ConfigDict, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from aegis.config import get_settings
from aegis.logger import get_logger

if TYPE_CHECKING:
    from aegis.audit import AuditLog

_log = get_logger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# Use the OS-native certificate store for TLS verification instead of
# Python's bundled certifi roots. Required on Windows dev machines where
# layered TLS (Tailscale et al.) block OCSP/CRL access through Python's
# default chain — every HTTPS call fails CERTIFICATE_VERIFY_FAILED. On
# Linux this is a no-op equivalent to the default. Constructed once at
# module load and reused across all CloseClient instances.
_TLS_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
_DEFAULT_RATE_LIMIT_SLEEP = 2.0  # fallback when 429 carries no usable hint
_BODY_TRUNCATE_LEN = 500


class CloseError(RuntimeError):
    """Base class for Close client failures.

    ``status_code`` and ``body`` are set on the exception so callers can
    branch on them; both are also baked into the str() form. ``body``
    is truncated to keep tracebacks bounded.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class CloseAuthError(CloseError):
    """The CLOSE_API_KEY env var is missing OR Close returned 401.

    Fails fast — never retried. 401 from Close means the key is wrong,
    revoked, or scoped out; retrying just wastes the rate budget and
    Close-side metering.
    """


class CloseAttachment(BaseModel):
    """One file attached to a Close Lead, as returned by the Lead Files API.

    Source: ``GET /api/v1/lead/{lead_id}/files/`` — the unified index that
    Close's UI uses for the "Files" tab. Aggregates files from every
    activity type (Notes, Emails, SMS/MMS, custom activities) AND files
    attached directly to the Lead with no activity wrapper. The provenance
    is carried on ``last_object_type`` / ``last_object_id``.

    Used by the attachment-orchestration arq job
    (``process_close_attachments``) to decide which attachments to pull
    through the parser. Filtering pipeline applied in the orchestrator:

      1. ``content_type == 'application/pdf'`` (strict — kills the
         PNG-named-statement case).
      2. ``is_pinned == True`` by default (operator-confirmed gate).
         Bypassed by the rescan-with-``ignore_pin`` path.
      3. ``checksum`` (MD5) dedup before download — same file attached
         twice (once to a Note, once direct to Lead) downloads once.

    Lenient on extra fields — Close's Lead Files response carries many
    keys we don't read (organization_id, thumbnail_url, lead_id, etc.).
    ``extra='ignore'`` keeps us robust to upstream additions.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    content_type: str | None = None
    size: int | None = None

    # MD5 checksum Close computes server-side. Used by the orchestrator
    # for cheap dedup before fetching bytes — same file attached twice
    # (e.g. via a Note AND as a direct Lead drop) downloads once.
    checksum: str | None = None

    # The persisted URL the file lives at. Always points at
    # ``app.close.com/go/file/persisted/...`` in responses; the
    # download path rewrites the host to ``api.close.com`` before
    # fetching (``app.close.com`` rejects API-key auth with HTTP 400).
    # Optional only to keep the model parseable when a future Close
    # response omits it; callers downloading bytes must check non-None.
    download_url: str | None = None

    # Operator-set pin flag (Close UI only — the public API exposes
    # this field read-only). When True, signals "operator confirms this
    # file is a bank statement" — the orchestrator's default filter.
    is_pinned: bool = False

    # Provenance — which activity (if any) the file was attached
    # through. Observed values:
    #   - "activity.note"  — Notes carrying attachments (web form, Close
    #                        "Add note" with file)
    #   - "activity.email" — Email attachments (inbound or composed)
    #   - "activity.sms"   — MMS attachments (none in our org currently)
    #   - "lead"           — File attached directly to the Lead via
    #                        Close UI drag-drop without a note wrapper
    # Informational; not used for filtering today.
    last_object_type: str | None = None
    last_object_id: str | None = None

    # Legacy field kept for backwards compatibility with chunk-1 tests
    # (the prior ``/api/v1/files/?lead_id=`` response shape included it
    # under different keys). The Lead Files endpoint does NOT return
    # this field; it will always be None going forward.
    created_by_name: str | None = None
    date_created: str | None = None


class CloseRateLimitError(CloseError):
    """429 Too Many Requests. Retried by tenacity within the budget.

    ``reset_seconds`` is the sleep hint extracted from the ``RateLimit``
    header's ``reset`` token (preferred per Close docs) or the
    ``Retry-After`` header. Defaults to ``_DEFAULT_RATE_LIMIT_SLEEP``
    when neither is present.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        body: str | None = None,
        reset_seconds: float = _DEFAULT_RATE_LIMIT_SLEEP,
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        self.reset_seconds = reset_seconds


class CloseClient:
    """Thin Close REST wrapper. One instance per process is fine.

    All request parameters and bodies stay typed (``dict[str, Any]``);
    field mapping to Pydantic models is the field_map module's job.
    """

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        # When no http_client is injected, build one that verifies against
        # the OS-native trust store (see _TLS_CONTEXT). Tests inject their
        # own MockTransport-backed client, which doesn't touch TLS.
        self._http = http_client or httpx.Client(
            timeout=_DEFAULT_TIMEOUT, verify=_TLS_CONTEXT
        )
        self._audit = audit

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CloseClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------
    # Generic request
    # ---------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(
            (httpx.TransportError, CloseRateLimitError)
        ),
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
        """Call ``CLOSE_API_BASE/{path}`` with Basic auth.

        Returns the JSON body as a dict on 2xx. Raises:

        - ``CloseAuthError`` on 401 (no retry).
        - ``CloseRateLimitError`` on 429 (sleeps reset_seconds, retried
          by the surrounding tenacity decorator up to the attempt budget).
        - ``CloseError`` on every other 4xx (no retry).
        - ``CloseError`` on 5xx — retried via tenacity through the
          ``retry_if_exception_type`` filter (5xx is caught and re-raised
          as a transient ``CloseError`` subclass through the same path
          tenacity sees).
        """
        settings = get_settings()
        if settings.close_api_key is None:
            raise CloseAuthError(
                "CLOSE_API_KEY is not configured; set it in .env or "
                "/etc/aegis/aegis.env"
            )

        url = f"{settings.close_api_base.rstrip('/')}{path}"
        auth = (settings.close_api_key.get_secret_value(), "")

        try:
            resp = self._http.request(
                method, url, json=json, params=params, auth=auth
            )
        except httpx.TransportError:
            # Tenacity will retry transport errors. Re-raise so the
            # decorator catches it.
            raise

        # Always observe the RateLimit header for debug visibility. Don't
        # block on it — Close doesn't publish exact numbers.
        rate_limit_header = resp.headers.get("RateLimit", "")
        if rate_limit_header:
            _log.debug("close.rate_limit headers=%s", rate_limit_header)

        if resp.status_code == 401:
            raise CloseAuthError(
                f"close 401: {self._safe_body(resp)}",
                status_code=401,
                body=self._safe_body(resp),
            )

        if resp.status_code == 429:
            reset_seconds = self._parse_reset_seconds(resp)
            body = self._safe_body(resp)
            _log.warning(
                "close.rate_limit_hit reset_seconds=%s body=%s",
                reset_seconds,
                body,
            )
            self._audit_rate_limit(
                method=method, path=path, reset_seconds=reset_seconds
            )
            # Respect the server signal before letting tenacity retry.
            # Sleeping here means the next retry attempt starts on the
            # other side of the reset window. Tenacity's exponential
            # backoff still applies on top — accepts a bit of extra
            # conservatism.
            time.sleep(reset_seconds)
            raise CloseRateLimitError(
                f"close 429: {body}",
                body=body,
                reset_seconds=reset_seconds,
            )

        if 500 <= resp.status_code < 600:
            # Transient 5xx — surface via CloseRateLimitError's parent
            # path so tenacity retries it.
            raise CloseRateLimitError(
                f"close {resp.status_code} transient: {self._safe_body(resp)}",
                status_code=resp.status_code,
                body=self._safe_body(resp),
                reset_seconds=0.0,
            )

        if resp.status_code >= 400:
            # Non-transient 4xx (other than 401 / 429). No retry.
            raise CloseError(
                f"close {resp.status_code}: {self._safe_body(resp)}",
                status_code=resp.status_code,
                body=self._safe_body(resp),
            )

        if resp.status_code == 204 or not resp.content:
            return {}
        body_json = resp.json()
        if not isinstance(body_json, dict):
            raise CloseError(
                f"close returned non-object body: "
                f"{type(body_json).__name__}"
            )
        return body_json

    # ---------------------------------------------------------------
    # Typed methods (thin wrappers — no business logic)
    # ---------------------------------------------------------------

    def get_lead(self, lead_id: str) -> dict[str, Any]:
        """GET /api/v1/lead/{lead_id}/ — returns the full Lead object."""
        return self.request("GET", f"/api/v1/lead/{lead_id}/")

    def update_lead_custom_fields(
        self, lead_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """PUT /api/v1/lead/{lead_id}/ — updates the named custom fields.

        ``fields`` must already be in Close-API shape — the field_map
        module turns AEGIS values into this shape (step 3). For Close
        custom fields the keys are ``custom.cf_<field_id>`` and values
        are the type-appropriate primitive.
        """
        return self.request("PUT", f"/api/v1/lead/{lead_id}/", json=fields)

    def get_opportunity(self, opportunity_id: str) -> dict[str, Any]:
        """GET /api/v1/opportunity/{opportunity_id}/."""
        return self.request("GET", f"/api/v1/opportunity/{opportunity_id}/")

    def list_lead_attachments(self, lead_id: str) -> list[CloseAttachment]:
        """GET /api/v1/lead/{lead_id}/files/ — unified Lead files index.

        Single source of truth for files on a Lead. Backs Close's "Files"
        tab in the UI and aggregates every file regardless of how it was
        attached:

          - Notes carrying attachments (``last_object_type='activity.note'``)
          - Email attachments (``last_object_type='activity.email'``)
          - SMS / MMS attachments (``last_object_type='activity.sms'``)
          - Files attached directly to the Lead via UI drag-drop with
            no activity wrapper (``last_object_type='lead'``)
          - Custom activity attachments (e.g. Commera's deferred
            Submission/Offer/Decline types)

        Returns ``list[CloseAttachment]`` carrying ``id``, ``name``,
        ``content_type``, ``size``, ``checksum`` (MD5), ``download_url``,
        ``is_pinned``, and provenance backrefs.

        Previously this method called ``/api/v1/files/?lead_id=...``
        which 404s in our Close org (the org-level Files API isn't
        available in our plan/version); every Feature-2 run silently
        no-op'd. Confirmed against a live web-form lead on 2026-05-28
        that the unified Lead-Files endpoint returns the 7 real files
        the form uploaded.

        Pagination: same Close convention — ``{"has_more": bool,
        "data": [...]}`` with ``_limit`` (capped at 100) and ``_skip``.
        Verified pagination behavior live with ``_limit=2`` against the
        7-file test lead.

        Same auth + retry semantics as siblings — each page goes
        through :meth:`request` with its own tenacity decorator.
        401 → CloseAuthError; 429 → CloseRateLimitError (retried);
        5xx → retried; other 4xx → CloseError raised.

        Returns an empty list if the Lead has no attachments.
        """
        items: list[CloseAttachment] = []
        skip = 0
        page_size = 100
        while True:
            page = self.request(
                "GET",
                f"/api/v1/lead/{lead_id}/files/",
                params={
                    "_limit": page_size,
                    "_skip": skip,
                },
            )
            raw_items = page.get("data", [])
            if not isinstance(raw_items, list):
                raise CloseError(
                    "close /api/v1/lead/{id}/files/ returned non-list data: "
                    f"{type(raw_items).__name__}"
                )
            for raw in raw_items:
                if not isinstance(raw, dict):
                    raise CloseError(
                        "close /api/v1/lead/{id}/files/ returned non-object item: "
                        f"{type(raw).__name__}"
                    )
                items.append(CloseAttachment.model_validate(raw))
            if not page.get("has_more"):
                return items
            skip += page_size

    @retry(
        retry=retry_if_exception_type(
            (httpx.TransportError, CloseRateLimitError)
        ),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    def download_attachment(self, attachment_id: str) -> tuple[bytes, str]:
        """GET /api/v1/files/{attachment_id}/download/.

        Returns ``(file_bytes, filename)``. ``filename`` is parsed from
        the response's ``Content-Disposition`` header (Close's standard
        for file downloads); falls back to ``"unknown.pdf"`` when the
        header is missing or unparseable.

        Used by the hybrid statement path (step 7) to pull bank-statement
        PDFs that the operator attached to a Close Lead instead of
        uploading to AEGIS directly.

        Same auth + retry semantics as :meth:`request`: 401 fails fast
        (wrong key — no retry), 429 sleeps the ``RateLimit`` reset value
        then retries within the tenacity budget, 5xx retries, other 4xx
        propagates immediately. Streams into memory bounded by the
        operator-configured upload cap upstream — the client does not
        enforce that cap itself.
        """
        settings = get_settings()
        if settings.close_api_key is None:
            raise CloseAuthError(
                "CLOSE_API_KEY is not configured; set it in .env or "
                "/etc/aegis/aegis.env"
            )
        url = (
            f"{settings.close_api_base.rstrip('/')}"
            f"/api/v1/files/{attachment_id}/download/"
        )
        auth = (settings.close_api_key.get_secret_value(), "")
        resp = self._http.request("GET", url, auth=auth)

        if resp.status_code == 401:
            raise CloseAuthError(
                "close 401 on attachment download",
                status_code=401,
                body=self._safe_body(resp),
            )

        if resp.status_code == 429:
            reset_seconds = self._parse_reset_seconds(resp)
            body = self._safe_body(resp)
            _log.warning(
                "close.rate_limit_hit reset_seconds=%s body=%s (attachment download)",
                reset_seconds,
                body,
            )
            self._audit_rate_limit(
                method="GET",
                path=f"/api/v1/files/{attachment_id}/download/",
                reset_seconds=reset_seconds,
            )
            time.sleep(reset_seconds)
            raise CloseRateLimitError(
                f"close 429 on attachment download: {body}",
                body=body,
                reset_seconds=reset_seconds,
            )

        if 500 <= resp.status_code < 600:
            raise CloseRateLimitError(
                f"close {resp.status_code} transient on attachment download: "
                f"{self._safe_body(resp)}",
                status_code=resp.status_code,
                body=self._safe_body(resp),
                reset_seconds=0.0,
            )

        if resp.status_code >= 400:
            raise CloseError(
                f"close {resp.status_code} on attachment download: "
                f"{self._safe_body(resp)}",
                status_code=resp.status_code,
                body=self._safe_body(resp),
            )

        filename = (
            _filename_from_content_disposition(
                resp.headers.get("content-disposition", "")
            )
            or "unknown.pdf"
        )
        return resp.content, filename

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    @staticmethod
    def _safe_body(resp: httpx.Response) -> str:
        """Truncated, header-free body for error messages.

        Truncates at ``_BODY_TRUNCATE_LEN`` characters. The response
        body itself does not contain the Authorization header — that's
        a request header — so this is really a length-cap, not a redact.
        Kept conservative so a future change to httpx error formatting
        cannot accidentally pull request metadata into a string.
        """
        try:
            text = resp.text
        except Exception:
            return "<unreadable body>"
        if len(text) > _BODY_TRUNCATE_LEN:
            return text[:_BODY_TRUNCATE_LEN] + "…"
        return text

    @staticmethod
    def _parse_reset_seconds(resp: httpx.Response) -> float:
        """Extract the sleep hint from a 429 response.

        Preference order, per Close docs:
          1. ``RateLimit`` header's ``reset`` token
             (e.g. "limit=100, remaining=0, reset=5" -> 5.0)
          2. ``Retry-After`` header (RFC 7231 — seconds or HTTP-date;
             we accept the integer-seconds form)
          3. ``_DEFAULT_RATE_LIMIT_SLEEP`` (2 seconds)
        """
        rate_limit_header = resp.headers.get("RateLimit", "")
        for token in rate_limit_header.split(","):
            token = token.strip()
            if token.lower().startswith("reset="):
                try:
                    return float(token.split("=", 1)[1])
                except ValueError:
                    break

        retry_after = resp.headers.get("Retry-After", "").strip()
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass

        return _DEFAULT_RATE_LIMIT_SLEEP

    def _audit_rate_limit(
        self, *, method: str, path: str, reset_seconds: float
    ) -> None:
        """Best-effort audit row for 429 visibility. No-op when no audit
        sink is injected (unit tests, scripts)."""
        if self._audit is None:
            return
        try:
            self._audit.record(
                actor="close_client",
                action="close.rate_limit_hit",
                details={
                    "method": method,
                    "path": path,
                    "reset_seconds": reset_seconds,
                },
            )
        except Exception:
            # An audit failure must not mask the rate-limit signal.
            # The standard logger warning above is the primary signal.
            _log.warning("close.rate_limit_audit_write_failed", exc_info=True)


def _filename_from_content_disposition(header_value: str) -> str | None:
    """Parse the ``filename`` parameter from a Content-Disposition header.

    Handles the standard ``filename="x.pdf"`` form and the RFC 5987
    ``filename*=UTF-8''x.pdf`` form. Uses ``email.message.Message`` so
    we don't ship a hand-rolled regex parser. Returns None if no
    filename token is present.
    """
    if not header_value:
        return None
    m = Message()
    m["content-disposition"] = header_value
    return m.get_filename()


__all__ = [
    "CloseAttachment",
    "CloseAuthError",
    "CloseClient",
    "CloseError",
    "CloseRateLimitError",
]
