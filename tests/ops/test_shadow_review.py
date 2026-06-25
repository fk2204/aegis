"""Unit tests for the weekly shadow-signal review pass.

Covers ``aegis.ops.shadow_review`` — parsing, extraction, the cron
pass itself (audit-row shape, summary, idempotency), and the Today
attention-section builder.

Pattern follows ``tests/compliance/test_compliance_obligation_tracker.py``:
in-memory repos for documents + merchants, ``InMemoryAuditLog`` for
audit assertions, pure ``date`` math driven by an explicit ``today``
argument so the tests never depend on wall-clock UTC vs local time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.ops.shadow_review import (
    DEFAULT_WINDOW_DAYS,
    ShadowReviewSummary,
    build_shadow_review_attention_section,
    collect_shadow_fires,
    extract_fires_from_document,
    parse_shadow_flag,
    run_shadow_review_pass,
)
from aegis.storage import DocumentRow, InMemoryDocumentRepository

TODAY = date(2026, 6, 25)
NOW = datetime(2026, 6, 25, 6, 0, tzinfo=UTC)


def _seed_doc(
    docs: InMemoryDocumentRepository,
    *,
    merchant_id: UUID,
    filename: str = "stmt.pdf",
    parsed_offset_days: int = 0,
    all_flags: list[str] | None = None,
) -> DocumentRow:
    """Push a synthetic DocumentRow into the in-memory repo with the
    chosen ``parsed_at`` offset (negative = earlier than TODAY).
    """
    parsed_at = datetime.combine(
        TODAY + timedelta(days=parsed_offset_days),
        datetime.min.time(),
        tzinfo=UTC,
    )
    row = DocumentRow(
        id=uuid4(),
        file_hash=uuid4().hex,
        byte_size=1024,
        original_filename=filename,
        merchant_id=merchant_id,
        uploaded_by="test",
        uploaded_at=parsed_at,
        parsed_at=parsed_at,
        all_flags=list(all_flags or []),
    )
    docs._docs[row.id] = row
    return row


def _seed_merchant(
    merchants: InMemoryMerchantRepository,
    *,
    business_name: str = "Test Merchant LLC",
) -> MerchantRow:
    row = MerchantRow(business_name=business_name, state="CA")
    merchants._by_id[row.id] = row
    return row


# ---------------------------------------------------------------------------
# parse_shadow_flag — string parsing
# ---------------------------------------------------------------------------


def test_parse_shadow_flag_with_detail() -> None:
    code, detail = parse_shadow_flag("[SHADOW] ai_generated_statement: score=72/100 signals=[a,b]")  # type: ignore[misc]
    assert code == "ai_generated_statement"
    assert detail == "score=72/100 signals=[a,b]"


def test_parse_shadow_flag_without_detail() -> None:
    parsed = parse_shadow_flag("[SHADOW] unreconciled_internal_transfer_v2")
    assert parsed is not None
    code, detail = parsed
    assert code == "unreconciled_internal_transfer_v2"
    assert detail == ""


def test_parse_shadow_flag_strips_whitespace() -> None:
    parsed = parse_shadow_flag("[SHADOW]   weird_code  :  detail with spaces  ")
    assert parsed == ("weird_code", "detail with spaces")


def test_parse_shadow_flag_rejects_non_shadow() -> None:
    assert parse_shadow_flag("[WARN] something") is None
    assert parse_shadow_flag("[PATTERN] X") is None
    assert parse_shadow_flag("plain text") is None
    assert parse_shadow_flag("") is None


def test_parse_shadow_flag_rejects_empty_body() -> None:
    assert parse_shadow_flag("[SHADOW] ") is None
    assert parse_shadow_flag("[SHADOW]   :detail") is None


# ---------------------------------------------------------------------------
# extract_fires_from_document
# ---------------------------------------------------------------------------


def test_extract_fires_collects_all_shadow_entries() -> None:
    docs = InMemoryDocumentRepository()
    merchant_id = uuid4()
    doc = _seed_doc(
        docs,
        merchant_id=merchant_id,
        filename="april.pdf",
        all_flags=[
            "[PATTERN] mca_stacking: 2 positions",
            "[SHADOW] unreconciled_internal_transfer_v2: $5k transfer-out",
            "[SHADOW] ai_generated_statement: score=72/100",
            "[WARN] fintech_bank_detected: Mercury",
        ],
    )
    fires = extract_fires_from_document(doc, merchant_name="Acme Co", merchant_id=merchant_id)
    codes = sorted(f.flag_code for f in fires)
    assert codes == ["ai_generated_statement", "unreconciled_internal_transfer_v2"]
    assert all(f.merchant_name == "Acme Co" for f in fires)
    assert all(f.document_filename == "april.pdf" for f in fires)


def test_extract_fires_skips_docs_with_no_parsed_at() -> None:
    docs = InMemoryDocumentRepository()
    merchant_id = uuid4()
    row = DocumentRow(
        id=uuid4(),
        file_hash="abc",
        byte_size=100,
        original_filename="x.pdf",
        merchant_id=merchant_id,
        uploaded_by="test",
        uploaded_at=NOW,
        parsed_at=None,
        all_flags=["[SHADOW] ai_generated_statement: hit"],
    )
    docs._docs[row.id] = row
    assert extract_fires_from_document(row, merchant_name="X", merchant_id=merchant_id) == []


# ---------------------------------------------------------------------------
# collect_shadow_fires — window filter
# ---------------------------------------------------------------------------


def test_collect_shadow_fires_filters_by_parsed_at_window() -> None:
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    m = _seed_merchant(merchants)
    # Inside window (parsed today).
    _seed_doc(
        docs,
        merchant_id=m.id,
        filename="inside.pdf",
        parsed_offset_days=0,
        all_flags=["[SHADOW] ai_generated_statement: hit"],
    )
    # Outside window (parsed 10 days ago).
    _seed_doc(
        docs,
        merchant_id=m.id,
        filename="outside.pdf",
        parsed_offset_days=-10,
        all_flags=["[SHADOW] unreconciled_internal_transfer_v2: leg"],
    )
    since = NOW - timedelta(days=DEFAULT_WINDOW_DAYS)
    fires, docs_scanned = collect_shadow_fires(docs=docs, merchants=merchants, since=since)
    assert docs_scanned == 1
    assert len(fires) == 1
    assert fires[0].flag_code == "ai_generated_statement"
    assert fires[0].document_filename == "inside.pdf"


def test_collect_shadow_fires_handles_orphan_merchant() -> None:
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    orphan_merchant_id = uuid4()  # not in merchants repo
    _seed_doc(
        docs,
        merchant_id=orphan_merchant_id,
        all_flags=["[SHADOW] ai_generated_statement: hit"],
    )
    since = NOW - timedelta(days=DEFAULT_WINDOW_DAYS)
    fires, _ = collect_shadow_fires(docs=docs, merchants=merchants, since=since)
    assert len(fires) == 1
    assert fires[0].merchant_name == "(unknown merchant)"


# ---------------------------------------------------------------------------
# run_shadow_review_pass — full pass + audit shape
# ---------------------------------------------------------------------------


def test_run_pass_writes_per_fire_and_summary_audit_rows() -> None:
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    m = _seed_merchant(merchants, business_name="Acme Co")
    d1 = _seed_doc(
        docs,
        merchant_id=m.id,
        filename="d1.pdf",
        all_flags=[
            "[SHADOW] unreconciled_internal_transfer_v2: leg-1",
            "[SHADOW] ai_generated_statement: score=72/100",
        ],
    )
    d2 = _seed_doc(
        docs,
        merchant_id=m.id,
        filename="d2.pdf",
        all_flags=["[SHADOW] ai_generated_statement: score=55/100"],
    )

    summary = run_shadow_review_pass(audit=audit, docs=docs, merchants=merchants, today=TODAY)

    # Per-fire rows: 2 (d1) + 1 (d2) = 3 distinct (doc, code) tuples.
    per_fire = [e for e in audit.entries if e["action"] == "shadow_signal.weekly_summary"]
    assert len(per_fire) == 3
    assert summary.audit_rows_written == 3

    # Each per-fire row carries the right shape.
    sample = next(
        e
        for e in per_fire
        if e["subject_id"] == str(d1.id)
        and (e["details"] or {}).get("flag_code") == "ai_generated_statement"
    )
    details = sample["details"]
    assert details["flag_code"] == "ai_generated_statement"
    assert details["flag_detail"] == "score=72/100"
    assert details["document_filename"] == "d1.pdf"
    assert details["merchant_name"] == "Acme Co"
    assert details["window_start"] == (TODAY - timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat()
    assert details["window_end"] == TODAY.isoformat()
    assert sample["actor"] == "cron.shadow_review"
    assert sample["subject_type"] == "document"

    # Summary row at the end.
    completes = [e for e in audit.entries if e["action"] == "shadow_signal.weekly_summary_complete"]
    assert len(completes) == 1
    summary_details = completes[0]["details"]
    assert summary_details["docs_scanned"] == 2
    assert summary_details["docs_with_shadow"] == 2
    assert summary_details["counts_by_code"]["ai_generated_statement"] == 2
    assert summary_details["counts_by_code"]["unreconciled_internal_transfer_v2"] == 1
    # source_document_ids preserved per the aggregate-with-source-ids rule.
    ai_ids = summary_details["source_document_ids_by_code"]["ai_generated_statement"]
    assert sorted(ai_ids) == sorted([str(d1.id), str(d2.id)])


def test_run_pass_idempotent_within_same_window() -> None:
    """A second run inside the same window must NOT write duplicate
    per-fire rows. The summary row writes again (auditable cadence)
    but the dedupe count reflects the skipped fires.
    """
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    m = _seed_merchant(merchants)
    _seed_doc(
        docs,
        merchant_id=m.id,
        all_flags=["[SHADOW] ai_generated_statement: hit"],
    )

    first = run_shadow_review_pass(audit=audit, docs=docs, merchants=merchants, today=TODAY)
    assert first.audit_rows_written == 1
    assert first.audit_rows_skipped_dup == 0

    second = run_shadow_review_pass(audit=audit, docs=docs, merchants=merchants, today=TODAY)
    assert second.audit_rows_written == 0
    assert second.audit_rows_skipped_dup == 1

    per_fire_rows = [e for e in audit.entries if e["action"] == "shadow_signal.weekly_summary"]
    assert len(per_fire_rows) == 1  # not 2 — dedupe held


def test_run_pass_writes_summary_even_when_no_fires() -> None:
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    # No documents — no fires.
    summary = run_shadow_review_pass(audit=audit, docs=docs, merchants=merchants, today=TODAY)
    assert summary.audit_rows_written == 0
    assert summary.docs_scanned == 0
    completes = [e for e in audit.entries if e["action"] == "shadow_signal.weekly_summary_complete"]
    assert len(completes) == 1
    assert completes[0]["details"]["docs_with_shadow"] == 0


def test_run_pass_collapses_duplicate_code_on_same_doc_to_one_audit_row() -> None:
    """A document with two ``[SHADOW] same_code: ...`` entries collapses
    to one audit row whose ``details`` carries the first detail in the
    main field and the rest in ``additional_details``.
    """
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    m = _seed_merchant(merchants)
    _seed_doc(
        docs,
        merchant_id=m.id,
        all_flags=[
            "[SHADOW] unreconciled_internal_transfer_v2: leg-1 $5k",
            "[SHADOW] unreconciled_internal_transfer_v2: leg-2 $7k",
        ],
    )
    summary = run_shadow_review_pass(audit=audit, docs=docs, merchants=merchants, today=TODAY)
    per_fire = [e for e in audit.entries if e["action"] == "shadow_signal.weekly_summary"]
    assert len(per_fire) == 1
    details = per_fire[0]["details"]
    assert details["flag_detail"] == "leg-1 $5k"
    assert details["additional_details"] == ["leg-2 $7k"]
    assert summary.counts_by_code["unreconciled_internal_transfer_v2"] == 1


# ---------------------------------------------------------------------------
# build_shadow_review_attention_section — Today card
# ---------------------------------------------------------------------------


def test_attention_section_counts_distinct_docs_not_fires() -> None:
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    m = _seed_merchant(merchants, business_name="Multi-fire Inc")
    # One doc, two distinct shadow codes → counts as ONE in the
    # "distinct docs" measure (the card displays distinct merchants in
    # need of review, not raw fire counts).
    _seed_doc(
        docs,
        merchant_id=m.id,
        all_flags=[
            "[SHADOW] ai_generated_statement: hit",
            "[SHADOW] unreconciled_internal_transfer_v2: leg",
        ],
    )
    count, source_ids, cards = build_shadow_review_attention_section(
        docs=docs, merchants=merchants, today=TODAY
    )
    assert count == 1
    assert len(source_ids) == 1
    assert len(cards) == 1
    assert sorted(cards[0].contributing_codes) == [
        "ai_generated_statement",
        "unreconciled_internal_transfer_v2",
    ]
    assert cards[0].merchant_name == "Multi-fire Inc"
    assert cards[0].href == "/ui/shadow-review"


def test_attention_section_caps_cards_but_keeps_full_count() -> None:
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    m = _seed_merchant(merchants)
    for i in range(7):
        _seed_doc(
            docs,
            merchant_id=m.id,
            filename=f"doc-{i}.pdf",
            all_flags=["[SHADOW] ai_generated_statement: hit"],
        )
    count, source_ids, cards = build_shadow_review_attention_section(
        docs=docs, merchants=merchants, today=TODAY, max_cards=5
    )
    assert count == 7
    assert len(source_ids) == 7
    assert len(cards) == 5  # capped


def test_attention_section_returns_zero_when_no_fires() -> None:
    docs = InMemoryDocumentRepository()
    merchants = InMemoryMerchantRepository()
    count, source_ids, cards = build_shadow_review_attention_section(
        docs=docs, merchants=merchants, today=TODAY
    )
    assert count == 0
    assert source_ids == []
    assert cards == []


def test_summary_dataclass_round_trip_is_immutable() -> None:
    """Sanity — ShadowReviewSummary is frozen so callers can't mutate
    fields after the fact and inadvertently corrupt audit-trail data."""
    s = ShadowReviewSummary(
        window_start=TODAY - timedelta(days=7),
        window_end=TODAY,
        docs_scanned=0,
        fires=(),
        counts_by_code={},
        source_document_ids_by_code={},
        audit_rows_written=0,
    )
    import dataclasses

    assert dataclasses.is_dataclass(s)
    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        s.audit_rows_written = 99  # type: ignore[misc]
