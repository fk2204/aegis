"""Tests for the activity-based attachment enumeration + download flow.

Close's API does not expose ``/api/v1/files/?lead_id=…`` — files live
ON Note and Email activities as ``attachments[]`` entries. The original
``CloseClient.list_lead_attachments`` 404'd every prod merchant; this
module covers the fixed activity-based shape end to end:

* ``list_lead_attachments`` walks ``/api/v1/activity/note/`` and
  ``/api/v1/activity/email/`` for the lead, filters to ``content_type
  == "application/pdf"``, and caches each PDF's source URL keyed by
  attachment id.
* ``download_attachment`` looks up the cached URL, rewrites
  ``app.close.com`` → ``api.close.com``, follows Close's 302 to the S3
  signed URL, and validates the PDF magic prefix.

Fixture ``fixtures/acti_note_with_pdf.json`` mirrors the real Close
payload shape captured from a live A&R KM LLC pull on 2026-06-05.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from aegis.close.client import (
    CloseAttachment,
    CloseAuthError,
    CloseClient,
    CloseError,
)
from aegis.config import get_settings

_TEST_KEY = "api_test_close_key"
_BASE = "https://api.close.example"

_FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _set_close_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLOSE_API_KEY", _TEST_KEY)
    monkeypatch.setenv("CLOSE_API_BASE", _BASE)
    get_settings.cache_clear()


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture by filename from tests/close/fixtures/."""
    parsed = json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError(f"fixture {name} is not a JSON object")
    return parsed


def _empty_page() -> dict[str, Any]:
    return {"has_more": False, "data": []}


def _make_notes_emails_transport(
    notes_pages: list[dict[str, Any]],
    emails_pages: list[dict[str, Any]],
) -> tuple[
    list[tuple[str, dict[str, str]]],
    httpx.MockTransport,
]:
    """Build a MockTransport that serves note + email pages in order
    and records every request URL + params seen.

    Returns ``(requests, transport)`` so individual tests can assert on
    which endpoint was hit, in which order, with which ``_skip`` cursor.
    """
    notes_iter = iter(notes_pages)
    emails_iter = iter(emails_pages)
    notes_exhausted = _empty_page()
    emails_exhausted = _empty_page()
    requests: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = {k: v for k, v in request.url.params.items()}
        requests.append((request.url.path, params))
        if "/activity/note/" in request.url.path:
            return httpx.Response(200, json=next(notes_iter, notes_exhausted))
        if "/activity/email/" in request.url.path:
            return httpx.Response(200, json=next(emails_iter, emails_exhausted))
        raise AssertionError(f"unexpected request path: {request.url.path}")

    return requests, httpx.MockTransport(handler)


# ---------------------------------------------------------------------
# list_lead_attachments — enumeration shape
# ---------------------------------------------------------------------


def test_list_lead_attachments_walks_notes_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notes carrying a PDF surface as CloseAttachment with the activity
    attachment's id, filename, and url propagated through."""
    _set_close_env(monkeypatch)
    notes_page = {
        "has_more": False,
        "data": [
            {
                "id": "acti_note_1",
                "attachments": [
                    {
                        "id": "attc_1",
                        "url": (
                            "https://app.close.com/go/file/persisted/2026/05/abc/Lili_2026-02.pdf"
                        ),
                        "filename": "Lili 2026-02.pdf",
                        "content_type": "application/pdf",
                    }
                ],
            }
        ],
    }
    requests, transport = _make_notes_emails_transport(notes_pages=[notes_page], emails_pages=[])

    with CloseClient(http_client=httpx.Client(transport=transport)) as client:
        items = client.list_lead_attachments("lead_abc")

    assert any(p == "/api/v1/activity/note/" for p, _ in requests)
    assert any(p == "/api/v1/activity/email/" for p, _ in requests)
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, CloseAttachment)
    assert item.id == "attc_1"
    assert item.name == "Lili 2026-02.pdf"
    assert item.url is not None
    assert item.url.startswith("https://app.close.com/")


