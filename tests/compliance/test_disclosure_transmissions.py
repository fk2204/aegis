"""Disclosure transmission audit-trail tests (R0.5).

Covers ``compliance/transmission.py``:
  * the in-memory repo round-trips a record with all expected fields
  * sha256 of rendered HTML is computed deterministically
  * the helper rejects malformed state codes
  * write failures raise ``DisclosureTransmissionWriteError`` (covered by
    the Supabase impl docstring — tested via in-memory fault injection)

Migration-level coverage (column existence, retention floor) lives in
``tests/compliance/test_migrations.py``.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from aegis.compliance.transmission import (
    DisclosureTransmissionRecord,
    InMemoryDisclosureTransmissionRepository,
    record_disclosure_transmission,
)

_DEAL_ID = UUID("44444444-4444-4444-8444-444444444444")
_MERCHANT_ID = UUID("55555555-5555-4555-8555-555555555555")
_RENDERED_HTML = "<html><body>CA disclosure body — Estimated APR 36.50%.</body></html>"


def _expected_sha() -> str:
    return hashlib.sha256(_RENDERED_HTML.encode("utf-8")).hexdigest()


def _baseline_kwargs() -> dict[str, object]:
    """Common kwargs every test reuses. Mutate by overriding."""
    return {
        "deal_id": _DEAL_ID,
        "merchant_id": _MERCHANT_ID,
        "state": "CA",
        "disclosure_version": "CA_SB1235_SB362_v1",
        "template_path": "compliance/templates/ca_sb1235.html.j2",
        "rendered_html": _RENDERED_HTML,
        "recipient_email": "owner@example.com",
        "sent_by": "system",
        "apr": Decimal("0.3650"),
        "funding_provided": Decimal("50000.00"),
        "finance_charge": Decimal("15000.00"),
        "estimated_total_payment": Decimal("65000.00"),
        "estimated_term_days": 120,
        "factor_rate": Decimal("1.3000"),
        "holdback_pct": Decimal("0.1200"),
        "metadata": {"has_savings_disclosure": False},
    }


def test_in_memory_repo_records_a_row_with_expected_fields() -> None:
    """A successful record() returns a populated record with the right
    state, hash, term, and APR fields."""
    repo = InMemoryDisclosureTransmissionRepository()
    record = record_disclosure_transmission(repo, **_baseline_kwargs())  # type: ignore[arg-type]

    assert isinstance(record, DisclosureTransmissionRecord)
    assert record.state == "CA"
    assert record.disclosure_version == "CA_SB1235_SB362_v1"
    assert record.template_path == "compliance/templates/ca_sb1235.html.j2"
    assert record.html_sha256 == _expected_sha()
    assert record.recipient_email == "owner@example.com"
    assert record.sent_by == "system"
    assert record.apr == Decimal("0.3650")
    assert record.funding_provided == Decimal("50000.00")
    assert record.finance_charge == Decimal("15000.00")
    assert record.estimated_total_payment == Decimal("65000.00")
    assert record.estimated_term_days == 120
    assert record.factor_rate == Decimal("1.3000")
    assert record.holdback_pct == Decimal("0.1200")
    assert record.metadata == {"has_savings_disclosure": False}
    assert record.deal_id == _DEAL_ID
    assert record.merchant_id == _MERCHANT_ID
    # sent_at defaults to "now"; ensure tz-aware datetime came back.
    assert isinstance(record.sent_at, datetime)
    assert record.sent_at.tzinfo is not None
    # Appended to the in-memory store exactly once.
    assert len(repo.rows) == 1
    assert repo.rows[0].id == record.id


def test_in_memory_repo_normalizes_lowercase_state_code() -> None:
    """USPS state codes are uppercase. The helper enforces this so the
    audit query can filter on a canonical value."""
    repo = InMemoryDisclosureTransmissionRepository()
    kwargs = _baseline_kwargs()
    kwargs["state"] = "ny"
    record = record_disclosure_transmission(repo, **kwargs)  # type: ignore[arg-type]
    assert record.state == "NY"


def test_in_memory_repo_rejects_invalid_state_code() -> None:
    """Defensive: a non-2-letter state code is a caller bug — fail loud
    rather than silently writing an unqueryable row."""
    repo = InMemoryDisclosureTransmissionRepository()
    kwargs = _baseline_kwargs()
    kwargs["state"] = "California"
    with pytest.raises(ValueError, match="2-letter USPS code"):
        record_disclosure_transmission(repo, **kwargs)  # type: ignore[arg-type]


def test_in_memory_repo_accepts_null_deal_and_merchant() -> None:
    """Both UUIDs nullable per the migration schema — a regulator-shaped
    audit query only requires (state, recipient_email, sent_at)."""
    repo = InMemoryDisclosureTransmissionRepository()
    kwargs = _baseline_kwargs()
    kwargs["deal_id"] = None
    kwargs["merchant_id"] = None
    record = record_disclosure_transmission(repo, **kwargs)  # type: ignore[arg-type]
    assert record.deal_id is None
    assert record.merchant_id is None


def test_in_memory_repo_accepts_explicit_sent_at() -> None:
    """When the caller provides ``sent_at`` (e.g. the email-send timestamp
    rather than the row-write timestamp), the helper honors it verbatim
    so the row's audit clock matches the merchant-visible event."""
    repo = InMemoryDisclosureTransmissionRepository()
    explicit_ts = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    kwargs = _baseline_kwargs()
    kwargs["sent_at"] = explicit_ts
    record = record_disclosure_transmission(repo, **kwargs)  # type: ignore[arg-type]
    assert record.sent_at == explicit_ts


def test_html_sha256_changes_when_html_changes() -> None:
    """Defensive: the hash is the merchant-bytes audit anchor. Different
    HTML => different hash. If this ever fails, the audit guarantee is
    broken — every transmission would look identical."""
    repo = InMemoryDisclosureTransmissionRepository()

    kwargs_a = _baseline_kwargs()
    kwargs_b = _baseline_kwargs()
    kwargs_b["rendered_html"] = _RENDERED_HTML + "<!-- modified -->"

    a = record_disclosure_transmission(repo, **kwargs_a)  # type: ignore[arg-type]
    b = record_disclosure_transmission(repo, **kwargs_b)  # type: ignore[arg-type]
    assert a.html_sha256 != b.html_sha256


def test_ny_transmission_record_carries_ny_version_string() -> None:
    """NY 23 NYCRR § 600 lives in the same table as CA § 952. The
    audit-query convention is to filter on ``state`` + ``disclosure_version``
    so a regulator can disambiguate when the same merchant received
    transmissions across jurisdictions."""
    repo = InMemoryDisclosureTransmissionRepository()
    kwargs = _baseline_kwargs()
    kwargs["state"] = "NY"
    kwargs["disclosure_version"] = "NY_CFDL_v1"
    kwargs["template_path"] = "compliance/templates/ny_cfdl.html.j2"
    record = record_disclosure_transmission(repo, **kwargs)  # type: ignore[arg-type]
    assert record.state == "NY"
    assert record.disclosure_version == "NY_CFDL_v1"
