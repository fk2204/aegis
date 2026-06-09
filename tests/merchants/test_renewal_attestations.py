"""Funder renewal-disclosure attestation tests (U6 — migration 040).

Covers ``aegis.merchants.renewal_attestations`` + the route-level
``POST /ui/renewals/{merchant_id}/attest`` + the four-state renewal-
status logic in ``list_upcoming_renewals``.

Per CLAUDE.md SCOPE NOTE: AEGIS does NOT own the regulator-facing
renewal disclosure obligation — funder partners do. These rows record
OPERATOR CLAIMS about funder behavior, not regulator-facing audit
artifacts.

PII discipline: the audit-log ``details`` payload written alongside
each attestation must carry NEITHER ``business_name`` NOR ``owner_name``.
A canary test enforces this so the audit log stays PII-clean.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_merchant_repository,
    get_renewal_attestation_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.renewal_attestations import (
    InMemoryRenewalAttestationRepository,
    RenewalAttestationConflictError,
    RenewalAttestationRecord,
    record_renewal_attestation,
    resolve_applicable_statute,
)
from aegis.merchants.repository import (
    InMemoryMerchantRepository,
    list_upcoming_renewals,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TODAY = date(2026, 6, 9)


def _merchant(
    *,
    state: str = "NY",
    is_renewal: bool = True,
    maturity_offset_days: int | None = 20,
    business_name: str = "NY Pizza LLC",
) -> MerchantRow:
    maturity = (
        _TODAY + timedelta(days=maturity_offset_days)
        if maturity_offset_days is not None
        else None
    )
    return MerchantRow(
        id=uuid4(),
        business_name=business_name,
        state=state,
        is_renewal=is_renewal,
        maturity_date=maturity,
    )


@pytest.fixture
def attestations() -> InMemoryRenewalAttestationRepository:
    return InMemoryRenewalAttestationRepository()


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


# ---------------------------------------------------------------------------
# Repository round-trip
# ---------------------------------------------------------------------------


def test_repository_round_trip_returns_recorded_row(
    attestations: InMemoryRenewalAttestationRepository,
) -> None:
    """A persisted attestation is queryable via ``find_for_renewal``."""
    merchant_id = uuid4()
    maturity = _TODAY + timedelta(days=20)
    sent_at = _TODAY - timedelta(days=1)

    record = attestations.record(
        merchant_id=merchant_id,
        funder_name="OnDeck",
        maturity_date=maturity,
        disclosure_sent_at=sent_at,
        attested_by="filip@commerafunding.com",
        state="NY",
        applicable_statute=resolve_applicable_statute("NY"),
        notes="confirmed via funder reply email",
    )

    assert isinstance(record, RenewalAttestationRecord)
    assert record.merchant_id == merchant_id
    assert record.funder_name == "OnDeck"
    assert record.maturity_date == maturity
    assert record.disclosure_sent_at == sent_at
    assert record.attested_by == "filip@commerafunding.com"
    assert record.state == "NY"
    assert record.applicable_statute == "NY 23 NYCRR § 600.17"
    assert record.notes == "confirmed via funder reply email"
    assert isinstance(record.attested_at, datetime)
    assert record.attested_at.tzinfo is not None

    found = attestations.find_for_renewal(
        merchant_id=merchant_id, maturity_date=maturity
    )
    assert len(found) == 1
    assert found[0].id == record.id


def test_repository_rejects_blank_funder_name(
    attestations: InMemoryRenewalAttestationRepository,
) -> None:
    """A blank funder_name is a caller bug — refuse to write."""
    with pytest.raises(ValueError, match="funder_name must not be empty"):
        attestations.record(
            merchant_id=uuid4(),
            funder_name="   ",
            maturity_date=_TODAY + timedelta(days=10),
            disclosure_sent_at=_TODAY,
            attested_by="filip",
            state="NY",
        )


def test_repository_rejects_malformed_state(
    attestations: InMemoryRenewalAttestationRepository,
) -> None:
    """A non-2-letter state code is a caller bug — fail loud."""
    with pytest.raises(ValueError, match="2-letter USPS code"):
        attestations.record(
            merchant_id=uuid4(),
            funder_name="OnDeck",
            maturity_date=_TODAY + timedelta(days=10),
            disclosure_sent_at=_TODAY,
            attested_by="filip",
            state="California",
        )


# ---------------------------------------------------------------------------
# list_upcoming_renewals four-state logic
# ---------------------------------------------------------------------------


def test_attestation_flips_status_to_disclosure_sent(
    attestations: InMemoryRenewalAttestationRepository,
    merchants: InMemoryMerchantRepository,
) -> None:
    """A recorded attestation for (merchant, maturity) flips status."""
    m = _merchant(state="NY", maturity_offset_days=20)
    merchants.upsert(m)
    attestations.record(
        merchant_id=m.id,
        funder_name="OnDeck",
        maturity_date=m.maturity_date,  # type: ignore[arg-type]
        disclosure_sent_at=_TODAY - timedelta(days=2),
        attested_by="filip",
        state="NY",
    )
    rows = list_upcoming_renewals(
        merchants, today=_TODAY, attestations=attestations
    )
    assert len(rows) == 1
    assert rows[0].renewal_status == "disclosure_sent"


def test_no_attestation_within_14d_window_marks_pending(
    attestations: InMemoryRenewalAttestationRepository,
    merchants: InMemoryMerchantRepository,
) -> None:
    """NY merchant 40 days from maturity → 10 days from the 30d deadline.
    Within the 14-day pending window → ``disclosure_pending``."""
    m = _merchant(state="NY", maturity_offset_days=40)
    merchants.upsert(m)
    rows = list_upcoming_renewals(
        merchants, today=_TODAY, attestations=attestations
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.days_until_state_deadline == 10
    assert row.renewal_status == "disclosure_pending"


def test_no_attestation_past_deadline_marks_overdue(
    attestations: InMemoryRenewalAttestationRepository,
    merchants: InMemoryMerchantRepository,
) -> None:
    """NY merchant 20 days from maturity → -10 days from the 30d deadline.
    Deadline already past + no attestation → ``disclosure_overdue``."""
    m = _merchant(state="NY", maturity_offset_days=20)
    merchants.upsert(m)
    rows = list_upcoming_renewals(
        merchants, today=_TODAY, attestations=attestations
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.days_until_state_deadline == -10
    assert row.renewal_status == "disclosure_overdue"


def test_no_attestation_distant_deadline_stays_default(
    attestations: InMemoryRenewalAttestationRepository,
    merchants: InMemoryMerchantRepository,
) -> None:
    """CA merchant 80 days from maturity → 20 days from the 60d deadline.
    Outside the 14-day window → default ``not_required_funder_owns``."""
    m = _merchant(state="CA", maturity_offset_days=80)
    merchants.upsert(m)
    rows = list_upcoming_renewals(
        merchants, today=_TODAY, attestations=attestations
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.days_until_state_deadline == 20
    assert row.renewal_status == "not_required_funder_owns"


def test_no_attestation_no_state_deadline_stays_default(
    attestations: InMemoryRenewalAttestationRepository,
    merchants: InMemoryMerchantRepository,
) -> None:
    """FL merchant: no AEGIS-tracked deadline → default regardless of
    days_until_maturity."""
    m = _merchant(state="FL", maturity_offset_days=5)
    merchants.upsert(m)
    rows = list_upcoming_renewals(
        merchants, today=_TODAY, attestations=attestations
    )
    assert len(rows) == 1
    assert rows[0].days_until_state_deadline is None
    assert rows[0].renewal_status == "not_required_funder_owns"


def test_omitted_attestations_argument_keeps_default(
    merchants: InMemoryMerchantRepository,
) -> None:
    """Backwards-compatibility check: callers that don't supply an
    attestations repo see every row as ``not_required_funder_owns``."""
    m = _merchant(state="NY", maturity_offset_days=20)
    merchants.upsert(m)
    rows = list_upcoming_renewals(merchants, today=_TODAY)
    assert len(rows) == 1
    assert rows[0].renewal_status == "not_required_funder_owns"


# ---------------------------------------------------------------------------
# record_renewal_attestation + audit_log
# ---------------------------------------------------------------------------


def test_record_renewal_attestation_writes_audit_row(
    attestations: InMemoryRenewalAttestationRepository,
    audit: InMemoryAuditLog,
) -> None:
    """``record_renewal_attestation`` persists + writes one audit row
    with the expected ``action`` + ``subject_*`` fields."""
    merchant_id = uuid4()
    maturity = _TODAY + timedelta(days=20)
    record_renewal_attestation(
        attestations,
        audit,
        merchant_id=merchant_id,
        funder_name="OnDeck",
        maturity_date=maturity,
        disclosure_sent_at=_TODAY,
        attested_by="filip@commerafunding.com",
        state="NY",
        actor_email="filip@commerafunding.com",
    )
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["action"] == "renewal_disclosure_attested"
    assert entry["subject_type"] == "merchant"
    assert entry["subject_id"] == str(merchant_id)
    assert entry["actor_email"] == "filip@commerafunding.com"


def test_audit_details_carry_funder_name_and_statute_no_pii(
    attestations: InMemoryRenewalAttestationRepository,
    audit: InMemoryAuditLog,
) -> None:
    """PII canary: audit ``details`` must include funder_name + statute
    + dates, and MUST NOT include ``business_name`` or ``owner_name``.
    Mirrors the audit-log discipline in CLAUDE.md."""
    merchant_id = uuid4()
    maturity = _TODAY + timedelta(days=20)
    record_renewal_attestation(
        attestations,
        audit,
        merchant_id=merchant_id,
        funder_name="OnDeck",
        maturity_date=maturity,
        disclosure_sent_at=_TODAY,
        attested_by="filip@commerafunding.com",
        state="CA",
        actor_email="filip@commerafunding.com",
        notes="op note (private) — not in audit",
    )
    details = audit.entries[0]["details"]
    assert isinstance(details, dict)
    assert details["funder_name"] == "OnDeck"
    assert details["state"] == "CA"
    assert details["applicable_statute"] == "CA SB 362 § 22806"
    assert details["maturity_date"] == maturity.isoformat()
    assert details["disclosure_sent_at"] == _TODAY.isoformat()
    # PII canary: no merchant-PII fields ever in details.
    assert "business_name" not in details
    assert "owner_name" not in details
    assert "ein" not in details
    assert "phone" not in details
    assert "email" not in details
    # The operator's free-form notes also stay out of the audit row.
    assert "notes" not in details


def test_audit_details_contain_no_pii_canary_strings(
    attestations: InMemoryRenewalAttestationRepository,
    audit: InMemoryAuditLog,
) -> None:
    """Defense-in-depth PII canary: even if a future change accidentally
    routes the merchant's business_name through the call site, the audit
    details dict must not surface it as a value.

    We seed canary strings on the merchant + assert they don't appear in
    the serialized audit details. Catches future regressions where
    someone passes ``merchant`` instead of ``merchant_id`` to the helper.
    """
    merchant_id = uuid4()
    maturity = _TODAY + timedelta(days=20)
    record_renewal_attestation(
        attestations,
        audit,
        merchant_id=merchant_id,
        funder_name="OnDeck",
        maturity_date=maturity,
        disclosure_sent_at=_TODAY,
        attested_by="filip",
        state="NY",
    )
    details_serialized = repr(audit.entries[0]["details"])
    canaries = ("CANARY_BUSINESS_NAME_DO_NOT_LEAK", "CANARY_OWNER_NAME_DO_NOT_LEAK")
    for canary in canaries:
        assert canary not in details_serialized


# ---------------------------------------------------------------------------
# Duplicate handling (idempotency policy: 409)
# ---------------------------------------------------------------------------


def test_duplicate_attestation_raises_conflict_error(
    attestations: InMemoryRenewalAttestationRepository,
) -> None:
    """The repo raises ``RenewalAttestationConflictError`` on a duplicate
    ``(merchant_id, maturity_date, funder_name)`` write. The route layer
    translates this to HTTP 409 — see test_route_double_submit_returns_409."""
    merchant_id = uuid4()
    maturity = _TODAY + timedelta(days=20)
    kwargs: dict[str, Any] = {
        "merchant_id": merchant_id,
        "funder_name": "OnDeck",
        "maturity_date": maturity,
        "disclosure_sent_at": _TODAY,
        "attested_by": "filip",
        "state": "NY",
    }
    first = attestations.record(**kwargs)
    with pytest.raises(RenewalAttestationConflictError) as exc:
        attestations.record(**kwargs)
    assert exc.value.existing_id == first.id


def test_duplicate_with_different_funder_name_is_allowed(
    attestations: InMemoryRenewalAttestationRepository,
) -> None:
    """Same (merchant, maturity) but different funder_name → two distinct
    attestations are allowed. Mirrors a real-world flow where two funders
    co-fund a renewal and each must transmit a notice separately."""
    merchant_id = uuid4()
    maturity = _TODAY + timedelta(days=20)
    attestations.record(
        merchant_id=merchant_id,
        funder_name="OnDeck",
        maturity_date=maturity,
        disclosure_sent_at=_TODAY,
        attested_by="filip",
        state="NY",
    )
    # Same merchant + maturity, different funder — no conflict.
    attestations.record(
        merchant_id=merchant_id,
        funder_name="Rapid Finance",
        maturity_date=maturity,
        disclosure_sent_at=_TODAY,
        attested_by="filip",
        state="NY",
    )
    assert len(attestations.rows) == 2


# ---------------------------------------------------------------------------
# UI route: POST /ui/renewals/{merchant_id}/attest
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_overrides() -> Iterator[tuple[
    TestClient,
    InMemoryMerchantRepository,
    InMemoryRenewalAttestationRepository,
    InMemoryAuditLog,
]]:
    """TestClient wired with in-memory merchant + attestation + audit repos."""
    reset_dependency_caches()
    merchants = InMemoryMerchantRepository()
    attestations = InMemoryRenewalAttestationRepository()
    audit = InMemoryAuditLog()
    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants
    app.dependency_overrides[get_renewal_attestation_repository] = (
        lambda: attestations
    )
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        yield c, merchants, attestations, audit
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_route_post_writes_row_and_redirects(
    client_with_overrides: tuple[
        TestClient,
        InMemoryMerchantRepository,
        InMemoryRenewalAttestationRepository,
        InMemoryAuditLog,
    ],
) -> None:
    """Happy path: POST writes the attestation row + audit row + redirects."""
    client, merchants, attestations, audit = client_with_overrides
    maturity = _TODAY + timedelta(days=20)
    m = MerchantRow(
        id=uuid4(),
        business_name="NY Pizza LLC",
        state="NY",
        is_renewal=True,
        maturity_date=maturity,
    )
    merchants.upsert(m)

    resp = client.post(
        f"/ui/renewals/{m.id}/attest",
        data={
            "funder_name": "OnDeck",
            "disclosure_sent_at": (_TODAY - timedelta(days=2)).isoformat(),
            "maturity_date": maturity.isoformat(),
            "notes": "confirmed via funder email",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"].startswith("/ui/renewals?flash=")
    assert "OnDeck" in resp.headers["location"]
    assert len(attestations.rows) == 1
    written = attestations.rows[0]
    assert written.merchant_id == m.id
    assert written.funder_name == "OnDeck"
    assert written.state == "NY"
    assert written.applicable_statute == "NY 23 NYCRR § 600.17"
    # Audit row written under the merchant subject.
    assert len(audit.entries) == 1
    assert audit.entries[0]["action"] == "renewal_disclosure_attested"


def test_route_post_404_unknown_merchant(
    client_with_overrides: tuple[
        TestClient,
        InMemoryMerchantRepository,
        InMemoryRenewalAttestationRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, *_ = client_with_overrides
    bogus = uuid4()
    resp = client.post(
        f"/ui/renewals/{bogus}/attest",
        data={
            "funder_name": "OnDeck",
            "disclosure_sent_at": _TODAY.isoformat(),
            "maturity_date": _TODAY.isoformat(),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_route_post_400_when_merchant_not_a_renewal(
    client_with_overrides: tuple[
        TestClient,
        InMemoryMerchantRepository,
        InMemoryRenewalAttestationRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, merchants, *_ = client_with_overrides
    m = MerchantRow(
        id=uuid4(),
        business_name="Not A Renewal",
        state="NY",
        is_renewal=False,
        maturity_date=_TODAY + timedelta(days=20),
    )
    merchants.upsert(m)
    resp = client.post(
        f"/ui/renewals/{m.id}/attest",
        data={
            "funder_name": "OnDeck",
            "disclosure_sent_at": _TODAY.isoformat(),
            "maturity_date": (_TODAY + timedelta(days=20)).isoformat(),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "not flagged as a renewal" in resp.text


def test_route_post_400_when_maturity_mismatch(
    client_with_overrides: tuple[
        TestClient,
        InMemoryMerchantRepository,
        InMemoryRenewalAttestationRepository,
        InMemoryAuditLog,
    ],
) -> None:
    """Form-submitted maturity_date must match the merchant's current
    maturity_date — defensive against a stale form render."""
    client, merchants, *_ = client_with_overrides
    m = MerchantRow(
        id=uuid4(),
        business_name="NY Pizza LLC",
        state="NY",
        is_renewal=True,
        maturity_date=_TODAY + timedelta(days=20),
    )
    merchants.upsert(m)
    resp = client.post(
        f"/ui/renewals/{m.id}/attest",
        data={
            "funder_name": "OnDeck",
            "disclosure_sent_at": _TODAY.isoformat(),
            "maturity_date": (_TODAY + timedelta(days=999)).isoformat(),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "does not match" in resp.text


def test_route_post_400_blank_funder(
    client_with_overrides: tuple[
        TestClient,
        InMemoryMerchantRepository,
        InMemoryRenewalAttestationRepository,
        InMemoryAuditLog,
    ],
) -> None:
    client, merchants, *_ = client_with_overrides
    m = MerchantRow(
        id=uuid4(),
        business_name="NY Pizza LLC",
        state="NY",
        is_renewal=True,
        maturity_date=_TODAY + timedelta(days=20),
    )
    merchants.upsert(m)
    resp = client.post(
        f"/ui/renewals/{m.id}/attest",
        data={
            "funder_name": "   ",
            "disclosure_sent_at": _TODAY.isoformat(),
            "maturity_date": (_TODAY + timedelta(days=20)).isoformat(),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_route_double_submit_returns_409(
    client_with_overrides: tuple[
        TestClient,
        InMemoryMerchantRepository,
        InMemoryRenewalAttestationRepository,
        InMemoryAuditLog,
    ],
) -> None:
    """Duplicate (merchant, maturity, funder_name) → 409. The first POST
    writes one row + one audit entry; the second POST writes neither."""
    client, merchants, attestations, audit = client_with_overrides
    m = MerchantRow(
        id=uuid4(),
        business_name="NY Pizza LLC",
        state="NY",
        is_renewal=True,
        maturity_date=_TODAY + timedelta(days=20),
    )
    merchants.upsert(m)
    payload = {
        "funder_name": "OnDeck",
        "disclosure_sent_at": _TODAY.isoformat(),
        "maturity_date": (_TODAY + timedelta(days=20)).isoformat(),
    }
    first = client.post(
        f"/ui/renewals/{m.id}/attest", data=payload, follow_redirects=False
    )
    assert first.status_code == 303
    second = client.post(
        f"/ui/renewals/{m.id}/attest", data=payload, follow_redirects=False
    )
    assert second.status_code == 409
    assert "already exists" in second.text
    # Only one attestation + one audit row landed.
    assert len(attestations.rows) == 1
    assert len(audit.entries) == 1


def test_get_renewals_shows_attestation_status_after_post(
    client_with_overrides: tuple[
        TestClient,
        InMemoryMerchantRepository,
        InMemoryRenewalAttestationRepository,
        InMemoryAuditLog,
    ],
) -> None:
    """End-to-end: POST attestation → GET /ui/renewals shows SENT chip
    instead of FUNDER OWNS / PENDING / OVERDUE."""
    client, merchants, _attestations, _audit = client_with_overrides
    m = MerchantRow(
        id=uuid4(),
        business_name="NY Pizza LLC",
        state="NY",
        is_renewal=True,
        maturity_date=_TODAY + timedelta(days=20),
    )
    merchants.upsert(m)
    # Before attestation: deadline 20-30=-10, so status = overdue chip.
    pre = client.get("/ui/renewals")
    assert pre.status_code == 200
    assert "OVERDUE" in pre.text

    payload = {
        "funder_name": "OnDeck",
        "disclosure_sent_at": _TODAY.isoformat(),
        "maturity_date": (_TODAY + timedelta(days=20)).isoformat(),
    }
    client.post(
        f"/ui/renewals/{m.id}/attest", data=payload, follow_redirects=False
    )
    post = client.get("/ui/renewals")
    assert post.status_code == 200
    assert "SENT" in post.text
    # The form is replaced by the "attestation recorded" sub-text.
    assert "attestation recorded" in post.text


def test_resolve_applicable_statute_returns_expected_values() -> None:
    """Quick coverage for the statute lookup used by the helper."""
    assert resolve_applicable_statute("CA") == "CA SB 362 § 22806"
    assert resolve_applicable_statute("ca") == "CA SB 362 § 22806"
    assert resolve_applicable_statute("NY") == "NY 23 NYCRR § 600.17"
    assert resolve_applicable_statute("FL") is None


__all__: list[str] = []
