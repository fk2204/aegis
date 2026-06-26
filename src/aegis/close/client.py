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

import hashlib
import logging
import ssl
import time
from datetime import date
from email.message import Message
from typing import TYPE_CHECKING, Any, Self

import httpx
import truststore
from pydantic import BaseModel, ConfigDict, Field, model_validator
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

# Defensive bound on the activity scan inside list_lead_attachments. A
# misbehaving lead must not let the worker loop indefinitely. 1000 is
# well above realistic operator behavior (active leads top out at a
# few dozen activities; pathologically chatty leads have ~200) but
# small enough that the worker bails before redis-job-timeout fires.
_MAX_ACTIVITIES_PER_LEAD = 1000

# PDF magic bytes. download_attachment validates the body prefix so
# corrupted CDN responses or wrong content-type-from-S3 fail loud
# instead of silently writing garbage to the parser pipeline.
_PDF_MAGIC = b"%PDF-"

# Close serves activity attachment URLs from app.close.com but those
# hosts refuse API-key Basic auth (400 "use api.close.com"). Swapping
# the host to api.close.com routes the same path through their
# authenticating gateway, which then 302s to the S3 signed URL.
_CLOSE_APP_HOST = "https://app.close.com/"
_CLOSE_API_HOST = "https://api.close.com/"


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
    """One PDF attachment on a Close Note or Email activity for a Lead.

    Source: ``GET /api/v1/lead/{lead_id}/files/`` — the unified index that
    Close's UI uses for the "Files" tab. Aggregates files from every
    activity type (Notes, Emails, SMS/MMS, custom activities) AND files
    attached directly to the Lead with no activity wrapper. The provenance
    is carried on ``last_object_type`` / ``last_object_id``.

    Close's API does not expose a standalone /files/ resource — files
    live on activities (Note, Email) as ``attachments[]`` items. Confirmed
    against live Close payload for A&R KM LLC on 2026-06-05: the real
    wire shape on a Note attachment is
    ``{content_type, filename, size, thumbnail_url, url}`` and on an
    Email attachment is ``{content_id, content_type, filename,
    inline_only, size, url}``. **Neither carries an ``id`` field.** An
    earlier rewrite assumed ``id`` was present and crashed the worker
    with a Pydantic ValidationError on every real lead. The captured
    real-payload fixture is ``tests/close/fixtures/acti_note_with_pdf.json``;
    the structural-coverage tests in ``tests/close/test_client_attachments.py``
    pin the field set.

    Since the orchestrator's call signature uses ``att.id`` as the
    cache key into the URL cache, we synthesize a stable id from the
    URL when Close didn't send one: ``sha256(url)[:16]``. The id is
    only used as a local cache key and an audit-trace token — not sent
    back to Close — so a deterministic hash is sufficient and avoids
    refactoring every caller.

    The ``filename`` field on the wire is exposed as ``name`` here for
    consistency with the orchestrator's filename-filter call shape.
    ``populate_by_name=True`` keeps direct ``name=...`` kwargs working
    for tests that pre-date the activity-based shape.

    Lenient on extra fields — activity attachment dicts carry more keys
    than AEGIS cares about (``thumbnail_url``, ``content_id``,
    ``inline_only``, organization metadata, etc.). ``extra="ignore"``
    keeps the model robust to upstream additions without forcing a
    schema change here.

    Filtering pipeline applied in the orchestrator
    (``process_close_attachments`` worker):

      1. ``content_type == 'application/pdf'`` (strict — kills the
         PNG-named-statement case).
      2. ``is_pinned`` OR ``note_pinned`` (operator-confirmed gate).
         Bypassed by the rescan-with-``ignore_pin`` path.
      3. ``checksum`` (MD5) dedup before download — same file attached
         twice (once to a Note, once direct to Lead) downloads once.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, populate_by_name=True)

    id: str = Field(default="", description="synthesized from URL when absent")
    name: str = Field(min_length=1, alias="filename")
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

    # Operator-set pin flag on the leadfile itself (Close "Files" tab
    # pin icon). Set via Close UI only — the public API exposes the
    # field read-only. When True, signals "operator confirms this
    # file is a bank statement."
    is_pinned: bool = False

    # Note-level pin flag, lifted from ``activity.note.pinned`` when
    # the file is attached via a Note (``last_object_type='activity.note'``).
    # The 2026-05-28 real-data test surfaced that the natural Close UX
    # — the pin icon in the activity feed — sets pin on the wrapping
    # Note, not on the file directly. Operators rarely navigate to the
    # Files tab to pin individually. Orchestrator gates on
    # ``is_pinned OR note_pinned`` so either surface confirms "this is
    # a statement."
    #
    # Always False for non-note provenance variants (lead-direct,
    # email, sms, custom_activity) — only ``activity.note`` has a
    # pin concept in this codebase today.
    note_pinned: bool = False

    # Provenance — which activity (if any) the file was attached
    # through. Observed values:
    #   - "activity.note"  — Notes carrying attachments (web form, Close
    #                        "Add note" with file)
    #   - "activity.email" — Email attachments (inbound or composed)
    #   - "activity.sms"   — MMS attachments (none in our org currently)
    #   - "lead"           — File attached directly to the Lead via
    #                        Close UI drag-drop without a note wrapper
    # Drives the note_pinned join in list_lead_attachments.
    last_object_type: str | None = None
    last_object_id: str | None = None

    # Legacy field kept for backwards compatibility with chunk-1 tests
    # (the prior ``/api/v1/files/?lead_id=`` response shape included it
    # under different keys). The Lead Files endpoint does NOT return
    # this field; it will always be None going forward.
    created_by_name: str | None = None
    date_created: str | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _synthesize_id_from_url(self) -> Self:
        """Synthesize ``id`` from ``url`` when the wire payload omits it.

        Real Close activity attachments have no ``id`` field. The cache
        key still needs to be stable across the listing/download steps
        within a single worker invocation, so we use a 16-hex-char
        SHA-256 prefix of the URL — stable, collision-resistant, and
        deterministic for a given URL. Raise if both ``id`` and ``url``
        are absent (malformed payload — fail loud).
        """
        if not self.id:
            if not self.url:
                raise ValueError("CloseAttachment requires at least one of id or url")
            # Bypass validate_assignment to avoid recursion.
            object.__setattr__(self, "id", hashlib.sha256(self.url.encode()).hexdigest()[:16])
        return self


class CloseNote(BaseModel):
    """One Close ``activity/note`` record for a Lead.

    Feature D — merchant context refresh consumes
    ``GET /api/v1/activity/note/?lead_id=...&_limit=N`` and joins the
    bodies of the most recent N items into ``merchants.close_notes_summary``.
    Only the structural minimum is modeled; Close's note payload carries
    many more fields (organization_id, user_id, attachments[], etc.) that
    AEGIS does not need at this surface and that ``extra="ignore"`` drops
    safely.

    Note body field name. Per the captured live payload in
    ``tests/close/fixtures/activity_note_list.json`` (2026-06-18), Close
    returns the body verbatim under ``note``. Some legacy API docs
    reference ``note_html`` / ``body_text``; we explicitly DO NOT trust
    those — the wire shape is ``note``. The body content is PII-bearing
    and never logged.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)

    id: str = Field(min_length=1)
    note: str | None = None
    date_created: str | None = None


