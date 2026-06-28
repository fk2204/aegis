"""Tests for the Phase E dossier license-verification gate.

Covers:
  * ``POST /ui/merchants/{id}/verify-license`` — write audit row,
    return 200 + HX-Refresh header; 403 for viewers.
  * Dossier template — gate banner + portal link render when
    ``license_gate.required`` is True; disabled per-funder submit
    button shows the ``license-gate`` data-test-id.
  * Submit button renders normally when the gate is bypassed.

The route tests follow the ``test_role_gate.py`` pattern: in-memory
backend, ``cf-access-authenticated-user-email`` header forging.
The template tests mirror ``test_dossier_patches.py`` — render the
real Jinja template against stub context.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_operator_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.business_intel.license_checker import LicenseGateContext
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.operator_repository import InMemoryOperatorRepository
from aegis.ops.operators import Operator, OperatorRole
from aegis.storage import AnalysisRow, DocumentRow
from aegis.web._templates import templates

# ---------------------------------------------------------------------------
# Route fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def gate_client() -> Iterator[
    tuple[
        TestClient,
        InMemoryMerchantRepository,
        InMemoryAuditLog,
        MerchantRow,
    ]
]:
    reset_dependency_caches()
    operators = InMemoryOperatorRepository()
    operators._seed(
        Operator(
            id=uuid4(),
            email="admin@aegis.test",
            display_name="Admin Operator",
            role=OperatorRole.ADMIN,
        )
    )
    operators._seed(
        Operator(
            id=uuid4(),
            email="uw@aegis.test",
            display_name="UW Operator",
            role=OperatorRole.UNDERWRITER,
        )
    )
    operators._seed(
        Operator(
            id=uuid4(),
            email="viewer@aegis.test",
            display_name="Viewer Operator",
            role=OperatorRole.VIEWER,
        )
    )

    merchants = InMemoryMerchantRepository()
    merchant = MerchantRow(
        id=uuid4(),
        business_name="Sunshine HVAC LLC",
        state="FL",
        industry_naics="238220",
    )
    merchants.upsert(merchant)

    audit = InMemoryAuditLog()

    app = create_app()
    app.dependency_overrides[get_operator_repository] = lambda: operators
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        yield c, merchants, audit, merchant
    app.dependency_overrides.clear()
    reset_dependency_caches()


def _as(email: str) -> dict[str, str]:
    return {"cf-access-authenticated-user-email": email}


# ---------------------------------------------------------------------------
# POST /verify-license
# ---------------------------------------------------------------------------


def test_verify_license_admin_writes_audit_and_returns_hx_refresh(
    gate_client: tuple[TestClient, InMemoryMerchantRepository, InMemoryAuditLog, MerchantRow],
) -> None:
    client, _merchants, audit, merchant = gate_client
    resp = client.post(
        f"/ui/merchants/{merchant.id}/verify-license",
        headers=_as("admin@aegis.test"),
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Refresh") == "true"
    assert "License verified" in resp.text
    assert 'data-test-id="dossier-license-gate-verified"' in resp.text
    rows = audit.list_for_subject(
        subject_type="merchant",
        subject_id=merchant.id,
        action="merchant.license_verified_manually",
    )
    assert len(rows) == 1
    assert rows[0]["actor"] == "operator:admin@aegis.test"
    assert rows[0]["actor_email"] == "admin@aegis.test"
    assert rows[0]["details"]["industry_naics"] == "238220"
    assert rows[0]["details"]["industry_key"] == "hvac_plumbing_contractor"
    assert rows[0]["details"]["state"] == "FL"


def test_verify_license_underwriter_allowed(
    gate_client: tuple[TestClient, InMemoryMerchantRepository, InMemoryAuditLog, MerchantRow],
) -> None:
    client, _merchants, audit, merchant = gate_client
    resp = client.post(
        f"/ui/merchants/{merchant.id}/verify-license",
        headers=_as("uw@aegis.test"),
    )
    assert resp.status_code == 200
    rows = audit.list_for_subject(
        subject_type="merchant",
        subject_id=merchant.id,
        action="merchant.license_verified_manually",
    )
    assert len(rows) == 1


def test_verify_license_viewer_403(
    gate_client: tuple[TestClient, InMemoryMerchantRepository, InMemoryAuditLog, MerchantRow],
) -> None:
    client, _merchants, audit, merchant = gate_client
    resp = client.post(
        f"/ui/merchants/{merchant.id}/verify-license",
        headers=_as("viewer@aegis.test"),
    )
    assert resp.status_code == 403
    assert "Access denied" in resp.text
    # No audit row written when gate blocks
    rows = audit.list_for_subject(
        subject_type="merchant",
        subject_id=merchant.id,
        action="merchant.license_verified_manually",
    )
    assert rows == []


def test_verify_license_unknown_merchant_404(
    gate_client: tuple[TestClient, InMemoryMerchantRepository, InMemoryAuditLog, MerchantRow],
) -> None:
    client, _merchants, _audit, _merchant = gate_client
    resp = client.post(
        f"/ui/merchants/{uuid4()}/verify-license",
        headers=_as("admin@aegis.test"),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Template render
# ---------------------------------------------------------------------------


def _make_merchant(
    *,
    state: str = "FL",
    industry_naics: str = "238220",
) -> MerchantRow:
    return MerchantRow(
        business_name="Sunshine HVAC LLC",
        state=state,
        industry_naics=industry_naics,
        time_in_business_months=24,
        credit_score=720,
        intake_date=date(2026, 5, 1),
    )


def _make_doc() -> DocumentRow:
    return DocumentRow(
        id=uuid4(),
        file_hash=uuid4().hex,
        byte_size=1024,
        original_filename="stmt.pdf",
        parse_status="proceed",
        fraud_score=20,
        all_flags=[],
        uploaded_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def _make_analysis() -> AnalysisRow:
    return AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        merchant_id=None,
        statement_period_start=date(2026, 5, 1),
        statement_period_end=date(2026, 5, 31),
        statement_days=31,
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("1500.00"),
        avg_daily_balance=Decimal("2000.00"),
        true_revenue=Decimal("50000.00"),
        monthly_revenue=Decimal("50000.00"),
        lowest_balance=Decimal("500.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=False,
        returned_ach_count=0,
    )


@dataclass
class _StubScore:
    recommendation: str = "approve"
    score: int = 70
    tier: str = "B"
    paper_grade: str = "B"
    suggested_max_advance: Decimal = Decimal("50000")
    hard_decline_reasons: list[str] = ()  # type: ignore[assignment]  # default sentinel; tests don't mutate
    soft_concerns: list[str] = ()  # type: ignore[assignment]
    decline_details: dict[str, Any] = ()  # type: ignore[assignment]


def _funder_card(funder_name: str = "Wide Net Capital") -> dict[str, Any]:
    return {
        "funder_id": str(uuid4()),
        "funder_name": funder_name,
        "match_score": 75,
        "color": "green",
        "hard_reasons": [],
        "soft_concerns": [],
        "criteria_comparison": [],
        "funder_requires_coj": False,
        "funder_charges_merchant_advance_fees": False,
        "estimated_terms": None,
        "tier_matches": [],
        "historical_approval_rate": None,
    }


def _render(*, license_gate: LicenseGateContext | None) -> str:
    """Render the dossier template with one funder card so the per-funder
    submit button section exercises."""
    return templates.get_template("merchant_detail_dossier.html.j2").render(
        request=None,
        merchant=_make_merchant(),
        documents=[],
        document=_make_doc(),
        analysis=_make_analysis(),
        aggregate_labels={},
        aggregate_unit_kind={},
        pattern_cards=[],
        latest_transactions=[],
        soft_signals=None,
        has_concentration_pattern=False,
        from_intake=False,
        intake_docs_uploaded=0,
        intake_docs_failed=0,
        score_result=_StubScore(recommendation="approve"),
        score_window=None,
        statement_coverage=None,
        stacking=None,
        mca_stack=None,
        balance_health=None,
        offer=None,
        state_tier=None,
        ofac_status="pending",
        ofac_match=None,
        trend=None,
        history=[],
        close_last_orchestration_capped=False,
        unified_tracks=None,
        shadow_signals=[],
        merchant_shadow_signals=[],
        revenue_trends=None,
        funder_note_submissions=[],
        operator_notes=[],
        operator_note_max_chars=2000,
        deal_summary=None,
        funder_narrative="",
        doc_checklist={
            "voided_check_on_file": False,
            "drivers_license_on_file": False,
            "bank_statements_months": 0,
        },
        stips_result=None,
        top_matched_funder_name="Wide Net Capital",
        matched_funders=[_funder_card()],
        matched_funder_responses={},
        submitted_funder_ids=set(),
        narrator_summary=None,
        all_statements_manual_review=False,
        license_gate=license_gate,
    )


def test_dossier_renders_gate_banner_when_required() -> None:
    gate = LicenseGateContext(
        required=True,
        industry_key="hvac_plumbing_contractor",
        industry_label="HVAC / Plumbing Contractor",
        portal_url="https://www.myfloridalicense.com/wl11.asp?mode=2&search=Name",
        state_name="Florida",
        already_verified=False,
    )
    html = _render(license_gate=gate)
    assert 'data-test-id="dossier-license-gate"' in html
    assert 'data-test-id="dossier-license-gate-portal-link"' in html
    assert 'data-test-id="dossier-license-gate-verify-button"' in html
    assert "Search Florida licensing portal" in html
    assert "HVAC / Plumbing Contractor" in html
    assert "myfloridalicense.com" in html
    # Per-funder submit button must be in the disabled state
    assert 'data-test-id="dossier-submit-funder-disabled-license-gate"' in html


def test_dossier_omits_gate_banner_when_not_required() -> None:
    gate = LicenseGateContext(
        required=False,
        industry_key=None,
        industry_label=None,
        portal_url=None,
        state_name=None,
        already_verified=False,
    )
    html = _render(license_gate=gate)
    assert 'data-test-id="dossier-license-gate"' not in html
    assert 'data-test-id="dossier-license-gate-portal-link"' not in html
    assert 'data-test-id="dossier-submit-funder-disabled-license-gate"' not in html


def test_dossier_omits_gate_banner_when_license_gate_missing() -> None:
    """Defensive: rendering with ``license_gate=None`` must not crash —
    the gate-required guard uses ``license_gate and license_gate.required``."""
    html = _render(license_gate=None)
    assert 'data-test-id="dossier-license-gate"' not in html


def test_dossier_omits_gate_banner_after_verification() -> None:
    """``already_verified=True`` flips ``required=False`` upstream, so the
    rendered dossier shows the Submit button (gate bypassed)."""
    gate = LicenseGateContext(
        required=False,
        industry_key="hvac_plumbing_contractor",
        industry_label="HVAC / Plumbing Contractor",
        portal_url="https://www.myfloridalicense.com/wl11.asp",
        state_name="Florida",
        already_verified=True,
    )
    html = _render(license_gate=gate)
    assert 'data-test-id="dossier-license-gate"' not in html
    # Per-funder submit state may be active OR another disabled variant
    # (no-lead, hard-fail, etc) — what matters is the license-gate
    # specific disabled state is gone now that the operator has verified.
    assert 'data-test-id="dossier-submit-funder-disabled-license-gate"' not in html
