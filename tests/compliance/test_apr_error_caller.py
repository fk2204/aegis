"""R0.4 caller plumbing — APRDisclosureError handling in the route layer.

R0.4 added ``APRDisclosureError`` so the disclosure-context builder raises
on brentq APR-computation failure instead of silently rendering 0.00%.
The class is correct; what was missing was the caller plumbing — the
production route had no try/except around ``render_disclosure``, so an
APR failure would surface as a 500 from FastAPI's generic exception
handler instead of a structured "held for operator review" response.

These tests pin the caller behavior:

  * JSON route (``POST /disclosures/render``) catches the error, writes
    one audit_log row with non-PII details, and returns 503 with a
    structured detail (``code=apr_compute_failed``,
    ``disclosure_status=needs_review``).
  * HTML route (``POST /disclosures/render.html``) returns the
    operator-facing error page at 503 with
    ``X-Disclosure-Status: needs_review`` and NEVER returns a disclosure
    HTML containing a 0.00% APR string.
  * The audit ``details`` JSONB carries deal_id + numeric inputs only —
    no business_name, owner_name, or transaction_description leakage.
  * Happy path still works end-to-end (regression guard).

Scope note: AEGIS is internal pre-flight per
``.claude/rules/compliance.md``; the funder owns regulator-facing
issuance. These tests verify the internal preview/audit-prep flow
fails closed, NOT that a regulator-facing disclosure is suppressed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_funder_repository,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.compliance import disclosure_context
from aegis.compliance.apr import APRCalculationError
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository

AUTH = {"Authorization": "Bearer test-token-not-real"}

# Deterministic merchant id so the audit-log subject_id assertion is
# stable across runs. The CA fixture below carries this UUID.
_FIXED_MERCHANT_ID = "22222222-2222-4222-8222-222222222222"

# A merchant/owner name + transaction-description-shaped string that
# would be a PII leak if any of these substrings showed up in the audit
# row's ``details``. Used as a canary in the PII-safety assertion.
_PII_BUSINESS = "Broken APR Bakery LLC"
_PII_OWNER = "Test Owner Pii Canary"


@pytest.fixture
def audit_log() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def client(audit_log: InMemoryAuditLog) -> Iterator[TestClient]:
    """TestClient with in-memory deps; audit log shared with the test."""
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = InMemoryMerchantRepository
    app.dependency_overrides[get_funder_repository] = InMemoryFunderRepository
    app.dependency_overrides[get_repository] = InMemoryDocumentRepository
    app.dependency_overrides[get_audit] = lambda: audit_log
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _deal_payload(state: str = "CA") -> dict[str, Any]:
    """A ScoreInput dict that passes Pydantic for CA. The APR failure is
    forced via ``monkeypatch`` on ``calculate_apr``, not by violating the
    ScoreInput constraints (factor > 1, term_days >= 1)."""
    return {
        "merchant_id": _FIXED_MERCHANT_ID,
        "business_name": _PII_BUSINESS,
        "owner_name": _PII_OWNER,
        "state": state,
        "avg_daily_balance": "12500.00",
        "true_revenue": "110000.00",
        "monthly_revenue": "110000.00",
        "lowest_balance": "3000.00",
        "num_nsf": 0,
        "days_negative": 0,
        "mca_positions": 0,
        "mca_daily_total": "0.00",
        "debt_to_revenue": "0.00",
        "fraud_score": 10,
        "statement_period_start": "2026-04-01",
        "statement_period_end": "2026-04-30",
        "statement_days": 30,
        "requested_amount": "50000.00",
        "requested_factor": "1.30",
        "requested_term_days": 120,
    }


def _score_payload() -> dict[str, Any]:
    return {
        "score": 70,
        "tier": "B",
        "recommendation": "approve",
        "suggested_max_advance": "50000.00",
        "recommended_factor_rate": "1.30",
        "recommended_holdback_pct": "0.12",
        "estimated_payback_days": 120,
    }


def _force_apr_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``calculate_apr`` with one that always raises.

    Patches the binding inside ``disclosure_context`` (the module that
    calls it) so the production catch path inside
    ``_build_common_tier1_fields`` fires and converts to
    ``APRDisclosureError``.
    """

    def _boom(*args: object, **kwargs: object) -> Decimal:
        raise APRCalculationError("brentq failed to converge")

    monkeypatch.setattr(disclosure_context, "calculate_apr", _boom)