def test_list_lead_attachments_walks_emails_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emails with PDF attachments surface the same way notes do."""
    _set_close_env(monkeypatch)
    emails_page = {
        "has_more": False,
        "data": [
            {
                "id": "acti_email_1",
                "attachments": [
                    {
                        "id": "attc_email_pdf",
                        "url": (
                            "https://app.close.com/go/file/persisted/2026/05/email/Statement.pdf"
                        ),
                        "filename": "Statement.pdf",
                        "content_type": "application/pdf",
                    }
                ],
            }
        ],
    }
    _, transport = _make_notes_emails_transport(notes_pages=[], emails_pages=[emails_page])

    with CloseClient(http_client=httpx.Client(transport=transport)) as client:
        items = client.list_lead_attachments("lead_abc")

    assert len(items) == 1
    assert items[0].id == "attc_email_pdf"
    assert items[0].name == "Statement.pdf"


def test_list_lead_attachments_filters_non_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attachment with content_type != application/pdf is dropped
    silently — driver licenses (image/png), voided checks (image/jpeg),
    etc. are never of interest to the parser pipeline.

    Inline payload here (not the real-shape fixture) so the assertion
    isolates the content-type filter behavior from any other shape
    variance. The real-shape fixture is exercised in
    ``test_list_lead_attachments_handles_real_note_shape``.
    """
    _set_close_env(monkeypatch)
    page = {
        "has_more": False,
        "data": [
            {
                "id": "acti_mixed",
                "attachments": [
                    {
                        "url": "https://app.close.com/go/file/p/keep.pdf",
                        "filename": "keep.pdf",
                        "content_type": "application/pdf",
                    },
                    {
                        "url": "https://app.close.com/go/file/p/drop.png",
                        "filename": "drop.png",
                        "content_type": "image/png",
                    },
                ],
            }
        ],
    }
    _, transport = _make_notes_emails_transport(notes_pages=[page], emails_pages=[])

    with CloseClient(http_client=httpx.Client(transport=transport)) as client:
        items = client.list_lead_attachments("lead_test")

    assert len(items) == 1
    assert items[0].name == "keep.pdf"
    assert items[0].content_type == "application/pdf"


def test_list_lead_attachments_paginates_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """has_more=True triggers a second GET with _skip advanced by the
    page size — pages are merged into one list."""
    _set_close_env(monkeypatch)
    page_one = {
        "has_more": True,
        "data": [
            {
                "id": "acti_note_p1",
                "attachments": [
                    {
                        "id": "attc_p1",
                        "url": "https://app.close.com/go/file/persisted/p1.pdf",
                        "filename": "p1.pdf",
                        "content_type": "application/pdf",
                    }
                ],
            }
        ],
    }
    page_two = {
        "has_more": False,
        "data": [
            {
                "id": "acti_note_p2",
                "attachments": [
                    {
                        "id": "attc_p2",
                        "url": "https://app.close.com/go/file/persisted/p2.pdf",
                        "filename": "p2.pdf",
                        "content_type": "application/pdf",
                    }
                ],
            }
        ],
    }
    requests, transport = _make_notes_emails_transport(
        notes_pages=[page_one, page_two], emails_pages=[]
    )

    with CloseClient(http_client=httpx.Client(transport=transport)) as client:
        items = client.list_lead_attachments("lead_big")

    notes_calls = [params for path, params in requests if path == "/api/v1/activity/note/"]
    assert [p["_skip"] for p in notes_calls] == ["0", "100"]
    assert [it.id for it in items] == ["attc_p1", "attc_p2"]


