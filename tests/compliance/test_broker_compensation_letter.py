"""NY § 600.21(f) broker-compensation letter tests (R3.1).

Covers ``compliance/broker_compensation.py`` letter renderer + the
transmission recorder that wires the letter into the existing
``disclosure_transmissions`` audit table.

Statutory cite: 23 NYCRR § 600.21(f) — "When a broker is involved, the
provider must inform the recipient in writing how, and by whom, the
broker is compensated for their role in the transaction." Source-of-
truth dossier: ``docs/compliance/02_new_york.md``.

Scope notes (per ``.claude/rules/compliance.md``):
  - AEGIS is internal pre-flight; the funder is the regulatory primary
    actor. R3.1 ships the generator + transmission recorder so AEGIS
    can deliver the letter on behalf of the funder relationship.
  - These tests do NOT modify ``test_new_york_tier1.py`` — that file's
    broker-comp tests cover the per-state pre-flight validator, which
    is a different surface.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from aegis.compliance.broker_compensation import (
    NY_BROKER_COMP_DISCLOSURE_VERSION,
    NY_BROKER_COMP_TEMPLATE_PATH,
    BrokerCompensationContext,
    BrokerCompensationLetterInputError,
    record_broker_compensation_transmission,
    render_broker_compensation_letter,
)
from aegis.compliance.transmission import (
    DisclosureTransmissionRecord,
    InMemoryDisclosureTransmissionRepository,
)

_FIXED_LETTER_DATE = date(2026, 6, 9)
_DEAL_ID = UUID("66666666-6666-4666-8666-666666666666")
_MERCHANT_ID = UUID("77777777-7777-4777-8777-777777777777")


def _baseline_context(**overrides: Any) -> BrokerCompensationContext:
    """Build a fully-populated context. Override individual fields by kwarg."""
    base: dict[str, Any] = {
        "broker_name": "Commera Capital",
        "broker_registration": "NY-ISO-2024-1234",
        "funder_name": "Acme Capital Funding LLC",
        "recipient_name": "Lighthouse Bakery Corp",
        "transaction_id": "TXN-2026-06-09-001",
        "letter_date": _FIXED_LETTER_DATE,
        "total_compensation": Decimal("5000.00"),
        "compensation_paid_by_funder": Decimal("5000.00"),
        "compensation_paid_by_recipient": Decimal("0.00"),
        "contingent_compensation_description": (
            "1% performance bonus payable if the recipient remains in good "
            "standing 90 days post-funding."
        ),
    }
    base.update(overrides)
    return BrokerCompensationContext(**base)


# --- Renderer: required fields populated ------------------------------------


def test_letter_renders_all_required_section_600_21_f_fields() -> None:
    """Every regulator-required disclosure element appears in the rendered HTML.

    Required elements per § 600.21(f):
      - Broker name + license/registration
      - Funder (provider) name
      - Total compensation broker receives in connection with this transaction
        (split: paid by funder, paid by recipient, contingent)
      - The recipient's right to ask for a written copy
      - Date + recipient identifier
    """
    ctx = _baseline_context()
    html = render_broker_compensation_letter(ctx)

    # Broker identification
    assert "Commera Capital" in html
    assert "NY-ISO-2024-1234" in html
    # Funder (provider)
    assert "Acme Capital Funding LLC" in html
    # Recipient identifier
    assert "Lighthouse Bakery Corp" in html
    # Transaction id + date
    assert "TXN-2026-06-09-001" in html
    assert "2026-06-09" in html
    # Money split formatted with 2dp and comma grouping
    assert "$5,000.00" in html  # total + funder
    assert "$0.00" in html  # recipient (0)
    # Contingent compensation text passes through verbatim
    assert (
        "1% performance bonus payable if the recipient remains in good "
        "standing 90 days post-funding."
    ) in html
    # Recipient's right to a written copy
    assert "request a written copy" in html
    # Cite locked at the bottom of the letter
    assert "23 NYCRR § 600.21(f)" in html


def test_letter_omits_broker_registration_row_cleanly_when_none() -> None:
    """Operator may not have a license/registration string; row must
    silently disappear rather than render a blank cell."""
    ctx = _baseline_context(broker_registration=None)
    html = render_broker_compensation_letter(ctx)
    assert "Broker license / registration" not in html
    # Other broker-identification content still present
    assert "Commera Capital" in html


# --- Renderer: validation -----------------------------------------------------


def test_total_compensation_none_rejected_at_context_boundary() -> None:
    """Pydantic config forbids None for total_compensation — it is a
    required Decimal field. § 600.21(f) requires a stated total."""
    # Bypassing the type checker on purpose — runtime validation is
    # the point of this test.
    with pytest.raises(BrokerCompensationLetterInputError):
        BrokerCompensationContext(
            broker_name="Commera Capital",
            broker_registration=None,
            funder_name="Acme Capital Funding LLC",
            recipient_name="Lighthouse Bakery Corp",
            transaction_id="TXN-2026-06-09-001",
            letter_date=_FIXED_LETTER_DATE,
            total_compensation=None,
            compensation_paid_by_funder=Decimal("0.00"),
            compensation_paid_by_recipient=Decimal("0.00"),
            contingent_compensation_description="None",
        )


def test_total_compensation_negative_rejected() -> None:
    """A negative total is incoherent and would silently lie to the
    recipient. Validator must reject."""
    with pytest.raises(
        BrokerCompensationLetterInputError, match="non-negative"
    ):
        _baseline_context(total_compensation=Decimal("-1.00"))


def test_compensation_split_negative_rejected() -> None:
    """Each leg of the split must be non-negative for the same reason."""
    with pytest.raises(
        BrokerCompensationLetterInputError, match="non-negative"
    ):
        _baseline_context(compensation_paid_by_funder=Decimal("-100.00"))
    with pytest.raises(
        BrokerCompensationLetterInputError, match="non-negative"
    ):
        _baseline_context(compensation_paid_by_recipient=Decimal("-0.01"))


def test_empty_string_identification_fields_rejected() -> None:
    """Broker / funder / recipient identifier are statutory minimums —
    a whitespace-only string would render an empty cell."""
    for field in (
        "broker_name",
        "funder_name",
        "recipient_name",
        "transaction_id",
        "contingent_compensation_description",
    ):
        with pytest.raises(
            BrokerCompensationLetterInputError, match="cannot be empty"
        ):
            _baseline_context(**{field: "   "})


# --- Renderer: snapshot lock --------------------------------------------------


def test_broker_compensation_letter_snapshot(snapshot: object) -> None:
    """Lock the rendered HTML byte-for-byte.

    First run creates the snapshot under
    ``tests/snapshots/broker_compensation/``. Subsequent runs assert
    byte-for-byte equality. A deliberate template or context change
    requires ``pytest --snapshot-update`` and a commit message
    explaining why (same discipline as the Tier 1 disclosure snapshots).
    """
    ctx = _baseline_context()
    html = render_broker_compensation_letter(ctx)
    snapshot.snapshot_dir = "tests/snapshots/broker_compensation"  # type: ignore[attr-defined]
    snapshot.assert_match(html, "ny_broker_compensation_letter.html")  # type: ignore[attr-defined]


# --- Transmission recorder ----------------------------------------------------


def test_transmission_record_carries_ny_broker_comp_version_and_hash() -> None:
    """Audit row distinguishes broker-comp letters from the § 600.6
    disclosure via the dedicated disclosure_version. The ``html_sha256``
    is the merchant-bytes audit anchor and must equal sha256(rendered_html).
    """
    ctx = _baseline_context()
    html = render_broker_compensation_letter(ctx)

    repo = InMemoryDisclosureTransmissionRepository()
    record = record_broker_compensation_transmission(
        repo,
        ctx,
        html,
        deal_id=_DEAL_ID,
        merchant_id=_MERCHANT_ID,
        recipient_email="owner@example.com",
        sent_by="system",
    )

    assert isinstance(record, DisclosureTransmissionRecord)
    assert record.state == "NY"
    assert record.disclosure_version == NY_BROKER_COMP_DISCLOSURE_VERSION
    assert record.disclosure_version == "NY_BROKER_COMP_v1"
    assert record.template_path == NY_BROKER_COMP_TEMPLATE_PATH
    assert (
        record.html_sha256
        == hashlib.sha256(html.encode("utf-8")).hexdigest()
    )
    # § 600.6-specific money fields aren't meaningful for a broker-comp
    # letter and must be left None on the row.
    assert record.apr is None
    assert record.funding_provided is None
    assert record.finance_charge is None
    assert record.estimated_total_payment is None
    assert record.estimated_term_days is None
    assert record.factor_rate is None
    assert record.holdback_pct is None
    # Appended exactly once.
    assert len(repo.rows) == 1
    assert repo.rows[0].id == record.id


def test_transmission_metadata_captures_broker_comp_split() -> None:
    """The broker-specific dollar amounts live on the audit row's
    ``metadata`` JSON so the full broker-comp fact-set is queryable
    without expanding the table schema."""
    ctx = _baseline_context(
        total_compensation=Decimal("7500.50"),
        compensation_paid_by_funder=Decimal("5000.00"),
        compensation_paid_by_recipient=Decimal("2500.50"),
    )
    html = render_broker_compensation_letter(ctx)

    repo = InMemoryDisclosureTransmissionRepository()
    record = record_broker_compensation_transmission(
        repo,
        ctx,
        html,
        deal_id=None,
        merchant_id=None,
        recipient_email=None,
        sent_by="ops-operator-1",
    )

    assert record.metadata is not None
    assert record.metadata["broker_name"] == "Commera Capital"
    assert record.metadata["funder_name"] == "Acme Capital Funding LLC"
    assert record.metadata["transaction_id"] == "TXN-2026-06-09-001"
    assert record.metadata["total_compensation"] == "7500.50"
    assert record.metadata["compensation_paid_by_funder"] == "5000.00"
    assert record.metadata["compensation_paid_by_recipient"] == "2500.50"


def test_transmission_caller_metadata_wins_on_conflict() -> None:
    """Caller-supplied metadata overrides defaults; documented contract."""
    ctx = _baseline_context()
    html = render_broker_compensation_letter(ctx)

    repo = InMemoryDisclosureTransmissionRepository()
    record = record_broker_compensation_transmission(
        repo,
        ctx,
        html,
        deal_id=None,
        merchant_id=None,
        recipient_email=None,
        sent_by=None,
        metadata={"transaction_id": "OVERRIDDEN", "funder_offer_id": "OFR-42"},
    )

    assert record.metadata is not None
    # Caller value wins on conflict
    assert record.metadata["transaction_id"] == "OVERRIDDEN"
    # Defaults preserved when not overridden
    assert record.metadata["broker_name"] == "Commera Capital"
    # New caller-supplied key passes through
    assert record.metadata["funder_offer_id"] == "OFR-42"


def test_transmission_explicit_sent_at_honored() -> None:
    """When the caller knows the actual send timestamp (e.g. SMTP
    timestamp from the mail server), the audit row must use it rather
    than the row-write clock."""
    ctx = _baseline_context()
    html = render_broker_compensation_letter(ctx)

    explicit_ts = datetime(2026, 6, 9, 14, 30, tzinfo=UTC)
    repo = InMemoryDisclosureTransmissionRepository()
    record = record_broker_compensation_transmission(
        repo,
        ctx,
        html,
        deal_id=_DEAL_ID,
        merchant_id=_MERCHANT_ID,
        recipient_email="owner@example.com",
        sent_by="system",
        sent_at=explicit_ts,
    )
    assert record.sent_at == explicit_ts