# --- JSON route --------------------------------------------------------------


def test_render_json_returns_503_with_needs_review_on_apr_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route catches APRDisclosureError → 503 with structured detail,
    NOT a 500. The detail carries the operator-actionable disclosure
    status so the dashboard can render a held-for-review badge."""
    _force_apr_failure(monkeypatch)
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}

    resp = client.post("/disclosures/render", json=body, headers=AUTH)

    assert resp.status_code == 503, resp.text
    payload = resp.json()
    detail = payload["detail"]
    assert detail["code"] == "apr_compute_failed"
    assert detail["disclosure_status"] == "needs_review"
    assert detail["state"] == "CA"
    assert detail["term_days"] == 120


def test_render_json_does_not_return_zero_percent_apr_disclosure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-braces against the silent 0.00% regression. If the route
    ever stops catching APRDisclosureError and falls back to the
    pre-R0.4 behavior (silent zero APR), the response body would carry
    a rendered disclosure with "0.00%" in it. This must never happen."""
    _force_apr_failure(monkeypatch)
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}

    resp = client.post("/disclosures/render", json=body, headers=AUTH)

    # 503, no rendered HTML, no 0.00% APR string anywhere in the response.
    assert resp.status_code == 503
    assert "0.00%" not in resp.text
    # The wrapper response model is never returned on the failure path.
    assert "rendered" not in resp.json()


# --- HTML route --------------------------------------------------------------


def test_render_html_returns_error_page_on_apr_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTML route returns the operator-facing needs_review page at
    503 with the ``X-Disclosure-Status`` header set. It does NOT return
    a rendered disclosure HTML with a missing/zero APR."""
    _force_apr_failure(monkeypatch)
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}

    resp = client.post("/disclosures/render.html", json=body, headers=AUTH)

    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["x-disclosure-status"] == "needs_review"
    assert "needs_review" in resp.text
    # Critical: the error page must not impersonate a disclosure with
    # a 0.00% APR string. The R0.4 audit gate exists to prevent this.
    assert "0.00%" not in resp.text


# --- Audit-log assertion -----------------------------------------------------


def test_apr_failure_writes_exactly_one_audit_log_row_with_non_pii_details(
    client: TestClient,
    audit_log: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When APRDisclosureError is caught the route MUST write exactly
    one audit_log row with ``action='aegis_apr_compute_failed'``,
    ``subject_type='deal'``, the deal-id-ish reference, and the numeric
    inputs. PII (business_name, owner_name, transaction descriptions)
    MUST NOT appear in ``details``."""
    _force_apr_failure(monkeypatch)
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}

    resp = client.post("/disclosures/render", json=body, headers=AUTH)
    assert resp.status_code == 503

    # Exactly one row, with the expected action + subject shape.
    rows = [
        e for e in audit_log.entries if e["action"] == "aegis_apr_compute_failed"
    ]
    assert len(rows) == 1, audit_log.entries
    row = rows[0]
    assert row["subject_type"] == "deal"
    # subject_id is the raw UUID (Audit row carries it as a string, not
    # through the PII masker — the masker only applies to details).
    assert row["subject_id"] == _FIXED_MERCHANT_ID

    details = row["details"]
    # The PII masker scrubs bare 9-16 digit runs from string values, so
    # the last segment of the UUID is masked to "***". This is the
    # expected belt-and-braces behavior — the row is still queryable
    # by subject_id (which is the unmasked UUID column).
    assert details["deal_id"].startswith("22222222-2222-4222-8222-")
    assert details["state"] == "CA"
    # 50000.00 is 7 chars including the dot — under the bare-digits regex
    # threshold (9), so the principal string is NOT masked.
    assert details["principal"] == "50000.00"
    assert details["factor"] == "1.30"
    assert details["term_days"] == 120
    # disbursement_date is set by _build_common_tier1_fields to rendered_at
    # when no explicit date was passed; just assert it's an ISO date string.
    assert isinstance(details["disbursement_date"], str)
    date.fromisoformat(details["disbursement_date"])  # ParseError if not ISO

    # PII canary: details must not carry business / owner names.
    flat = repr(details)
    assert _PII_BUSINESS not in flat
    assert _PII_OWNER not in flat
    # And no unexpected keys that would indicate someone bolted PII on later.
    expected_keys = {
        "deal_id",
        "state",
        "principal",
        "factor",
        "term_days",
        "disbursement_date",
        "reason",
    }
    assert set(details.keys()) == expected_keys


