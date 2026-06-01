"""Tests for chunk-3 ``PatternIndex`` + ``categorize_flags`` drill-down hook.

Covers:

* ``PatternIndex.build_for_document`` — per-doc lookup table from flag
  code to contributing ``FlagSourceTransaction`` rows. Empty when no
  PatternAnalysis cache (legacy pre-chunk-2 rows). Resolves Pattern
  source_ids against the doc's classified transactions; missing tx_ids
  are skipped; same-id dupes across patterns of the same code are
  dropped.
* ``PatternIndex.build_for_merchant`` — per-merchant aggregator across
  N docs with cross-doc filename tagging. Cross-doc duplicates (same
  code + tx_id but different doc) are preserved so the operator can
  see each contributing upload; in-doc duplicates are dropped.
* ``categorize_flags(pattern_index=...)`` — decorates each HumanFlag
  whose code matches an index entry with the contributing
  FlagSourceTransactions. Codes without entries leave the chip
  undecorated. Backward compatibility: ``pattern_index=None`` matches
  the legacy zero-arg behavior.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import PatternAnalysisDTO, PatternDTO
from aegis.storage import AnalysisRow
from aegis.web._attention_card import (
    DocumentPatternContext,
    PatternIndex,
    categorize_flags,
)
from aegis.web._flag_labels import FlagSourceTransaction

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _tx(
    *,
    tx_id: UUID | None = None,
    posted_date: date = date(2026, 4, 5),
    description: str = "DEPOSIT",
    amount: Decimal = Decimal("100.00"),
    source_page: int = 1,
    source_line: int = 10,
    category: str = "deposit",
) -> ClassifiedTransaction:
    """Minimal ClassifiedTransaction stub for PatternIndex tests."""
    return ClassifiedTransaction(
        id=tx_id or uuid4(),
        posted_date=posted_date,
        description=description,
        amount=amount,
        running_balance=None,
        source_page=source_page,
        source_line=source_line,
        category=category,
        classification_confidence=95,
    )


def _analysis_with_patterns(
    *, patterns: list[PatternDTO], merchant_id: UUID | None = None
) -> AnalysisRow:
    """AnalysisRow stub carrying a populated pattern_analysis cache."""
    return AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        merchant_id=merchant_id,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("2000.00"),
        avg_daily_balance=Decimal("1500.00"),
        true_revenue=Decimal("5000.00"),
        monthly_revenue=Decimal("5000.00"),
        lowest_balance=Decimal("500.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=False,
        returned_ach_count=0,
        pattern_analysis=PatternAnalysisDTO(patterns=patterns),
    )


def _analysis_without_pattern_cache() -> AnalysisRow:
    """AnalysisRow stub matching a legacy pre-chunk-2 row (pattern_analysis=None)."""
    return AnalysisRow(
        id=uuid4(),
        document_id=uuid4(),
        merchant_id=None,
        statement_period_start=date(2026, 4, 1),
        statement_period_end=date(2026, 4, 30),
        statement_days=30,
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("2000.00"),
        avg_daily_balance=Decimal("1500.00"),
        true_revenue=Decimal("5000.00"),
        monthly_revenue=Decimal("5000.00"),
        lowest_balance=Decimal("500.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=False,
        returned_ach_count=0,
        pattern_analysis=None,
    )


# ---------------------------------------------------------------------------
# PatternIndex.build_for_document
# ---------------------------------------------------------------------------


def test_build_for_document_returns_empty_when_analysis_is_none() -> None:
    """No AnalysisRow at all (orphaned doc) → empty index, chip render
    degrades to plain span without drill-down."""
    idx = PatternIndex.build_for_document(
        analysis=None,
        transactions=[_tx()],
        document_id=uuid4(),
        filename="x.pdf",
    )

    assert idx.by_code == {}
    assert idx.get("wash_deposit_suspected") is None


def test_build_for_document_returns_empty_when_pattern_cache_is_none() -> None:
    """Legacy pre-chunk-2 row (pattern_analysis=None) → empty index."""
    idx = PatternIndex.build_for_document(
        analysis=_analysis_without_pattern_cache(),
        transactions=[_tx()],
        document_id=uuid4(),
        filename="x.pdf",
    )

    assert idx.by_code == {}


def test_build_for_document_resolves_source_ids_to_flag_sources() -> None:
    """source_ids in the cache resolve against the doc's transactions
    and emerge as FlagSourceTransaction rows tagged with the doc's
    filename + document_id."""
    tx_a = _tx(tx_id=uuid4(), description="ACME DEPOSIT")
    tx_b = _tx(tx_id=uuid4(), description="ACME WITHDRAWAL")
    doc_id = uuid4()
    analysis = _analysis_with_patterns(
        patterns=[
            PatternDTO(
                code="wash_deposit_suspected",
                severity=35,
                detail="2 round-trip pairs within 5 days",
                source_ids=[tx_a.id, tx_b.id],
            ),
        ],
    )

    idx = PatternIndex.build_for_document(
        analysis=analysis,
        transactions=[tx_a, tx_b],
        document_id=doc_id,
        filename="march.pdf",
    )

    sources = idx.get("wash_deposit_suspected")
    assert sources is not None
    assert len(sources) == 2
    assert {s.description for s in sources} == {"ACME DEPOSIT", "ACME WITHDRAWAL"}
    assert all(isinstance(s, FlagSourceTransaction) for s in sources)
    assert all(s.filename == "march.pdf" for s in sources)
    assert all(s.document_id == str(doc_id) for s in sources)


def test_build_for_document_skips_source_ids_missing_from_transactions() -> None:
    """A source_id that doesn't match any transaction in the doc is
    silently dropped — defensive against a partial transactions list
    or a stale cache referencing a deleted row. Doesn't crash."""
    tx_a = _tx(tx_id=uuid4())
    orphan_source_id = uuid4()
    analysis = _analysis_with_patterns(
        patterns=[
            PatternDTO(
                code="duplicate_deposits_detected",
                severity=30,
                detail="2 same-date+amount pair(s)",
                source_ids=[tx_a.id, orphan_source_id],
            ),
        ],
    )

    idx = PatternIndex.build_for_document(
        analysis=analysis,
        transactions=[tx_a],
        document_id=uuid4(),
        filename="x.pdf",
    )

    sources = idx.get("duplicate_deposits_detected")
    assert sources is not None
    assert len(sources) == 1
    assert sources[0].posted_date == tx_a.posted_date


