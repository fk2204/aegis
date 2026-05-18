"""Tests for the decision snapshot writer (mp Phase 2).

Covers the in-memory implementation (the Supabase path needs a live
DB; that's exercised against test fixtures elsewhere). What we verify:

- Payload validation rejects invalid inputs at construction time.
- write() returns a UUID and appends to the in-memory list.
- write() invokes audit.record() with the correct action / subject.
- AuditWriteError propagates (audit-write failure fails the decision
  write contract).
- backfill_quality + decided_at flow through correctly.
- Frozen-model semantics (no mutation after construction).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from aegis.audit import AuditLog, AuditWriteError, InMemoryAuditLog
from aegis.compliance.snapshot import (
    DecisionPayload,
    InMemoryDecisionSnapshot,
    record_decision,
)

AEGIS_VERSION = "2.0.0"
RULE_PACK_VERSION = "2026.05.18"


def _payload(**overrides: Any) -> DecisionPayload:
    """Build a valid DecisionPayload, then apply overrides.

    Frozen / strict model so we can't update; instead we splat into a new
    construction. Defaults represent a clean CA approve.
    """
    defaults: dict[str, Any] = {
        "deal_id": uuid4(),
        "decided_by": "filip",
        "decision": "approve",
        "decision_reason_codes": [],
        "score": Decimal("72.50"),
        "score_factors": {"revenue": 25, "balance": 15, "nsf": -8},
        "analysis_id": uuid4(),
        "contributing_transaction_uuids": [uuid4(), uuid4()],
        "bank_statement_pdf_sha256": "a" * 64,
        "state_code": "CA",
        "cfdl_tier": 1,
        "disclosure_template_path": "docs/compliance/states/CA/03_disclosure_template.j2",
        "disclosure_template_sha256": "b" * 64,
        "disclosure_pdf_sha256": "c" * 64,
        "apr_calculated": Decimal("32.4500"),
        "apr_method": "reg_z_1026_22",
        "ofac_cache_timestamp": datetime.now(UTC),
        "ofac_cache_sha256": "d" * 64,
        "aegis_version": AEGIS_VERSION,
        "rule_pack_version": RULE_PACK_VERSION,
    }
    defaults.update(overrides)
    return DecisionPayload(**defaults)


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


def test_payload_rejects_bad_decision() -> None:
    with pytest.raises(ValidationError):
        _payload(decision="yolo")


def test_payload_rejects_bad_state_length() -> None:
    with pytest.raises(ValidationError):
        _payload(state_code="CAL")


def test_payload_rejects_extra_field() -> None:
    # strict + extra=forbid: typo in a field name fails validation.
    with pytest.raises(ValidationError):
        _payload(unknown_field="oops")


def test_payload_rejects_cfdl_tier_out_of_range() -> None:
    with pytest.raises(ValidationError):
        _payload(cfdl_tier=4)


def test_payload_is_frozen() -> None:
    p = _payload()
    with pytest.raises(ValidationError):
        p.score = Decimal("99")  # type: ignore[misc]


def test_payload_rejects_float_money() -> None:
    """Strict-mode Decimal rejects float for money fields (CLAUDE.md)."""
    with pytest.raises(ValidationError):
        _payload(score=72.5)


# ---------------------------------------------------------------------------
# In-memory write path
# ---------------------------------------------------------------------------


def test_write_appends_row_and_records_audit() -> None:
    audit = InMemoryAuditLog()
    snapshot = InMemoryDecisionSnapshot()
    payload = _payload(decision="approve")

    decision_id = snapshot.write(payload, audit=audit)

    assert isinstance(decision_id, UUID)
    rows = snapshot.rows()
    assert len(rows) == 1
    assert rows[0]["id"] == str(decision_id)
    assert rows[0]["deal_id"] == str(payload.deal_id)
    assert rows[0]["decision"] == "approve"

    # Audit row mirrors the decision.
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["action"] == "decision.approve"
    assert entry["subject_type"] == "deal"
    assert entry["subject_id"] == str(payload.deal_id)
    assert entry["details"]["decision_id"] == str(decision_id)


def test_record_decision_convenience_wraps_write() -> None:
    audit = InMemoryAuditLog()
    snapshot = InMemoryDecisionSnapshot()
    payload = _payload(decision="decline", decision_reason_codes=["nsf_count_gte_10"])

    decision_id = record_decision(payload, snapshot=snapshot, audit=audit)
    assert isinstance(decision_id, UUID)
    assert snapshot.rows()[0]["decision_reason_codes"] == ["nsf_count_gte_10"]
    assert audit.entries[0]["details"]["reason_codes"] == ["nsf_count_gte_10"]


def test_multiple_decisions_append() -> None:
    audit = InMemoryAuditLog()
    snapshot = InMemoryDecisionSnapshot()
    deal_id = uuid4()
    snapshot.write(_payload(deal_id=deal_id, decision="manual_review"), audit=audit)
    snapshot.write(_payload(deal_id=deal_id, decision="approve"), audit=audit)
    assert len(snapshot.rows()) == 2
    assert [r["decision"] for r in snapshot.rows()] == ["manual_review", "approve"]
    assert len(audit.entries) == 2


def test_backfill_quality_flows_through() -> None:
    audit = InMemoryAuditLog()
    snapshot = InMemoryDecisionSnapshot()
    decided_at = datetime(2025, 3, 14, 10, 30, tzinfo=UTC)
    payload = _payload(
        decided_by="backfill_2026_05",
        aegis_version="backfill",
        rule_pack_version="pre-snapshot-table",
        backfill_quality="partial",
        decided_at=decided_at,
        score_factors={},
    )
    snapshot.write(payload, audit=audit)
    row = snapshot.rows()[0]
    assert row["backfill_quality"] == "partial"
    assert row["decided_at"] == decided_at.isoformat()
    assert row["aegis_version"] == "backfill"


def test_score_factors_optional_default_empty() -> None:
    audit = InMemoryAuditLog()
    snapshot = InMemoryDecisionSnapshot()
    payload = _payload(score_factors={})
    snapshot.write(payload, audit=audit)
    assert snapshot.rows()[0]["score_factors"] == {}


# ---------------------------------------------------------------------------
# Audit-write failure propagates
# ---------------------------------------------------------------------------


class _FailingAudit:
    """AuditLog stub that raises on every record() call."""

    def record(self, **kwargs: Any) -> None:
        raise AuditWriteError("simulated audit-log outage")

    def list_recent(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return []

    def list_for_subject(
        self,
        *,
        subject_type: str,
        subject_id: UUID,
        action: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return []


def test_audit_write_failure_propagates() -> None:
    """Per master plan: audit-write failure must FAIL the decision write
    contract, not silent-fail. The decisions row is durable; the caller
    still sees the error so the audit gap is surfaced."""
    audit: AuditLog = _FailingAudit()
    snapshot = InMemoryDecisionSnapshot()
    with pytest.raises(AuditWriteError):
        snapshot.write(_payload(), audit=audit)
    # The row was still appended to the in-memory store before audit
    # failed — matches the Supabase implementation's ordering.
    assert len(snapshot.rows()) == 1
