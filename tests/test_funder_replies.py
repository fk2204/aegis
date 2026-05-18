"""Funder reply ingestion + outcome stamping tests (mp Phase 10).

Covers the four idempotency scenarios from the crystalline-swimming-
twilight plan refinement (5):

1. Reply arrives with an OPEN matching override → override stamped.
2. Reply arrives with an ALREADY-STAMPED matching override → reply
   persists; stamp NOT overwritten.
3. Reply arrives BEFORE the override exists → reply persists.
   Override created later → back-stamped from the latest reply.
4. Two concurrent replies (e.g. webhook + operator paste both fired)
   → only the first stamp lands; the second sees outcome IS NOT NULL
   and returns False (the in-memory repo and the SQL UPDATE WHERE
   outcome IS NULL share the same idempotency contract).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.funders.replies import (
    STATUS_TO_OUTCOME,
    FunderReplyPayload,
    InMemoryFunderReplyRepository,
    ReplyTerms,
    ingest_reply,
    parse_terms_from_blob,
    stamp_override_from_replies,
    validate_reply,
)

NOW = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)


def _approved_terms(
    *,
    amount: str = "20000.00",
    factor: str = "1.32",
    payback: str | None = "26400.00",
) -> ReplyTerms:
    return ReplyTerms(
        amount=Decimal(amount),
        factor=Decimal(factor),
        payback=Decimal(payback) if payback else None,
    )


def _payload(
    *,
    deal_id: UUID | None = None,
    funder_id: UUID | None = None,
    status: str = "approved",
    terms: ReplyTerms | None = None,
    raw_text: str = "Funder reply body",
    ingested_via: str = "webhook",
) -> FunderReplyPayload:
    return FunderReplyPayload(
        deal_id=deal_id or uuid4(),
        funder_id=funder_id or uuid4(),
        status=status,
        raw_text=raw_text,
        ingested_via=ingested_via,
        terms=terms or _approved_terms(),
        parsed_confidence=85,
    )


def _seed_open_override(
    repo: InMemoryFunderReplyRepository, deal_id: UUID, *, override_id: UUID | None = None
) -> UUID:
    """Helper: insert a synthetic override row with outcome=NULL."""
    override_id = override_id or uuid4()
    repo.add_override(
        {
            "id": str(override_id),
            "deal_id": str(deal_id),
            "outcome": None,
            "created_at": NOW.isoformat(),
        }
    )
    return override_id


# ---------------------------------------------------------------------------
# Validation gate (deterministic)
# ---------------------------------------------------------------------------


def test_validate_passes_on_tied_out_approved_terms() -> None:
    result = validate_reply(_payload(terms=_approved_terms()))
    assert result.passed
    assert result.failures == []


def test_validate_fails_on_amount_factor_payback_mismatch() -> None:
    """Approved reply with wrong math → gate fails."""
    bad_terms = ReplyTerms(
        amount=Decimal("20000.00"),
        factor=Decimal("1.32"),
        payback=Decimal("99999.00"),  # nowhere near amount * factor
    )
    result = validate_reply(_payload(terms=bad_terms))
    assert not result.passed
    assert any("amount_factor_payback_mismatch" in f for f in result.failures)


def test_validate_warns_when_required_fields_missing() -> None:
    """Approved reply lacking enough fields to reconcile → warning,
    not a failure. The operator hand-corrects."""
    result = validate_reply(
        _payload(terms=ReplyTerms(amount=Decimal("20000.00")))
    )
    assert result.passed
    assert any("missing_terms_for_reconcile" in w for w in result.warnings)


def test_validate_skips_math_for_declined_and_countered() -> None:
    """Declined / countered replies don't need math reconciliation —
    they advertise non-approval and the structured terms are advisory."""
    for status in ("declined", "countered"):
        result = validate_reply(_payload(status=status, terms=ReplyTerms()))
        assert result.passed, f"{status} should pass without terms"


# ---------------------------------------------------------------------------
# Refinement (5) scenario 1: reply with OPEN override → stamped
# ---------------------------------------------------------------------------


def test_reply_stamps_open_override(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    override_id = _seed_open_override(repo, deal_id)

    payload = _payload(deal_id=deal_id, status="approved")
    result = ingest_reply(payload, repo=repo, audit=audit, now=NOW)

    # Reply persisted.
    assert isinstance(result.reply_id, UUID)
    # Override stamped with the matching outcome.
    assert result.stamped_override_id == override_id
    stamped_row = next(r for r in repo.overrides() if r["id"] == str(override_id))
    assert stamped_row["outcome"] == "funded"
    assert stamped_row["outcome_recorded_at"] is not None


def test_declined_reply_stamps_override_declined_by_funder() -> None:
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    override_id = _seed_open_override(repo, deal_id)

    result = ingest_reply(
        _payload(deal_id=deal_id, status="declined", terms=ReplyTerms()),
        repo=repo,
        audit=audit,
        now=NOW,
    )
    assert result.stamped_override_id == override_id
    stamped_row = next(r for r in repo.overrides() if r["id"] == str(override_id))
    assert stamped_row["outcome"] == "declined_by_funder"


def test_countered_reply_does_not_stamp() -> None:
    """Counter-offers require operator acceptance before stamping;
    the follow-up reply (after operator accepts/declines the counter)
    is what stamps the outcome."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    _ = _seed_open_override(repo, deal_id)

    result = ingest_reply(
        _payload(deal_id=deal_id, status="countered", terms=ReplyTerms()),
        repo=repo,
        audit=audit,
        now=NOW,
    )
    assert result.stamped_override_id is None
    open_rows = [r for r in repo.overrides() if r.get("outcome") is None]
    assert len(open_rows) == 1  # override still open