def test_list_lead_attachments_passes_lead_id_to_activity_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression sentinel for the 2026-06-26 lead_id-scoping bug.

    The ``_list_activity_pdf_attachments`` walk previously omitted
    ``lead_id`` from the query string on ``/api/v1/activity/note/``
    and ``/api/v1/activity/email/``. Close's API returns the ENTIRE
    org-wide activity feed when the param is missing, so every lead
    got the same ~1416 cap-bound attachment set.

    This test asserts every request to either endpoint carries
    ``lead_id`` equal to the lead we asked about. Failure here is
    Bug 1 silently reappearing.
    """
    _set_close_env(monkeypatch)
    notes_page = {
        "has_more": False,
        "data": [
            {
                "id": "acti_n",
                "attachments": [
                    {
                        "id": "attc_1",
                        "url": "https://app.close.com/go/file/persisted/x.pdf",
                        "filename": "x.pdf",
                        "content_type": "application/pdf",
                    }
                ],
            }
        ],
    }
    emails_page = {"has_more": False, "data": []}
    requests, transport = _make_notes_emails_transport(
        notes_pages=[notes_page], emails_pages=[emails_page]
    )

    with CloseClient(http_client=httpx.Client(transport=transport)) as client:
        client.list_lead_attachments("lead_specific_xyz")

    activity_calls = [
        (path, params) for path, params in requests if path.startswith("/api/v1/activity/")
    ]
    assert activity_calls, "no activity-endpoint calls observed"
    for path, params in activity_calls:
        assert params.get("lead_id") == "lead_specific_xyz", (
            f"Bug 1 regression: {path} missing lead_id (got params={params!r})"
        )


def test_list_lead_attachments_activity_without_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An activity with ``attachments: []`` yields nothing — no error,
    no synthetic entry."""
    _set_close_env(monkeypatch)
    page = {
        "has_more": False,
        "data": [{"id": "acti_blank", "attachments": []}],
    }
    _, transport = _make_notes_emails_transport(notes_pages=[page], emails_pages=[])

    with CloseClient(http_client=httpx.Client(transport=transport)) as client:
        items = client.list_lead_attachments("lead_x")
    assert items == []


def test_list_lead_attachments_empty_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both endpoints empty → empty list. Confirms both endpoints are
    hit even when the first returns nothing."""
    _set_close_env(monkeypatch)
    requests, transport = _make_notes_emails_transport(notes_pages=[], emails_pages=[])

    with CloseClient(http_client=httpx.Client(transport=transport)) as client:
        items = client.list_lead_attachments("lead_zero")

    assert items == []
    paths_hit = {path for path, _ in requests}
    assert "/api/v1/activity/note/" in paths_hit
    assert "/api/v1/activity/email/" in paths_hit


def test_list_lead_attachments_401_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_close_env(monkeypatch)

    def transport(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        with pytest.raises(CloseAuthError):
            client.list_lead_attachments("lead_abc")


def test_list_lead_attachments_rejects_non_list_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: a contract violation from Close (data not a list)
    must fail loud, not silent-skip."""
    _set_close_env(monkeypatch)

    def transport(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"has_more": False, "data": {"oops": 1}})

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport))) as client:
        with pytest.raises(CloseError, match="non-list"):
            client.list_lead_attachments("lead_abc")


def test_list_lead_attachments_populates_download_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The (url, filename) for each PDF must be stashed on the client
    so download_attachment can find it without a second API hop.

    The real Close payload has no ``id`` field on attachments, so the
    cache key is the synthesized id = sha256(url)[:16]. We verify the
    cache by re-deriving the key from the URL we put on the wire.
    """
    import hashlib

    _set_close_env(monkeypatch)
    url = "https://app.close.com/go/file/persisted/2026/05/abc/x.pdf"
    page = {
        "has_more": False,
        "data": [
            {
                "id": "acti_cache",
                "attachments": [
                    {
                        "url": url,
                        "filename": "x.pdf",
                        "content_type": "application/pdf",
                    }
                ],
            }
        ],
    }
    _, transport = _make_notes_emails_transport(notes_pages=[page], emails_pages=[])

    synth_id = hashlib.sha256(url.encode()).hexdigest()[:16]
    with CloseClient(http_client=httpx.Client(transport=transport)) as client:
        client.list_lead_attachments("lead_test")
        cached = client._attachment_cache.get(synth_id)

    assert cached is not None, (
        f"cache miss on synthesized id {synth_id!r}; keys={list(client._attachment_cache)}"
    )
    cached_url, cached_filename = cached
    assert cached_url == url
    assert cached_filename == "x.pdf"


# ---------------------------------------------------------------------
# download_attachment — host rewrite + redirect + magic-bytes validation
# ---------------------------------------------------------------------


def test_download_attachment_swaps_host_and_returns_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The download path must rewrite app.close.com → api.close.com on
    the source URL. The mock transport receives the rewritten host."""
    _set_close_env(monkeypatch)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["path"] = request.url.path
        return httpx.Response(200, content=b"%PDF-1.7 contents")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        client._attachment_cache["attc_x"] = (
            "https://app.close.com/go/file/persisted/2026/05/abc/x.pdf",
            "x.pdf",
        )
        data, filename = client.download_attachment("attc_x")

    assert seen["host"] == "api.close.com"
    assert seen["path"] == "/go/file/persisted/2026/05/abc/x.pdf"
    assert data == b"%PDF-1.7 contents"
    assert filename == "x.pdf"