def test_build_for_document_dedupes_same_tx_id_across_patterns_of_same_code() -> None:
    """If a single doc has two PatternDTO entries with the same code
    that share a source_id (shouldn't happen in practice but the
    PatternAnalysis cache is structurally not unique-per-code), the
    same tx_id is not listed twice in the chip drill-down."""
    tx_a = _tx(tx_id=uuid4())
    analysis = _analysis_with_patterns(
        patterns=[
            PatternDTO(
                code="round_number_deposits",
                severity=15,
                detail="78% land on $100 multiples",
                source_ids=[tx_a.id],
            ),
            PatternDTO(
                code="round_number_deposits",
                severity=15,
                detail="80% land on $100 multiples",
                source_ids=[tx_a.id],
            ),
        ],
    )

    idx = PatternIndex.build_for_document(
        analysis=analysis,
        transactions=[tx_a],
        document_id=uuid4(),
        filename="x.pdf",
    )

    sources = idx.get("round_number_deposits")
    assert sources is not None
    assert len(sources) == 1


def test_build_for_document_empty_patterns_list_yields_empty_index() -> None:
    """Cache present but no patterns inside → empty index, no crash."""
    analysis = _analysis_with_patterns(patterns=[])

    idx = PatternIndex.build_for_document(
        analysis=analysis,
        transactions=[_tx()],
        document_id=uuid4(),
        filename="x.pdf",
    )

    assert idx.by_code == {}


# ---------------------------------------------------------------------------
# PatternIndex.build_for_merchant
# ---------------------------------------------------------------------------


