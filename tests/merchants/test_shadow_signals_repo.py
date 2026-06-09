"""U22 — merchant-scope shadow signal repository tests.

Three concerns covered:

  1. Repository round-trip — a recorded row is queryable via
     ``list_by_merchant`` + ``list_by_code``, newest-first sort, limit
     respected.
  2. ``record_shadow_signal`` writes the paired ``audit_log`` row with
     ``action='shadow_signal_detected'``, subject_type='merchant',
     subject_id=merchant_id.
  3. PII canary — the audit ``details`` payload carries the signal CODE
     + severity + source_document_id ONLY. ``record.detail`` (which for
     ``related_account_suspected`` embeds the raw account_holder per
     the U12 detector) MUST NOT appear in the audit row.

Per CLAUDE.md "Decision-boundary changes — shadow-first": the U12
detector emits Pattern.severity=0; the U22 worker hook persists with
``signal_severity=0``. The repository accepts severity values >= 0 so
a future operator-validated decision-boundary flip via env-var doesn't
require a schema or contract change — but no test in this file writes
severity > 0 (the shadow contract).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.merchants.shadow_signals import (
    InMemoryMerchantShadowSignalRepository,
    MerchantShadowSignalRecord,
    record_shadow_signal,
)

# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def repo() -> InMemoryMerchantShadowSignalRepository:
    return InMemoryMerchantShadowSignalRepository()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


# ---------------------------------------------------------------------------
# Repository round-trip


def test_repository_round_trip_returns_recorded_row(
    repo: InMemoryMerchantShadowSignalRepository,
) -> None:
    """A persisted shadow signal is queryable via ``list_by_merchant``."""
    merchant_id = uuid4()
    src_doc_id = uuid4()
    prior_doc_id = uuid4()

    record = repo.record(
        merchant_id=merchant_id,
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail="sha256_match_with_doc=abc:uploaded=2026-06-08T00:00:00+00:00",
        source_document_id=src_doc_id,
        source_ids=[prior_doc_id],
        metadata={"emitted_by": "cross_statement_detector"},
        detected_by="worker",
    )

    matches = repo.list_by_merchant(merchant_id=merchant_id)
    assert len(matches) == 1
    assert matches[0].id == record.id
    assert matches[0].signal_code == "duplicate_pdf_upload"
    assert matches[0].signal_severity == 0
    assert matches[0].source_document_id == src_doc_id
    assert matches[0].source_ids == [prior_doc_id]
    assert matches[0].metadata == {"emitted_by": "cross_statement_detector"}
    assert matches[0].detected_by == "worker"


def test_list_by_merchant_filters_to_one_merchant(
    repo: InMemoryMerchantShadowSignalRepository,
) -> None:
    """Two merchants have independent shadow-signal feeds."""
    m1 = uuid4()
    m2 = uuid4()
    repo.record(
        merchant_id=m1,
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail="d",
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
    )
    repo.record(
        merchant_id=m2,
        signal_code="related_account_suspected",
        signal_severity=0,
        detail="d",
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
    )
    assert len(repo.list_by_merchant(merchant_id=m1)) == 1
    assert len(repo.list_by_merchant(merchant_id=m2)) == 1
    assert repo.list_by_merchant(merchant_id=m1)[0].signal_code == "duplicate_pdf_upload"
    assert repo.list_by_merchant(merchant_id=m2)[0].signal_code == "related_account_suspected"


def test_list_by_merchant_orders_newest_first(
    repo: InMemoryMerchantShadowSignalRepository,
) -> None:
    """Rows return newest-first by ``detected_at`` regardless of insert order."""
    merchant_id = uuid4()
    older = datetime(2026, 6, 1, 12, tzinfo=UTC)
    newer = datetime(2026, 6, 8, 12, tzinfo=UTC)

    # Insert NEWER first then OLDER to prove the sort isn't relying on
    # insertion order.
    repo.record(
        merchant_id=merchant_id,
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail="newer",
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
        detected_at=newer,
    )
    repo.record(
        merchant_id=merchant_id,
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail="older",
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
        detected_at=older,
    )

    rows = repo.list_by_merchant(merchant_id=merchant_id)
    assert [r.detail for r in rows] == ["newer", "older"]


def test_list_by_merchant_respects_limit(
    repo: InMemoryMerchantShadowSignalRepository,
) -> None:
    """``limit`` caps the result set."""
    merchant_id = uuid4()
    base = datetime(2026, 6, 1, 12, tzinfo=UTC)
    for i in range(5):
        repo.record(
            merchant_id=merchant_id,
            signal_code="duplicate_pdf_upload",
            signal_severity=0,
            detail=f"row{i}",
            source_document_id=None,
            source_ids=[],
            metadata=None,
            detected_by="worker",
            detected_at=base + timedelta(minutes=i),
        )
    assert len(repo.list_by_merchant(merchant_id=merchant_id, limit=3)) == 3
    assert len(repo.list_by_merchant(merchant_id=merchant_id, limit=10)) == 5


def test_list_by_code_filters_and_orders_newest_first(
    repo: InMemoryMerchantShadowSignalRepository,
) -> None:
    """``list_by_code`` returns matching code across merchants newest-first."""
    base = datetime(2026, 6, 1, 12, tzinfo=UTC)
    # Two duplicates, one related-account. Mix merchants.
    repo.record(
        merchant_id=uuid4(),
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail="d1",
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
        detected_at=base,
    )
    repo.record(
        merchant_id=uuid4(),
        signal_code="related_account_suspected",
        signal_severity=0,
        detail="r1",
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
        detected_at=base + timedelta(minutes=10),
    )
    repo.record(
        merchant_id=uuid4(),
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail="d2",
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
        detected_at=base + timedelta(minutes=20),
    )

    dups = repo.list_by_code(signal_code="duplicate_pdf_upload")
    assert len(dups) == 2
    assert [r.detail for r in dups] == ["d2", "d1"]

    rels = repo.list_by_code(signal_code="related_account_suspected")
    assert len(rels) == 1
    assert rels[0].detail == "r1"


def test_record_rejects_empty_signal_code(
    repo: InMemoryMerchantShadowSignalRepository,
) -> None:
    """Defensive: stray whitespace must not silently land as a code."""
    with pytest.raises(ValueError, match="signal_code must not be empty"):
        repo.record(
            merchant_id=uuid4(),
            signal_code="   ",
            signal_severity=0,
            detail=None,
            source_document_id=None,
            source_ids=[],
            metadata=None,
            detected_by="worker",
        )


def test_record_rejects_negative_severity(
    repo: InMemoryMerchantShadowSignalRepository,
) -> None:
    """Defensive: severity is shadow=0 today; negative is a caller bug."""
    with pytest.raises(ValueError, match="signal_severity must be >= 0"):
        repo.record(
            merchant_id=uuid4(),
            signal_code="duplicate_pdf_upload",
            signal_severity=-1,
            detail=None,
            source_document_id=None,
            source_ids=[],
            metadata=None,
            detected_by="worker",
        )


# ---------------------------------------------------------------------------
# record_shadow_signal — pairs the row with an audit_log entry


def test_record_shadow_signal_writes_paired_audit_row(
    repo: InMemoryMerchantShadowSignalRepository,
    audit: InMemoryAuditLog,
) -> None:
    """``record_shadow_signal`` writes one merchants_shadow_signals row AND
    one audit_log row with the documented shape."""
    merchant_id = uuid4()
    src_doc_id = uuid4()

    record = record_shadow_signal(
        repo,
        audit,
        merchant_id=merchant_id,
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail="sha256_match_with_doc=...:uploaded=...",
        source_document_id=src_doc_id,
        source_ids=[uuid4()],
        metadata={"emitted_by": "cross_statement_detector"},
        detected_by="worker",
    )

    assert len(repo.rows) == 1
    assert repo.rows[0].id == record.id
    assert len(audit.entries) == 1

    entry = audit.entries[0]
    assert entry["action"] == "shadow_signal_detected"
    assert entry["subject_type"] == "merchant"
    assert entry["subject_id"] == str(merchant_id)
    assert entry["actor"] == "worker"

    details: dict[str, Any] = entry["details"]
    assert details["code"] == "duplicate_pdf_upload"
    assert details["severity"] == 0
    assert details["source_document_id"] == str(src_doc_id)


# ---------------------------------------------------------------------------
# PII canary — audit details strip the holder string


def test_audit_details_omit_pattern_detail_pii_canary(
    repo: InMemoryMerchantShadowSignalRepository,
    audit: InMemoryAuditLog,
) -> None:
    """The U12 ``related_account_suspected`` detail string embeds the
    raw account_holder. The audit_log row MUST NOT carry the detail
    string — only the code + severity + source_document_id.

    Mirrors ``tests/merchants/test_renewal_attestations.py``
    ``test_audit_details_contain_no_pii_canary_strings``.
    """
    canary_holder = "CANARY_HOLDER_DO_NOT_LEAK"
    canary_detail = (
        f"holder={canary_holder}:existing_last4=9999:new_last4=1234"
    )
    record_shadow_signal(
        repo,
        audit,
        merchant_id=uuid4(),
        signal_code="related_account_suspected",
        signal_severity=0,
        detail=canary_detail,
        source_document_id=uuid4(),
        source_ids=[uuid4()],
        metadata={"emitted_by": "cross_statement_detector"},
        detected_by="worker",
    )

    assert len(audit.entries) == 1
    serialized = repr(audit.entries[0]["details"])
    assert canary_holder not in serialized, (
        "audit_log details leaked the raw account_holder via Pattern.detail"
    )
    # Defensive: ensure 'detail' key is not present at all.
    assert "detail" not in audit.entries[0]["details"]
    # The row itself MAY carry the holder (operator dossier surface) —
    # but the audit row must not.
    assert canary_holder in (repo.rows[0].detail or "")


def test_record_returned_object_is_pydantic_model(
    repo: InMemoryMerchantShadowSignalRepository,
) -> None:
    """``record`` returns a ``MerchantShadowSignalRecord`` — Pydantic, not a
    raw dict. Mirrors the ``transmission.py`` precedent."""
    rec = repo.record(
        merchant_id=uuid4(),
        signal_code="duplicate_pdf_upload",
        signal_severity=0,
        detail="d",
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
    )
    assert isinstance(rec, MerchantShadowSignalRecord)


def test_signal_code_strip_normalizes_whitespace(
    repo: InMemoryMerchantShadowSignalRepository,
) -> None:
    """Leading / trailing whitespace is trimmed so the audit roll-up
    query keys cleanly off the canonical code."""
    rec = repo.record(
        merchant_id=uuid4(),
        signal_code="  duplicate_pdf_upload  ",
        signal_severity=0,
        detail=None,
        source_document_id=None,
        source_ids=[],
        metadata=None,
        detected_by="worker",
    )
    assert rec.signal_code == "duplicate_pdf_upload"