def test_download_attachment_follows_redirect_to_s3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close 302s to an S3 signed URL. httpx with follow_redirects=True
    must land on the final body, which the client validates as a PDF."""
    _set_close_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.close.com":
            return httpx.Response(
                302,
                headers={
                    "location": "https://s3.example/signed/bank_stmt.pdf?sig=z",
                },
            )
        if request.url.host == "s3.example":
            return httpx.Response(200, content=b"%PDF-1.7 final body")
        raise AssertionError(f"unexpected host {request.url.host}")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        client._attachment_cache["attc_x"] = (
            "https://app.close.com/go/file/persisted/abc/bank_stmt.pdf",
            "bank_stmt.pdf",
        )
        data, filename = client.download_attachment("attc_x")

    assert data == b"%PDF-1.7 final body"
    assert filename == "bank_stmt.pdf"


def test_download_attachment_rejects_non_pdf_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 with an HTML body (CDN error page, mis-routed S3 object)
    must raise CloseError instead of returning garbage."""
    _set_close_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops</html>")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        client._attachment_cache["attc_x"] = (
            "https://app.close.com/go/file/persisted/abc/wrong.pdf",
            "wrong.pdf",
        )
        with pytest.raises(CloseError, match="not a PDF"):
            client.download_attachment("attc_x")


def test_download_attachment_cache_miss_raises_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cache entry → CloseError that points at the listing contract.
    The transport is never touched; verifies no fallback network call."""
    _set_close_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call should happen on cache miss")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        with pytest.raises(CloseError, match="cache miss"):
            client.download_attachment("attc_unknown")


def test_download_attachment_filename_falls_back_to_unknown_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive fallback: if the cache somehow holds an empty filename
    and the response carries no Content-Disposition either, we still
    return a usable name rather than ``""``."""
    _set_close_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.7 ok")

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        client._attachment_cache["attc_x"] = (
            "https://app.close.com/go/file/persisted/abc/x.pdf",
            "",
        )
        _, filename = client.download_attachment("attc_x")

    assert filename == "unknown.pdf"