def test_build_for_merchant_aggregates_across_docs_with_filename_tagging() -> None:
    """Two docs each contributing a different flag → merged index with
    each row tagged with its source filename."""
    tx_a = _tx(tx_id=uuid4(), description="MARCH WASH IN")
    tx_b = _tx(tx_id=uuid4(), description="APRIL DUP DEPOSIT")
    doc_a_id = uuid4()
    doc_b_id = uuid4()
    ctx_a = DocumentPatternContext(
        document_id=doc_a_id,
        filename="march.pdf",
        analysis=_analysis_with_patterns(
            patterns=[
                PatternDTO(
                    code="wash_deposit_suspected",
                    severity=35,
                    detail="x",
                    source_ids=[tx_a.id],
                ),
            ],
        ),
        transactions=[tx_a],
    )
    ctx_b = DocumentPatternContext(
        document_id=doc_b_id,
        filename="april.pdf",
        analysis=_analysis_with_patterns(
            patterns=[
                PatternDTO(
                    code="duplicate_deposits_detected",
                    severity=30,
                    detail="y",
                    source_ids=[tx_b.id],
                ),
            ],
        ),
        transactions=[tx_b],
    )

    idx = PatternIndex.build_for_merchant([ctx_a, ctx_b])

    wash = idx.get("wash_deposit_suspected")
    dup = idx.get("duplicate_deposits_detected")
    assert wash is not None
    assert dup is not None
    assert {s.filename for s in wash} == {"march.pdf"}
    assert {s.filename for s in dup} == {"april.pdf"}


def test_build_for_merchant_preserves_cross_doc_dupes_for_filename_tagging() -> None:
    """The same code firing in two docs with overlapping tx_ids (e.g.
    the same merchant's recurring round-number deposit pattern) keeps
    one row per (doc, tx_id) tuple — workers see both contributing
    uploads in the drill-down, distinguished by the filename column."""
    shared_tx_id = uuid4()
    tx_in_doc_a = _tx(tx_id=shared_tx_id, description="ROUND $500")
    tx_in_doc_b = _tx(tx_id=shared_tx_id, description="ROUND $500")
    doc_a_id = uuid4()
    doc_b_id = uuid4()
    ctx_a = DocumentPatternContext(
        document_id=doc_a_id,
        filename="march.pdf",
        analysis=_analysis_with_patterns(
            patterns=[
                PatternDTO(
                    code="round_number_deposits",
                    severity=15,
                    detail="x",
                    source_ids=[shared_tx_id],
                ),
            ],
        ),
        transactions=[tx_in_doc_a],
    )
    ctx_b = DocumentPatternContext(
        document_id=doc_b_id,
        filename="april.pdf",
        analysis=_analysis_with_patterns(
            patterns=[
                PatternDTO(
                    code="round_number_deposits",
                    severity=15,
                    detail="y",
                    source_ids=[shared_tx_id],
                ),
            ],
        ),
        transactions=[tx_in_doc_b],
    )

    idx = PatternIndex.build_for_merchant([ctx_a, ctx_b])

    sources = idx.get("round_number_deposits")
    assert sources is not None
    assert len(sources) == 2
    assert {s.filename for s in sources} == {"march.pdf", "april.pdf"}


def test_build_for_merchant_skips_docs_with_no_pattern_cache() -> None:
    """A doc whose AnalysisRow.pattern_analysis is None (legacy
    pre-chunk-2 row) contributes nothing to the index. Other docs in
    the merchant group still populate normally."""
    tx_b = _tx(tx_id=uuid4())
    ctx_legacy = DocumentPatternContext(
        document_id=uuid4(),
        filename="legacy.pdf",
        analysis=_analysis_without_pattern_cache(),
        transactions=[],
    )
    ctx_fresh = DocumentPatternContext(
        document_id=uuid4(),
        filename="fresh.pdf",
        analysis=_analysis_with_patterns(
            patterns=[
                PatternDTO(
                    code="mca_stacking",
                    severity=30,
                    detail="x",
                    source_ids=[tx_b.id],
                ),
            ],
        ),
        transactions=[tx_b],
    )

    idx = PatternIndex.build_for_merchant([ctx_legacy, ctx_fresh])

    assert list(idx.by_code.keys()) == ["mca_stacking"]
    sources = idx.get("mca_stacking")
    assert sources is not None
    assert {s.filename for s in sources} == {"fresh.pdf"}


def test_build_for_merchant_empty_contexts_yields_empty_index() -> None:
    assert PatternIndex.build_for_merchant([]).by_code == {}


# ---------------------------------------------------------------------------
# PatternIndex.get contract
# ---------------------------------------------------------------------------


