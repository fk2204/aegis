"""Tests for the merchant-detail bundling helpers in ``aegis.web.router``.

Bundling groups a merchant's statements by ``(bank_name, account_last4)``
so the scoring window doesn't mix accounts. Tests cover:

  * default bundle is the most-populated bank/last4 pair
  * explicit bundle filter overrides the default
  * pre-migration analyses (NULL bank/last4) bundle together
  * single-statement merchants behave identically to pre-bundling code
  * bundle switcher options reflect every (bank, last4) the merchant has
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from aegis.storage import (
    AnalysisRow,
    DocumentRow,
    InMemoryDocumentRepository,
)
from aegis.web.router import (
    _bundle_keys_for_merchant,
    _collect_analyzed_for_merchant,
    _select_default_bundle,
)


def _seed(
    repo: InMemoryDocumentRepository,
    *,
    merchant_id: UUID,
    bank_name: str | None,
    account_last4: str | None,
    period_start: date,
    period_end: date,
    uploaded_offset_days: int = 0,
) -> tuple[DocumentRow, AnalysisRow]:
    """Seed one (document, analysis) pair into the in-memory repo."""
    doc = DocumentRow(
        id=uuid4(),
        file_hash=f"hash-{uuid4().hex}",
        byte_size=2048,
        original_filename=f"{bank_name or 'unknown'}_{period_start}.pdf",
        merchant_id=merchant_id,
        parse_status="proceed",
        fraud_score=5,
        uploaded_at=datetime.now(UTC) - timedelta(days=uploaded_offset_days),
    )
    repo._docs[doc.id] = doc

    analysis = AnalysisRow(
        id=uuid4(),
        document_id=doc.id,
        merchant_id=merchant_id,
        statement_period_start=period_start,
        statement_period_end=period_end,
        statement_days=(period_end - period_start).days,
        beginning_balance=Decimal("10000.00"),
        ending_balance=Decimal("11000.00"),
        avg_daily_balance=Decimal("15000.00"),
        true_revenue=Decimal("30000.00"),
        monthly_revenue=Decimal("30000.00"),
        lowest_balance=Decimal("1000.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.10"),
        payroll_detected=True,
        bank_name=bank_name,
        account_last4=account_last4,
    )
    repo._analyses[doc.id] = analysis
    return doc, analysis


def test_default_bundle_is_most_populated() -> None:
    """Three Chase statements + one Wells Fargo should default to Chase."""
    repo = InMemoryDocumentRepository()
    merchant_id = uuid4()
    for i, start in enumerate(
        [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]
    ):
        _seed(
            repo,
            merchant_id=merchant_id,
            bank_name="Chase",
            account_last4="1234",
            period_start=start,
            period_end=start.replace(day=28),
            uploaded_offset_days=90 - i * 30,
        )
    _seed(
        repo,
        merchant_id=merchant_id,
        bank_name="Wells Fargo",
        account_last4="9999",
        period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 28),
        uploaded_offset_days=0,
    )

    items = _collect_analyzed_for_merchant(repo, merchant_id)
    assert len(items) == 3, "default should bundle the three Chase statements"
    assert {a.bank_name for _, a in items} == {"Chase"}
    assert {a.account_last4 for _, a in items} == {"1234"}


def test_explicit_bundle_filter_selects_other_account() -> None:
    """Operator switching to Wells Fargo bundle gets only that account's statements."""
    repo = InMemoryDocumentRepository()
    merchant_id = uuid4()
    for start in [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]:
        _seed(
            repo, merchant_id=merchant_id, bank_name="Chase",
            account_last4="1234", period_start=start,
            period_end=start.replace(day=28),
        )
    _seed(
        repo, merchant_id=merchant_id, bank_name="Wells Fargo",
        account_last4="9999", period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 28),
    )

    items = _collect_analyzed_for_merchant(
        repo, merchant_id, bundle=("Wells Fargo", "9999")
    )
    assert len(items) == 1
    _, analysis = items[0]
    assert analysis.bank_name == "Wells Fargo"
    assert analysis.account_last4 == "9999"


def test_pre_migration_null_bundle_groups_together() -> None:
    """Pre-migration analyses (bank_name=None, account_last4=None) all bundle as one."""
    repo = InMemoryDocumentRepository()
    merchant_id = uuid4()
    for start in [date(2026, 1, 1), date(2026, 2, 1)]:
        _seed(
            repo, merchant_id=merchant_id, bank_name=None,
            account_last4=None, period_start=start,
            period_end=start.replace(day=28),
        )

    items = _collect_analyzed_for_merchant(repo, merchant_id)
    assert len(items) == 2
    assert all(a.bank_name is None and a.account_last4 is None for _, a in items)


def test_single_statement_returns_one_item() -> None:
    """Single-statement merchant must behave identically to pre-bundling code."""
    repo = InMemoryDocumentRepository()
    merchant_id = uuid4()
    _seed(
        repo, merchant_id=merchant_id, bank_name="Chase",
        account_last4="1234", period_start=date(2026, 3, 1),
        period_end=date(2026, 3, 31),
    )

    items = _collect_analyzed_for_merchant(repo, merchant_id)
    assert len(items) == 1


def test_no_analyses_returns_empty() -> None:
    repo = InMemoryDocumentRepository()
    assert _collect_analyzed_for_merchant(repo, uuid4()) == []


def test_bundle_keys_for_merchant_counts_and_orders_by_population() -> None:
    """Most-populated bundle ranks first; ties broken by latest period_end."""
    repo = InMemoryDocumentRepository()
    merchant_id = uuid4()
    for start in [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]:
        _seed(
            repo, merchant_id=merchant_id, bank_name="Chase",
            account_last4="1234", period_start=start,
            period_end=start.replace(day=28),
        )
    _seed(
        repo, merchant_id=merchant_id, bank_name="Wells Fargo",
        account_last4="9999", period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),  # newer than the Chase bundle
    )

    all_items = _collect_analyzed_for_merchant(
        repo, merchant_id, window=999, bundle=None
    )
    # Force the function to enumerate everything for the bundle helper to inspect.
    keys = _bundle_keys_for_merchant(
        [(d, repo._analyses[d.id]) for d in repo._docs.values() if d.merchant_id == merchant_id]
    )
    assert keys[0][0] == ("Chase", "1234")
    assert keys[0][1] == 3
    assert keys[1][0] == ("Wells Fargo", "9999")
    assert keys[1][1] == 1
    # And the collector's default bundle is also Chase.
    assert len(all_items) >= 1
    assert _select_default_bundle(
        [(d, repo._analyses[d.id]) for d in repo._docs.values() if d.merchant_id == merchant_id]
    ) == ("Chase", "1234")


def test_window_cap_applies_within_a_bundle() -> None:
    """Five Chase statements + default window=3 → 3 items returned, all Chase."""
    repo = InMemoryDocumentRepository()
    merchant_id = uuid4()
    for i, start in enumerate(
        [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1),
         date(2026, 2, 1), date(2026, 3, 1)]
    ):
        _seed(
            repo, merchant_id=merchant_id, bank_name="Chase",
            account_last4="1234", period_start=start,
            period_end=start.replace(day=28),
            uploaded_offset_days=150 - i * 30,
        )

    items = _collect_analyzed_for_merchant(repo, merchant_id)
    assert len(items) == 3, "default window caps to 3 even when more are available"
    # Newest-first ordering preserved (caller sees uploaded_at desc).
    starts = [a.statement_period_start for _, a in items]
    assert starts == sorted(starts, reverse=True)