def test_download_attachment_end_to_end_via_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration: list → cache → download in one round, the way the
    orchestrator drives the client. Confirms the orchestrator's call
    shape (``download_attachment(att.id)``) still works against the
    REAL Close note-activity payload shape (no ``id`` field on the
    attachment — id is synthesized from the URL)."""
    _set_close_env(monkeypatch)
    notes_page = _load_fixture("acti_note_with_pdf.json")

    pdf_body = b"%PDF-1.7 from-cdn"

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/v1/activity/note/" in request.url.path:
            return httpx.Response(200, json=notes_page)
        if "/api/v1/activity/email/" in request.url.path:
            return httpx.Response(200, json=_empty_page())
        # Anything else is the download path.
        assert request.url.host == "api.close.com"
        return httpx.Response(200, content=pdf_body)

    with CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(handler))) as client:
        items = client.list_lead_attachments("lead_test")
        # Real fixture: act1 has 2 PDFs, act2 has 1 PDF, act3 is empty.
        assert len(items) == 3
        # Drive download against the first item — the way the worker
        # iterates. ``att.id`` is the synthesized hash; download finds
        # the cached URL by it.
        first = items[0]
        assert first.id  # synthesized, non-empty
        data, filename = client.download_attachment(first.id)

    assert data == pdf_body
    assert filename == first.name


def test_list_lead_attachments_handles_real_note_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real Close note attachment payload has NO ``id`` field — the
    earlier rewrite required it and crashed every prod merchant. This
    fixture is the sanitized capture of the live response that broke
    A&R KM LLC (lead_Qny6E…). The model must accept it cleanly and
    synthesize a stable id from the URL.
    """
    _set_close_env(monkeypatch)
    notes_page = _load_fixture("acti_note_with_pdf.json")
    _, transport = _make_notes_emails_transport(notes_pages=[notes_page], emails_pages=[])

    with CloseClient(http_client=httpx.Client(transport=transport)) as client:
        items = client.list_lead_attachments("lead_test")

    # 3 PDFs across the fixture's note activities (2 in act1, 1 in act2,
    # 0 in act3).
    assert len(items) == 3
    # Every synthesized id is non-empty and distinct (URLs differ so
    # hashes do too).
    ids = [it.id for it in items]
    assert all(ids)
    assert len(set(ids)) == 3, f"id collision among {ids!r}"
    # No attachment had an ``id`` key on the wire — all values are the
    # 16-char sha256 prefix.
    for it in items:
        assert len(it.id) == 16
        assert all(c in "0123456789abcdef" for c in it.id)


def test_list_lead_attachments_handles_real_email_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real Close email attachment payload has NO ``id`` field AND
    carries extra keys (``content_id``, ``inline_only``) not present on
    note attachments. Both shapes must validate cleanly through the
    same model (``extra="ignore"`` covers the email-only keys).
    """
    _set_close_env(monkeypatch)
    emails_page = _load_fixture("acti_email_with_pdf.json")
    _, transport = _make_notes_emails_transport(notes_pages=[], emails_pages=[emails_page])

    with CloseClient(http_client=httpx.Client(transport=transport)) as client:
        items = client.list_lead_attachments("lead_test")

    # 2 PDFs on the first email activity, 0 on the second.
    assert len(items) == 2
    for it in items:
        assert len(it.id) == 16
        assert it.url is not None
        assert it.url.startswith("https://app.close.com/")


def test_close_attachment_synthesizes_id_when_wire_omits_it() -> None:
    """Unit test on the model: a payload without ``id`` populates a
    deterministic sha256-derived id. Same URL ⇒ same id (cache key
    stability across worker invocations on the same lead)."""
    import hashlib

    url = "https://app.close.com/go/file/persisted/abc/x.pdf"
    a1 = CloseAttachment.model_validate(
        {"url": url, "filename": "x.pdf", "content_type": "application/pdf"}
    )
    a2 = CloseAttachment.model_validate(
        {"url": url, "filename": "x.pdf", "content_type": "application/pdf"}
    )
    expected = hashlib.sha256(url.encode()).hexdigest()[:16]
    assert a1.id == expected
    assert a1.id == a2.id


def test_close_attachment_requires_id_or_url() -> None:
    """A payload missing BOTH id and url is unsalvageable — fail loud."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CloseAttachment.model_validate({"filename": "x.pdf", "content_type": "application/pdf"})


def test_close_attachment_explicit_id_wins_over_synthesis() -> None:
    """If the wire payload DOES carry an explicit id (legacy fixture
    shape, or a hypothetical future change), it overrides synthesis."""
    a = CloseAttachment.model_validate(
        {
            "id": "explicit_id_value",
            "url": "https://app.close.com/go/file/persisted/abc/x.pdf",
            "filename": "x.pdf",
            "content_type": "application/pdf",
        }
    )
    assert a.id == "explicit_id_value"