def test_pattern_index_get_returns_none_for_unknown_code() -> None:
    """Unknown code → None, NOT empty list. Lets categorize_flags
    distinguish "no drill-down available" from "drill-down with zero
    rows" (which would be confusing UI)."""
    idx = PatternIndex.empty()

    assert idx.get("anything_unregistered") is None


def test_pattern_index_get_returns_none_for_empty_list() -> None:
    """An empty list in by_code (defensive, shouldn't happen in
    practice) is normalized to None on read so the template never
    sees an empty drill-down."""
    idx = PatternIndex(by_code={"some_code": []})

    assert idx.get("some_code") is None


# ---------------------------------------------------------------------------
# categorize_flags(pattern_index=...)
# ---------------------------------------------------------------------------


def test_categorize_flags_without_pattern_index_leaves_source_transactions_none() -> None:
    """Legacy behavior (pattern_index=None) must not change. Every
    HumanFlag emerges with source_transactions=None."""
    raw = [
        "[PATTERN] mca_stacking: 1 MCA position(s) detected",
        "[META] page_layer_anomaly: 2 page(s) have an off-mode /Contents stream count",
    ]
    result = categorize_flags(raw)

    for hf in result.by_category["stacking"]:
        assert hf.source_transactions is None
    for hf in result.by_category["tampering"]:
        assert hf.source_transactions is None


def test_categorize_flags_with_pattern_index_decorates_matching_codes() -> None:
    """Codes that match an index entry get source_transactions
    populated; codes that don't match (or don't exist in the index)
    stay undecorated."""
    tx_a = _tx(tx_id=uuid4())
    analysis = _analysis_with_patterns(
        patterns=[
            PatternDTO(
                code="mca_stacking",
                severity=30,
                detail="x",
                source_ids=[tx_a.id],
            ),
        ],
    )
    idx = PatternIndex.build_for_document(
        analysis=analysis,
        transactions=[tx_a],
        document_id=uuid4(),
        filename="march.pdf",
    )

    raw = [
        "[PATTERN] mca_stacking: 1 MCA position(s) detected",
        "[META] page_layer_anomaly: 2 page(s) have an off-mode /Contents stream count",
    ]
    result = categorize_flags(raw, pattern_index=idx)

    stacking_hf = result.by_category["stacking"][0]
    assert stacking_hf.code == "mca_stacking"
    assert stacking_hf.source_transactions is not None
    assert len(stacking_hf.source_transactions) == 1
    assert stacking_hf.source_transactions[0].filename == "march.pdf"

    tampering_hf = result.by_category["tampering"][0]
    assert tampering_hf.code == "page_layer_anomaly"
    assert tampering_hf.source_transactions is None


def test_categorize_flags_with_pattern_index_decorates_decline_class_flags() -> None:
    """A decline-class flag (e.g. wash_deposit_suspected) lifted to
    decline_class still picks up source_transactions from the index."""
    tx_a = _tx(tx_id=uuid4())
    analysis = _analysis_with_patterns(
        patterns=[
            PatternDTO(
                code="wash_deposit_suspected",
                severity=35,
                detail="x",
                source_ids=[tx_a.id],
            ),
        ],
    )
    idx = PatternIndex.build_for_document(
        analysis=analysis,
        transactions=[tx_a],
        document_id=uuid4(),
        filename="march.pdf",
    )

    result = categorize_flags(
        ["[PATTERN] wash_deposit_suspected: 2 round-trip pairs"],
        pattern_index=idx,
    )

    assert len(result.decline_class) == 1
    decline_hf = result.decline_class[0]
    assert decline_hf.source_transactions is not None
    assert decline_hf.source_transactions[0].document_id


def test_categorize_flags_empty_pattern_index_is_equivalent_to_none() -> None:
    """An empty PatternIndex (e.g. all merchant docs are legacy
    pre-chunk-2 rows) decorates nothing — chips look identical to the
    legacy pattern_index=None path."""
    idx_empty = PatternIndex.empty()

    result_empty = categorize_flags(
        ["[PATTERN] mca_stacking: 1 MCA position(s) detected"], pattern_index=idx_empty
    )
    result_none = categorize_flags(
        ["[PATTERN] mca_stacking: 1 MCA position(s) detected"]
    )

    assert (
        result_empty.by_category["stacking"][0].source_transactions
        == result_none.by_category["stacking"][0].source_transactions
        is None
    )