# --- Happy path regression ---------------------------------------------------


def test_render_json_happy_path_still_succeeds(client: TestClient) -> None:
    """End-to-end regression: a normal CA disclosure render must still
    return 200 with the wrapper response model + a rendered Tier 1
    disclosure (the route's added try/except must not break the
    success path)."""
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}

    resp = client.post("/disclosures/render", json=body, headers=AUTH)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["disclosure_status"] == "ok"
    assert payload["rendered"]["state"] == "CA"
    assert payload["rendered"]["tier"] == 1
    # Sanity: a real APR string is present, not the failure sentinel.
    assert "0.00%" not in payload["rendered"]["html"]


def test_render_html_happy_path_still_succeeds(client: TestClient) -> None:
    """Same regression for the HTML route — 200 with the disclosure HTML."""
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}

    resp = client.post("/disclosures/render.html", json=body, headers=AUTH)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "0.00%" not in resp.text


def test_happy_path_writes_no_apr_failed_audit_row(
    client: TestClient, audit_log: InMemoryAuditLog
) -> None:
    """On the success path no ``aegis_apr_compute_failed`` row is written
    (regression guard against accidentally always-logging the row)."""
    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}
    resp = client.post("/disclosures/render", json=body, headers=AUTH)
    assert resp.status_code == 200
    failed = [
        e for e in audit_log.entries if e["action"] == "aegis_apr_compute_failed"
    ]
    assert failed == []


# --- Non-UUID deal_id branch -------------------------------------------------


def test_audit_subject_id_none_when_deal_id_is_not_a_uuid(
    client: TestClient,
    audit_log: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``APRDisclosureError.deal_id`` is somehow not a UUID string
    (defensive — current shape is always a merchant UUID), the audit
    row's ``subject_id`` falls back to ``None`` while ``details.deal_id``
    preserves the raw value so the row is still queryable by hand."""
    from aegis.api.routes import disclosures as disclosures_module
    from aegis.compliance.disclosure_context import APRDisclosureError

    # Patch render_disclosure to raise an APRDisclosureError with a
    # non-UUID deal_id. We don't go through the context-builder path
    # because we explicitly want to exercise the UUID-parse branch.
    def _raise(*_args: object, **_kwargs: object) -> Any:
        raise APRDisclosureError(
            "synthetic non-uuid deal_id",
            state="CA",
            principal=Decimal("50000.00"),
            factor=Decimal("1.30"),
            term_days=120,
            disbursement_date=date(2026, 5, 13),
            deal_id="not-a-uuid",
        )

    monkeypatch.setattr(disclosures_module, "render_disclosure", _raise)

    body = {"state": "CA", "deal": _deal_payload(), "score": _score_payload()}
    resp = client.post("/disclosures/render", json=body, headers=AUTH)
    assert resp.status_code == 503

    rows = [
        e for e in audit_log.entries if e["action"] == "aegis_apr_compute_failed"
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["subject_id"] is None
    assert row["details"]["deal_id"] == "not-a-uuid"