# ---------------------------------------------------------------------------
# Refinement (5) scenario 2: ALREADY-STAMPED override → no overwrite
# ---------------------------------------------------------------------------


def test_second_reply_does_not_overwrite_stamp() -> None:
    """First reply approves → stamp 'funded'. Second reply declines
    → reply persists but the original 'funded' stamp survives. This
    is the symmetric "first reply wins" rule."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    override_id = _seed_open_override(repo, deal_id)

    ingest_reply(
        _payload(deal_id=deal_id, status="approved"),
        repo=repo,
        audit=audit,
        now=NOW,
    )
    later = NOW + timedelta(hours=2)
    result2 = ingest_reply(
        _payload(deal_id=deal_id, status="declined", terms=ReplyTerms()),
        repo=repo,
        audit=audit,
        now=later,
    )

    # Second reply persisted but did NOT stamp (override already stamped).
    assert result2.stamped_override_id is None
    assert len(repo.replies()) == 2
    stamped_row = next(r for r in repo.overrides() if r["id"] == str(override_id))
    assert stamped_row["outcome"] == "funded"  # original stamp preserved


# ---------------------------------------------------------------------------
# Refinement (5) scenario 3: reply BEFORE override → back-stamped on
# override creation
# ---------------------------------------------------------------------------


def test_reply_before_override_back_stamps_on_override_creation() -> None:
    """A reply arrives, persists, but no override exists yet. Later
    when the override lands, the override-creation path calls
    stamp_override_from_replies which looks up the most-recent reply
    and stamps the override."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()

    # Step 1: reply lands with no override in flight.
    ingest_reply(
        _payload(deal_id=deal_id, status="approved"), repo=repo, audit=audit, now=NOW
    )
    assert len(repo.replies()) == 1
    assert all(r.get("outcome") is None for r in repo.overrides())  # no overrides yet

    # Step 2: override lands later.
    override_id = _seed_open_override(repo, deal_id)
    stamped = stamp_override_from_replies(
        override_id=override_id,
        deal_id=deal_id,
        repo=repo,
        audit=audit,
        now=NOW + timedelta(hours=1),
    )
    assert stamped == override_id
    stamped_row = next(r for r in repo.overrides() if r["id"] == str(override_id))
    assert stamped_row["outcome"] == "funded"


