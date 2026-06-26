"""Close CRM client tests.

Unit tests use ``httpx.MockTransport`` — no real HTTP. They cover:

* HTTP Basic auth construction (api key as username, blank password)
* 401 fails fast, never retried
* 429 with ``RateLimit`` header sleeps for the ``reset`` value and is
  retried by tenacity
* 429 with ``Retry-After`` header (no RateLimit) falls back correctly
* 5xx is retried via the same path as 429
* 4xx other than 401/429 propagates immediately as ``CloseError``
* Context-manager and explicit close()
* Typed methods (``get_lead``, ``update_lead_custom_fields``,
  ``get_opportunity``, ``download_attachment``) call the right
  method + path

One integration test gated on ``CLOSE_API_KEY`` env var hits the live
org's ``/api/v1/me/`` endpoint to confirm end-to-end auth.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx
import pytest

from aegis.audit import InMemoryAuditLog
from aegis.close.client import (
    CloseAttachment,
    CloseAuthError,
    CloseClient,
    CloseError,
    CloseRateLimitError,
)
from aegis.config import get_settings

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


_TEST_KEY = "api_test_close_key"
_BASE = "https://api.close.example"


def _set_close_env(monkeypatch: pytest.MonkeyPatch, *, key: str = _TEST_KEY) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", key)
    monkeypatch.setenv("CLOSE_API_BASE", _BASE)
    get_settings.cache_clear()


def _expected_basic_auth(key: str = _TEST_KEY) -> str:
    """The Authorization header value httpx produces for (key, '')."""
    encoded = base64.b64encode(f"{key}:".encode()).decode()
    return f"Basic {encoded}"


# ----------------------------------------------------------------------
# Auth shape
# ----------------------------------------------------------------------


def test_request_sends_basic_auth_key_as_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    seen: dict[str, str] = {}

    def transport(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True})

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        assert client.request("GET", "/api/v1/me/") == {"ok": True}

    assert seen["auth"] == _expected_basic_auth(), "client did not send HTTP Basic with api_key:''"


def test_request_raises_close_auth_error_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mock get_settings directly so the test is independent of whether
    # the dev machine's .env contains CLOSE_API_KEY. pydantic-settings
    # loads env_file values automatically, so delenv + cache_clear is
    # not enough on a machine where the operator's real key is present.
    fake_settings = type(
        "S",
        (),
        {"close_api_key": None, "close_api_base": "https://api.close.example"},
    )()
    monkeypatch.setattr("aegis.close.client.get_settings", lambda: fake_settings)
    with pytest.raises(CloseAuthError, match="CLOSE_API_KEY"):
        with CloseClient() as client:
            client.request("GET", "/api/v1/me/")


# ----------------------------------------------------------------------
# 401 — fails fast, no retry
# ----------------------------------------------------------------------


def test_401_fails_fast_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    calls = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        with pytest.raises(CloseAuthError) as exc:
            client.request("GET", "/api/v1/me/")
    assert calls["n"] == 1, "401 must not be retried"
    assert exc.value.status_code == 401


# ----------------------------------------------------------------------
# 429 — retry, sleep from RateLimit reset
# ----------------------------------------------------------------------


def test_429_with_ratelimit_reset_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={"RateLimit": "limit=20, remaining=0, reset=3"},
                text="slow down",
            )
        return httpx.Response(200, json={"ok": True})

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        assert client.request("GET", "/api/v1/me/") == {"ok": True}

    assert calls["n"] == 2
    assert 3.0 in sleeps, f"expected reset=3 sleep; saw {sleeps}"


def test_429_with_retry_after_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
        return httpx.Response(200, json={"ok": True})

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        client.request("GET", "/api/v1/me/")

    assert 7.0 in sleeps, f"expected Retry-After=7 sleep; saw {sleeps}"


def test_429_audits_when_audit_log_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"RateLimit": "limit=20, remaining=0, reset=1"},
            text="slow down",
        )

    audit = InMemoryAuditLog()
    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
        audit=audit,
    ) as client:
        with pytest.raises(CloseRateLimitError):
            client.request("GET", "/api/v1/me/")

    rate_limit_audits = [e for e in audit.entries if e["action"] == "close.rate_limit_hit"]
    assert rate_limit_audits, "expected a close.rate_limit_hit audit entry"
    assert rate_limit_audits[0]["actor"] == "close_client"
    assert rate_limit_audits[0]["details"]["reset_seconds"] == 1.0


# ----------------------------------------------------------------------
# 5xx — retried via tenacity
# ----------------------------------------------------------------------


def test_500_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(500, text="server boom")
        return httpx.Response(200, json={"ok": True})

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        assert client.request("GET", "/api/v1/me/") == {"ok": True}
    assert calls["n"] == 2


# ----------------------------------------------------------------------
# 4xx other than 401/429 — propagates immediately
# ----------------------------------------------------------------------


def test_400_propagates_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    calls = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        with pytest.raises(CloseError) as exc:
            client.request("GET", "/api/v1/me/")
    assert calls["n"] == 1, "400 must not be retried"
    assert exc.value.status_code == 400
    # Not a CloseAuthError, not a CloseRateLimitError — plain CloseError.
    assert not isinstance(exc.value, CloseAuthError)
    assert not isinstance(exc.value, CloseRateLimitError)


def test_404_propagates_with_status_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        with pytest.raises(CloseError) as exc:
            client.get_lead("lead_does_not_exist")
    assert exc.value.status_code == 404


# ----------------------------------------------------------------------
# Body sanitization
# ----------------------------------------------------------------------


def test_long_response_body_is_truncated_in_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    long_body = "x" * 5000

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=long_body)

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        with pytest.raises(CloseError) as exc:
            client.request("GET", "/api/v1/me/")
    # Truncated to ~500 chars + ellipsis. Comfortably under the raw 5000.
    assert exc.value.body is not None
    assert len(exc.value.body) < 600


# ----------------------------------------------------------------------
# Typed methods route to the right path + method
# ----------------------------------------------------------------------


def test_get_lead_hits_correct_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    seen: dict[str, str] = {}

    def transport(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "lead_abc", "display_name": "Acme"})

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        result = client.get_lead("lead_abc")
    assert result["id"] == "lead_abc"
    assert seen["method"] == "GET"
    assert seen["url"].endswith("/api/v1/lead/lead_abc/")


def test_update_lead_custom_fields_puts_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    seen: dict[str, Any] = {}

    def transport(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"id": "lead_abc"})

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        client.update_lead_custom_fields("lead_abc", {"custom.cf_aegis_score": 80})
    assert seen["method"] == "PUT"
    assert seen["url"].endswith("/api/v1/lead/lead_abc/")
    assert b"cf_aegis_score" in seen["body"]


def test_get_opportunity_hits_correct_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    seen: dict[str, str] = {}

    def transport(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"id": "oppo_abc"})

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        client.get_opportunity("oppo_abc")
    assert seen["method"] == "GET"
    assert seen["url"].endswith("/api/v1/opportunity/oppo_abc/")


def _seed_attachment_cache(
    client: CloseClient, attachment_id: str, *, url: str, filename: str
) -> None:
    """Test helper: populate the URL cache that ``download_attachment``
    consumes without going through ``list_lead_attachments``.

    Mirrors the side effect ``list_lead_attachments`` would have had if
    the orchestrator had just enumerated the lead — used by the download
    tests below so each one stays focused on one concern.
    """
    client._attachment_cache[attachment_id] = (url, filename)


def test_download_attachment_returns_bytes_and_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persisted URL always points at app.close.com; api auth fails
    there. The download path must rewrite app.close.com → api.close.com
    before issuing the GET."""
    _set_close_env(monkeypatch)
    seen: dict[str, str] = {}

    def transport(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["path"] = request.url.path
        return httpx.Response(200, content=b"%PDF-1.7 fake")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        _seed_attachment_cache(
            client,
            "att_xyz",
            url="https://app.close.com/go/file/persisted/abc123",
            filename="bank_stmt.pdf",
        )
        data, filename = client.download_attachment("att_xyz")
    assert data == b"%PDF-1.7 fake"
    assert filename == "bank_stmt.pdf"
    # Host swap: app.close.com → api.close.com.
    assert seen["host"] == "api.close.com"
    assert seen["path"] == "/go/file/persisted/abc123"


def test_download_attachment_rejects_non_pdf_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 with an HTML error page (or any non-PDF body) must fail
    loud rather than silently feeding garbage into the parser."""
    _set_close_env(monkeypatch)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not a pdf</html>")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        _seed_attachment_cache(
            client,
            "att_xyz",
            url="https://app.close.com/go/file/persisted/abc",
            filename="bank_stmt.pdf",
        )
        with pytest.raises(CloseError, match="not a PDF"):
            client.download_attachment("att_xyz")


def test_download_attachment_cache_miss_raises_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the orchestrator calls download_attachment for an id that
    list_lead_attachments never populated, raise a clear error pointing
    at the new contract rather than silently 404ing."""
    _set_close_env(monkeypatch)

    def transport(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call should happen on cache miss")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        with pytest.raises(CloseError, match="cache miss"):
            client.download_attachment("att_unknown")


def test_download_attachment_retries_on_500_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """500 from the api.close.com download must retry via tenacity."""
    _set_close_env(monkeypatch)
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)

    calls = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(500, text="server boom")
        return httpx.Response(200, content=b"%PDF-1.7 fake")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        _seed_attachment_cache(
            client,
            "att_xyz",
            url="https://app.close.com/go/file/persisted/abc",
            filename="x.pdf",
        )
        data, filename = client.download_attachment("att_xyz")
    assert calls["n"] == 2
    assert data == b"%PDF-1.7 fake"
    assert filename == "x.pdf"


def test_download_attachment_401_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        _seed_attachment_cache(
            client,
            "att_xyz",
            url="https://app.close.com/go/file/persisted/abc",
            filename="bank_stmt.pdf",
        )
        with pytest.raises(CloseAuthError):
            client.download_attachment("att_xyz")


# ----------------------------------------------------------------------
# CloseAttachment model — field defaults and parsing
# ----------------------------------------------------------------------


def test_close_attachment_defaults_new_fields_to_none_or_false() -> None:
    """The chunk-1 minimal shape (id+name) still parses; the new fields
    introduced by the note-attachments fix default to None / False."""
    a = CloseAttachment(id="leadfile_x", name="x.pdf")
    assert a.checksum is None
    assert a.download_url is None
    assert a.is_pinned is False
    assert a.last_object_type is None
    assert a.last_object_id is None


def test_close_attachment_accepts_unified_lead_files_shape() -> None:
    """Mirror of the real ``/api/v1/lead/{id}/files/`` response shape so
    a future Close-API addition (extra unknown keys) doesn't break the
    model. ``extra='ignore'`` is the contract — keys we don't read are
    silently dropped, not raised on."""
    payload = {
        "id": "leadfile_0b4YISG3WcbhVfVCxgOT4d",
        "name": "Screenshot (349).png",
        "content_type": "image/png",
        "size": 278585,
        "checksum": "835b9dc89efa2a1fba2223497a773426",
        "download_url": (
            "https://app.close.com/go/file/persisted/orga_xyz/"
            "activity.note/acti_xyz/token/Screenshot(349).png/"
        ),
        "thumbnail_url": "https://app.close.com/.../thumbnail/",
        "is_pinned": False,
        "last_object_type": "activity.note",
        "last_object_id": "acti_xyz",
        "lead_id": "lead_xyz",
        "organization_id": "orga_xyz",
        "date_created": "2026-05-28T04:29:07",
        "date_updated": "2026-05-28T04:29:07",
        "last_activity_at": "2026-05-28T04:29:06",
        "updated_by": None,
    }
    a = CloseAttachment.model_validate(payload)
    assert a.id == "leadfile_0b4YISG3WcbhVfVCxgOT4d"
    assert a.name == "Screenshot (349).png"
    assert a.content_type == "image/png"
    assert a.size == 278585
    assert a.checksum == "835b9dc89efa2a1fba2223497a773426"
    assert a.download_url is not None and "activity.note" in a.download_url
    assert a.is_pinned is False
    assert a.last_object_type == "activity.note"
    assert a.last_object_id == "acti_xyz"


def test_close_attachment_provenance_lead_direct() -> None:
    """Files attached directly to the Lead (no activity wrapper) carry
    ``last_object_type='lead'``. Verified against
    lead_FbO7xqlUiNII1Mgp9ihQUPkfMbOsOJiJmu2vDrCDSJo on 2026-05-28."""
    payload = {
        "id": "leadfile_2UYA5WkbZ8xEFw3gPIUo7z",
        "name": "Screenshot (344).png",
        "content_type": "image/png",
        "size": 303379,
        "checksum": "8d425106552463a6accbec15fb7c7365",
        "download_url": (
            "https://app.close.com/go/file/persisted/orga_xyz/"
            "lead/lead_FbO7/token/Screenshot(344).png/"
        ),
        "is_pinned": False,
        "last_object_type": "lead",
        "last_object_id": "lead_FbO7xqlUiNII1Mgp9ihQUPkfMbOsOJiJmu2vDrCDSJo",
    }
    a = CloseAttachment.model_validate(payload)
    assert a.last_object_type == "lead"
    assert a.last_object_id == payload["last_object_id"]


def test_close_attachment_is_pinned_true_parses() -> None:
    """The pin gate reads this field — confirm True flows through cleanly."""
    a = CloseAttachment(
        id="leadfile_x",
        name="stmt.pdf",
        content_type="application/pdf",
        is_pinned=True,
    )
    assert a.is_pinned is True


def test_close_attachment_note_pinned_defaults_false() -> None:
    """note_pinned defaults to False — set post-hoc by list_lead_attachments
    when joining activity.note.pinned for note-provenanced files."""
    a = CloseAttachment(id="leadfile_x", name="stmt.pdf")
    assert a.note_pinned is False


def test_close_attachment_note_pinned_can_be_set() -> None:
    """The list_lead_attachments join mutates note_pinned in place after
    model_validate. Confirm the field accepts True."""
    a = CloseAttachment(
        id="leadfile_x",
        name="stmt.pdf",
        content_type="application/pdf",
        last_object_type="activity.note",
        last_object_id="acti_xyz",
    )
    a.note_pinned = True
    assert a.note_pinned is True


# list_lead_attachments coverage now lives under
# tests/close/test_client_attachments.py — the new activity-based shape
# (Close API exposes attachments on Note/Email activities, not via
# /api/v1/files/) has its own dedicated test module to keep the
# enumeration + download fixtures co-located.


# ----------------------------------------------------------------------
# Context manager / close
# ----------------------------------------------------------------------


def test_context_manager_closes_underlying_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    http = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    )
    with CloseClient(http_client=http) as client:
        client.request("GET", "/api/v1/me/")
    assert http.is_closed, "underlying httpx.Client was not closed"


# ----------------------------------------------------------------------
# Live integration — runs only when CLOSE_API_KEY is set
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("CLOSE_API_KEY"),
    reason=("set CLOSE_API_KEY in the env to run live Close API test (hits api.close.com)"),
)
def test_live_me_endpoint_authenticates() -> None:
    """One harmless GET against the live Close org to verify end-to-end
    auth via the real CloseClient. /api/v1/me/ returns the authenticated
    user — no side effects.

    Uses CloseClient (not raw httpx) so the truststore TLS context and
    the retry/error pipeline are exercised, not just the auth header.
    The conftest sets AEGIS_DATA_RESIDENCY_CONFIRMED=true; pytest's
    subprocess invoker is responsible for putting CLOSE_API_KEY in
    os.environ before this test runs (see the @skipif).
    """
    # Force a fresh settings read so CLOSE_API_KEY in os.environ takes
    # effect even if get_settings was cached during conftest setup.
    get_settings.cache_clear()
    with CloseClient() as client:
        body = client.request("GET", "/api/v1/me/")
    assert "id" in body and body["id"].startswith("user_"), f"unexpected /me body: {body!r}"
    assert "email" in body and "@" in body["email"]
