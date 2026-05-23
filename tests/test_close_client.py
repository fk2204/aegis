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

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
        assert client.request("GET", "/api/v1/me/") == {"ok": True}

    assert seen["auth"] == _expected_basic_auth(), (
        "client did not send HTTP Basic with api_key:''"
    )


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

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
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
    monkeypatch.setattr(
        "aegis.close.client.time.sleep", lambda s: sleeps.append(s)
    )

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

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
        assert client.request("GET", "/api/v1/me/") == {"ok": True}

    assert calls["n"] == 2
    assert 3.0 in sleeps, f"expected reset=3 sleep; saw {sleeps}"


def test_429_with_retry_after_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(
        "aegis.close.client.time.sleep", lambda s: sleeps.append(s)
    )

    calls = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429, headers={"Retry-After": "7"}, text="slow down"
            )
        return httpx.Response(200, json={"ok": True})

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
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

    rate_limit_audits = [
        e for e in audit.entries if e["action"] == "close.rate_limit_hit"
    ]
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

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
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

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
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

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
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

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
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
        return httpx.Response(
            200, json={"id": "lead_abc", "display_name": "Acme"}
        )

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
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

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
        client.update_lead_custom_fields(
            "lead_abc", {"custom.cf_aegis_score": 80}
        )
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

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
        client.get_opportunity("oppo_abc")
    assert seen["method"] == "GET"
    assert seen["url"].endswith("/api/v1/opportunity/oppo_abc/")


def test_download_attachment_returns_bytes_and_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/api/v1/files/att_xyz/download/")
        return httpx.Response(
            200,
            content=b"%PDF-1.7 fake",
            headers={
                "content-disposition": 'attachment; filename="bank_stmt.pdf"',
            },
        )

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
        data, filename = client.download_attachment("att_xyz")
    assert data == b"%PDF-1.7 fake"
    assert filename == "bank_stmt.pdf"


def test_download_attachment_filename_fallback_when_no_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close sometimes omits Content-Disposition. We fall back to a
    benign default so the caller always gets a usable filename."""
    _set_close_env(monkeypatch)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF")

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
        data, filename = client.download_attachment("att_xyz")
    assert data == b"%PDF"
    assert filename == "unknown.pdf"


def test_download_attachment_retries_on_500_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per step 7 spec: download_attachment now wraps the retry decorator
    (it previously did not). 500 should retry."""
    _set_close_env(monkeypatch)
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)

    calls = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(500, text="server boom")
        return httpx.Response(
            200,
            content=b"%PDF",
            headers={"content-disposition": 'attachment; filename="x.pdf"'},
        )

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
        data, filename = client.download_attachment("att_xyz")
    assert calls["n"] == 2
    assert data == b"%PDF"
    assert filename == "x.pdf"


def test_download_attachment_401_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    with CloseClient(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    ) as client:
        with pytest.raises(CloseAuthError):
            client.download_attachment("att_xyz")


# ----------------------------------------------------------------------
# Context manager / close
# ----------------------------------------------------------------------


def test_context_manager_closes_underlying_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)
    http = httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"ok": True})
        )
    )
    with CloseClient(http_client=http) as client:
        client.request("GET", "/api/v1/me/")
    assert http.is_closed, "underlying httpx.Client was not closed"


# ----------------------------------------------------------------------
# Live integration — runs only when CLOSE_API_KEY is set
# ----------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("CLOSE_API_KEY"),
    reason=(
        "set CLOSE_API_KEY in the env to run live Close API test "
        "(hits api.close.com)"
    ),
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
    assert "id" in body and body["id"].startswith("user_"), (
        f"unexpected /me body: {body!r}"
    )
    assert "email" in body and "@" in body["email"]