class CloseCall(BaseModel):
    """One Close ``activity/call`` record for a Lead.

    Feature D — merchant context refresh consumes
    ``GET /api/v1/activity/call/?lead_id=...&_limit=N`` and joins the
    ``note`` field (post-call operator notes / disposition transcript)
    of the most recent N items into ``merchants.close_call_transcripts``.

    Same posture as :class:`CloseNote`: structural minimum only, PII
    body, extra fields ignored. Close exposes additional fields
    (duration, recording_url, direction, etc.) that AEGIS does not need.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)

    id: str = Field(min_length=1)
    note: str | None = None
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
        self._http = http_client or httpx.Client(timeout=_DEFAULT_TIMEOUT, verify=_TLS_CONTEXT)
        self._audit = audit
        # In-memory cache populated by list_lead_attachments. Keyed by
        # the attachment id Close returns on each note/email activity;
        # value is (download_url, filename). download_attachment looks
        # this up to avoid a second round-trip to the activity endpoint.
        # Lives per CloseClient instance — fine for the one-instance-
        # per-worker-job lifecycle the orchestrator uses today.
        self._attachment_cache: dict[str, tuple[str, str]] = {}

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
        retry=retry_if_exception_type((httpx.TransportError, CloseRateLimitError)),
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
                "CLOSE_API_KEY is not configured; set it in .env or /etc/aegis/aegis.env"
            )

        url = f"{settings.close_api_base.rstrip('/')}{path}"
        auth = (settings.close_api_key.get_secret_value(), "")

        try:
            resp = self._http.request(method, url, json=json, params=params, auth=auth)
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
            self._audit_rate_limit(method=method, path=path, reset_seconds=reset_seconds)
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
            raise CloseError(f"close returned non-object body: {type(body_json).__name__}")
        return body_json

    # ---------------------------------------------------------------
    # Typed methods (thin wrappers — no business logic)
    # ---------------------------------------------------------------

    def get_lead(self, lead_id: str) -> dict[str, Any]:
        """GET /api/v1/lead/{lead_id}/ — returns the full Lead object."""
        return self.request("GET", f"/api/v1/lead/{lead_id}/")

    def update_lead_custom_fields(self, lead_id: str, fields: dict[str, Any]) -> dict[str, Any]:
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

    def update_opportunity_custom_fields(
        self, opportunity_id: str, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """PUT /api/v1/opportunity/{opportunity_id}/ — updates custom fields.

        Mirrors :meth:`update_lead_custom_fields`'s shape. ``fields``
        must already be in Close-API form (``custom.cf_<field_id>``
        keys, type-appropriate primitive values). The Opportunity-side
        sync helper (``aegis.close.sync.push_offer_to_opportunity``)
        wraps this with read-before-write diffing per the same
        idempotency guarantee the Lead sync uses.
        """
        return self.request("PUT", f"/api/v1/opportunity/{opportunity_id}/", json=fields)

    def create_task(
        self,
        lead_id: str,
        text: str,
        due_date: date | None = None,
        assigned_to: str | None = None,
    ) -> dict[str, Any]:
        """POST /api/v1/task/ — create a Lead Task in Close.

        Body shape per Close docs:
          { "_type": "lead", "lead_id": "<lead_id>", "text": "<task text>",
            "date": "<YYYY-MM-DD or omitted>", "assigned_to": "<user_id or omitted>" }

        Use this for operator-action prompts (e.g. "Pull credit score
        for X", "Fund the deal — funder approved Y for $Z"). The task
        becomes a Close-side checklist item the operator clears
        manually; AEGIS does NOT poll for completion.

        Caller responsibilities:
          * Idempotency. Close does not de-dupe; same payload twice
            creates two tasks. The standard guard is an ``audit_log``
            check via ``AuditLog.list_for_subject`` before calling.
          * Text composition. No length validation here — Close enforces
            its own server-side cap.

        Same auth + retry semantics as :meth:`request` — 401 fails fast,
        429 sleeps the ``RateLimit`` reset value then retries within the
        tenacity budget, 5xx retries, other 4xx propagates.
        """
        payload: dict[str, Any] = {
            "_type": "lead",
            "lead_id": lead_id,
            "text": text,
        }
        if due_date is not None:
            payload["date"] = due_date.isoformat()
        if assigned_to is not None:
            payload["assigned_to"] = assigned_to
        return self.request("POST", "/api/v1/task/", json=payload)

    def post_note(self, lead_id: str, note_text: str) -> dict[str, Any]:
        """POST /api/v1/activity/note/ — append a plain-text Note to a Lead.

        Close's Note activities accept ``{"lead_id": "...", "note": "..."}``
        as the minimal body. The response is the created activity dict
        (carries ``id``, ``date_created``, etc.). Same auth + retry
        semantics as :meth:`request` — 401 fails fast, 429 sleeps the
        ``RateLimit`` reset value then retries within the tenacity
        budget, 5xx retries, other 4xx propagates.

        Callers must build the note text themselves
        (``aegis.close.funder_note.format_funder_note`` is the standard
        producer). This method does NOT validate or shape the body —
        Close enforces its own length / content policy server-side.
        """
        return self.request(
            "POST",
            "/api/v1/activity/note/",
            json={"lead_id": lead_id, "note": note_text},
        )

    def list_recent_notes(self, lead_id: str, limit: int = 5) -> list[CloseNote]:
        """GET /api/v1/activity/note/?lead_id={id}&_limit={limit}.

        Returns the most-recent ``limit`` Note activities for the lead
        as :class:`CloseNote` structs. Feature D — the orchestrator
        concatenates the ``note`` bodies into ``merchants.close_notes_summary``
        so the Bedrock extraction prompt can use the operator's
        narrative as context.

        ``limit`` is clamped to a minimum of 1. The wire layer applies
        the same auth + retry semantics as :meth:`request`.
        """
        safe_limit = max(1, limit)
        page = self.request(
            "GET",
            "/api/v1/activity/note/",
            params={"lead_id": lead_id, "_limit": safe_limit},
        )
        raw_items = page.get("data", [])
        if not isinstance(raw_items, list):
            raise CloseError(
                f"close /api/v1/activity/note/ returned non-list data: {type(raw_items).__name__}"
            )
        items: list[CloseNote] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise CloseError(
                    f"close /api/v1/activity/note/ entry non-object: {type(raw).__name__}"
                )
            items.append(CloseNote.model_validate(raw))
        return items

    def list_recent_calls(self, lead_id: str, limit: int = 3) -> list[CloseCall]:
        """GET /api/v1/activity/call/?lead_id={id}&_limit={limit}.

        Returns the most-recent ``limit`` Call activities for the lead
        as :class:`CloseCall` structs. Feature D — the orchestrator
        concatenates the ``note`` fields (operator-typed post-call
        disposition summaries) into ``merchants.close_call_transcripts``
        for the extraction prompt.

        ``limit`` is clamped to a minimum of 1. Same auth + retry
        semantics as :meth:`request`.
        """
        safe_limit = max(1, limit)
        page = self.request(
            "GET",
            "/api/v1/activity/call/",
            params={"lead_id": lead_id, "_limit": safe_limit},
        )
        raw_items = page.get("data", [])
        if not isinstance(raw_items, list):
            raise CloseError(
                f"close /api/v1/activity/call/ returned non-list data: {type(raw_items).__name__}"
            )
        items: list[CloseCall] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise CloseError(
                    f"close /api/v1/activity/call/ entry non-object: {type(raw).__name__}"
                )
            items.append(CloseCall.model_validate(raw))
        return items

    def list_lead_attachments(self, lead_id: str) -> list[CloseAttachment]:
        """Enumerate PDF attachments across all Note + Email activities
        for the lead.

        Close's API does NOT expose a standalone ``/api/v1/files/?lead_id=…``
        endpoint — the previous implementation hit a 404 on every prod
        merchant. Files live ON activities. We enumerate them via:

        * ``GET /api/v1/activity/note/?lead_id=<lead_id>``
        * ``GET /api/v1/activity/email/?lead_id=<lead_id>``

        For each activity we walk ``attachments[]`` (a list of dicts each
        carrying ``id``, ``filename``, ``content_type``, ``url`` pointing
        at ``app.close.com/go/file/persisted/…``) and emit one
        :class:`CloseAttachment` per ``content_type == "application/pdf"``
        entry. Non-PDFs (driver licenses, voided checks as JPEGs, etc.)
        are dropped silently — the orchestrator only ever wants
        statement PDFs.

        The ``(url, filename)`` pair for every PDF is cached on the
        client instance under the attachment id. :meth:`download_attachment`
        consumes that cache instead of refetching the activity, which
        keeps the worker's per-attachment cost to one network call.

        For Note-attached PDFs, the note's ``pinned`` flag is lifted
        onto :attr:`CloseAttachment.note_pinned` so the orchestrator can
        gate on ``is_pinned OR note_pinned`` — operators most commonly
        pin the wrapping Note in the activity feed rather than the file
        itself in the Files tab.

        Pagination: Close uses ``{"has_more": bool, "data": [...]}``
        envelopes with ``_limit`` + ``_skip``. We follow ``has_more``
        on both endpoints until exhausted. A defensive cap of
        ``_MAX_ACTIVITIES_PER_LEAD`` aborts loops on a misbehaving lead
        rather than spinning forever.

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

        Returns an empty list if neither endpoint has activities with
        PDF attachments.
        """
        items: list[CloseAttachment] = []
        for activity_kind in ("note", "email"):
            items.extend(self._list_activity_pdf_attachments(activity_kind, lead_id))
        return items

    def _list_activity_pdf_attachments(
        self, activity_kind: str, lead_id: str
    ) -> list[CloseAttachment]:
        """Walk one activity endpoint (note or email) and collect every
        PDF attachment as a :class:`CloseAttachment`.

        Populates ``self._attachment_cache`` as a side effect so the
        download path can find the source URL by attachment id.
        """
        items: list[CloseAttachment] = []
        skip = 0
        page_size = 100
        activities_seen = 0
        path = f"/api/v1/activity/{activity_kind}/"
        while True:
            page = self.request(
                "GET",
                path,
                params={
                    # 2026-06-26 regression fix: without ``lead_id`` the
                    # Close activity endpoints return the ORG-WIDE activity
                    # feed, not the per-lead one. Symptom was every lead
                    # returning the identical ~1416 attachments (the org's
                    # first 1000 activities, capped). The sibling
                    # ``_fetch_note_pinned_map`` always passed lead_id; this
                    # path quietly never did.
                    "lead_id": lead_id,
                    "_limit": page_size,
                    "_skip": skip,
                },
            )
            raw_activities = page.get("data", [])
            if not isinstance(raw_activities, list):
                raise CloseError(
                    f"close {path} returned non-list data: {type(raw_activities).__name__}"
                )
            for activity in raw_activities:
                if not isinstance(activity, dict):
                    raise CloseError(
                        f"close {path} returned non-object activity: {type(activity).__name__}"
                    )
                activities_seen += 1
                attachments = activity.get("attachments") or []
                if not isinstance(attachments, list):
                    # Defensive — Close's contract says list, but a
                    # surprise should fail loud rather than silent-skip.
                    raise CloseError(
                        f"close {path} activity attachments non-list: {type(attachments).__name__}"
                    )
                for raw_att in attachments:
                    if not isinstance(raw_att, dict):
                        raise CloseError(
                            f"close {path} attachment entry non-object: {type(raw_att).__name__}"
                        )
                    if raw_att.get("content_type") != "application/pdf":
                        continue
                    attachment = CloseAttachment.model_validate(raw_att)
                    if attachment.url is not None:
                        self._attachment_cache[attachment.id] = (
                            attachment.url,
                            attachment.name,
                        )
                    items.append(attachment)
            if activities_seen >= _MAX_ACTIVITIES_PER_LEAD:
                _log.warning(
                    "close.list_attachments cap_hit lead_id=%s kind=%s activities_seen=%s",
                    lead_id,
                    activity_kind,
                    activities_seen,
                )
                return items
            if not page.get("has_more"):
                break
            skip += page_size

        # Join activity.note.pinned onto note-provenanced files. The
        # operator's natural Close UX is pinning notes in the activity
        # feed (one-click action on the visible row); pinning files in
        # the Files tab is a less-visited UI surface. Reading both
        # signals lets either route confirm "this is a statement."
        # Fixed cost: at most one extra paginated call per lead,
        # regardless of how many note-attached files exist.
        if any(it.last_object_type == "activity.note" for it in items):
            pinned_map = self._fetch_note_pinned_map(lead_id)
            for it in items:
                if it.last_object_type == "activity.note" and it.last_object_id is not None:
                    it.note_pinned = pinned_map.get(it.last_object_id, False)

        return items

    def _fetch_note_pinned_map(self, lead_id: str) -> dict[str, bool]:
        """Return ``{note_id: pinned}`` for every Note on the Lead.

        Used by :meth:`list_lead_attachments` to enrich note-attached
        files with their parent note's pin state. Paginated via the
        same ``_limit`` (capped at 100) / ``_skip`` / ``has_more``
        convention as siblings.

        A non-list ``data`` payload raises CloseError so a future Close
        API shape change fails loud rather than silently dropping pins.
        """
        result: dict[str, bool] = {}
        skip = 0
        page_size = 100
        while True:
            page = self.request(
                "GET",
                "/api/v1/activity/note/",
                params={
                    "lead_id": lead_id,
                    "_limit": page_size,
                    "_skip": skip,
                },
            )
            notes = page.get("data", [])
            if not isinstance(notes, list):
                raise CloseError(
                    f"close /api/v1/activity/note/ returned non-list data: {type(notes).__name__}"
                )
            for n in notes:
                if isinstance(n, dict) and isinstance(n.get("id"), str):
                    result[n["id"]] = bool(n.get("pinned", False))
            if not page.get("has_more"):
                return result
            skip += page_size

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, CloseRateLimitError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    def download_attachment(self, attachment_id: str) -> tuple[bytes, str]:
        """Download a PDF attachment by id and return ``(bytes, filename)``.

        Looks up the attachment's ``(url, filename)`` from the cache
        populated by :meth:`list_lead_attachments`. Rewrites the host
        from ``app.close.com`` → ``api.close.com`` (the only host that
        accepts API-key Basic auth; ``app.close.com`` 400s with
        "use api.close.com"). Follows the resulting 302 to the S3
        signed URL and returns the body.

        Validates the body begins with the ``%PDF-`` magic prefix and
        raises :class:`CloseError` if not — corrupted CDN responses,
        Close edge cases that serve HTML error pages with 200 status,
        and content-type drift all fail loud here rather than silently
        writing junk into the parser pipeline.

        Same auth + retry semantics as :meth:`request`: 401 fails fast
        (wrong key — no retry), 429 sleeps the ``RateLimit`` reset value
        then retries within the tenacity budget, 5xx retries, other 4xx
        propagates immediately. Streams into memory bounded by the
        operator-configured upload cap upstream — the client does not
        enforce that cap itself.

        Cache-miss policy: if the orchestrator calls
        :meth:`download_attachment` for an id that
        :meth:`list_lead_attachments` did not populate (shouldn't happen
        in the current call shape; defensive against future refactors),
        we raise :class:`CloseError` with a clear pointer rather than
        silently 404-ing against a dead endpoint.
        """
        cached = self._attachment_cache.get(attachment_id)
        if cached is None:
            raise CloseError(
                f"close download_attachment cache miss for {attachment_id!r}; "
                "call list_lead_attachments first (Close attachments are "
                "discovered via /activity/{note,email}/, not /files/)"
            )
        source_url, filename = cached
        return self._download_attachment_url(source_url, filename, attachment_id=attachment_id)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, CloseRateLimitError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
        reraise=True,
    )
    def _download_attachment_url(
        self, source_url: str, filename: str, *, attachment_id: str
    ) -> tuple[bytes, str]:
        """GET the swapped-host URL, follow redirects, validate PDF magic.

        Split from :meth:`download_attachment` so the cache lookup +
        error message stay outside the retry loop (no point retrying
        a cache miss) while the network call benefits from the same
        tenacity policy as :meth:`request`.
        """
        settings = get_settings()
        if settings.close_api_key is None:
            raise CloseAuthError(
                "CLOSE_API_KEY is not configured; set it in .env or /etc/aegis/aegis.env"
            )

        url = source_url.replace(_CLOSE_APP_HOST, _CLOSE_API_HOST, 1)
        auth = (settings.close_api_key.get_secret_value(), "")
        try:
            resp = self._http.request("GET", url, auth=auth, follow_redirects=True)
        except httpx.TransportError:
            raise

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
                path=f"/attachment/{attachment_id}",
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
                f"close {resp.status_code} on attachment download: {self._safe_body(resp)}",
                status_code=resp.status_code,
                body=self._safe_body(resp),
            )

        pdf_bytes = resp.content
        if not pdf_bytes.startswith(_PDF_MAGIC):
            raise CloseError(
                "close attachment body is not a PDF "
                f"(attachment_id={attachment_id} head={pdf_bytes[:8]!r})"
            )

        # Prefer the filename carried forward from the activity payload.
        # Fall back to Content-Disposition (defensive — the S3 redirect
        # target normally carries one) then to the benign default.
        resolved_filename = (
            filename
            or _filename_from_content_disposition(resp.headers.get("content-disposition", ""))
            or "unknown.pdf"
        )
        return pdf_bytes, resolved_filename

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

    def _audit_rate_limit(self, *, method: str, path: str, reset_seconds: float) -> None:
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
    "CloseCall",
    "CloseClient",
    "CloseError",
    "CloseNote",
    "CloseRateLimitError",
]