# ---------------------------------------------------------------------------
# Refinement (5) scenario 4: concurrent stamp attempts (idempotency)
# ---------------------------------------------------------------------------


def test_concurrent_stamp_attempt_returns_false_on_second() -> None:
    """Two callers attempt to stamp the same override at the same
    time. The repo's "WHERE outcome IS NULL" guard means only one
    write succeeds. In-memory repo returns False on the second; the
    Supabase impl returns the same shape via .update(...).is_('outcome',
    'null')."""
    repo = InMemoryFunderReplyRepository()
    deal_id = uuid4()
    override_id = _seed_open_override(repo, deal_id)
    first = repo.stamp_override_outcome(
        override_id, outcome="funded", stamped_at=NOW
    )
    second = repo.stamp_override_outcome(
        override_id, outcome="declined_by_funder", stamped_at=NOW
    )
    assert first is True
    assert second is False
    stamped_row = next(r for r in repo.overrides() if r["id"] == str(override_id))
    assert stamped_row["outcome"] == "funded"  # first wins


# ---------------------------------------------------------------------------
# Reply persistence on validation failure (refinement: still capture)
# ---------------------------------------------------------------------------


def test_validation_failure_still_persists_reply_at_zero_confidence() -> None:
    """An approved reply with broken math doesn't get to stamp, but the
    raw inbound STILL persists with parsed_confidence=0 so the operator
    sees it on the dashboard and hand-corrects."""
    repo = InMemoryFunderReplyRepository()
    audit = InMemoryAuditLog()
    deal_id = uuid4()
    _seed_open_override(repo, deal_id)

    bad_terms = ReplyTerms(
        amount=Decimal("20000.00"),
        factor=Decimal("1.32"),
        payback=Decimal("999.00"),  # wrong
    )
    result = ingest_reply(
        _payload(deal_id=deal_id, terms=bad_terms),
        repo=repo,
        audit=audit,
        now=NOW,
    )
    assert not result.validation.passed
    assert result.stamped_override_id is None  # no stamp on failed reconcile
    assert len(repo.replies()) == 1
    persisted = repo.replies()[0]
    assert persisted["parsed_confidence"] == 0  # lowered per the docstring
    # Override remains OPEN — operator can correct and re-submit.
    open_rows = [r for r in repo.overrides() if r.get("outcome") is None]
    assert len(open_rows) == 1


# ---------------------------------------------------------------------------
# Mapping discipline
# ---------------------------------------------------------------------------


def test_status_to_outcome_mapping_is_exhaustive_for_reply_status() -> None:
    """Every ReplyStatus literal must appear in the mapping. A new
    status without a mapping entry would silently fail to stamp."""
    expected = {"approved", "declined", "countered"}
    assert set(STATUS_TO_OUTCOME.keys()) == expected


# ---------------------------------------------------------------------------
# Terms parsing helper
# ---------------------------------------------------------------------------


def test_parse_terms_from_blob_handles_clean_json() -> None:
    terms = parse_terms_from_blob('{"amount": "10000.00", "factor": "1.30"}')
    assert terms.amount == Decimal("10000.00")
    assert terms.factor == Decimal("1.30")


def test_parse_terms_from_blob_returns_empty_on_garbage() -> None:
    """Garbage input → empty terms (operator hand-fills). Never raise
    on a malformed paste — the row should still persist."""
    terms = parse_terms_from_blob("not json at all")
    assert terms.amount is None
    assert terms.factor is None


def test_parse_terms_rejects_float_input_via_str_coercion() -> None:
    """Float values that sneak through JSON parse are str()'d before
    Pydantic sees them — preserves precision per CLAUDE.md."""
    # JSON numbers are floats in Python by default; verify the coercion path.
    payload: dict[str, Any] = {"amount": 10000.50}
    terms = parse_terms_from_blob('{"amount": 10000.50}')
    assert terms.amount == Decimal("10000.5")
    _ = payload  # silence ruff
