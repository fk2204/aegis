"""Shadow-disagreement repository tests (R1.6 Step 2 cutover prep).

Covers ``scoring_v2/shadow_disagreements.py``:
  * the in-memory repo round-trips a record with all expected fields
  * idempotency: same (merchant, day, category, evidence) does NOT
    create a duplicate row
  * category enum coverage: all five CAT_* values accepted; everything
    else rejected at the API boundary
  * triage decision enum coverage on the operator-side update path
  * evidence hash is stable across dict-key ordering
  * ``list_open()`` returns only un-triaged rows

Migration-level coverage (column existence, view shape) lives in
``tests/compliance/test_migrations.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from aegis.scoring_v2.shadow_disagreements import (
    ALLOWED_CATEGORIES,
    ALLOWED_TRIAGE_DECISIONS,
    CATEGORY_AGREEMENT,
    CATEGORY_AMBIGUOUS,
    CATEGORY_INSUFFICIENT,
    CATEGORY_NEW_BETTER,
    CATEGORY_OLD_BETTER,
    InMemoryScoringDisagreementRepository,
    ScoringDisagreementRecord,
    _evidence_hash,
    record_disagreement,
)

_MERCHANT_A = UUID("11111111-1111-4111-8111-111111111111")
_MERCHANT_B = UUID("22222222-2222-4222-8222-222222222222")
_DEAL_A = UUID("33333333-3333-4333-8333-333333333333")
_FIXED_TS = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)


def _baseline_kwargs() -> dict[str, object]:
    return {
        "merchant_id": _MERCHANT_A,
        "deal_id": _DEAL_A,
        "category": CATEGORY_OLD_BETTER,
        "legacy_fraud_score": 75,
        "legacy_tier": "F",
        "legacy_recommendation": "decline",
        "legacy_hard_declines": ["fraud_score_critical"],
        "track_a_verdict": "clean",
        "track_b_band": "low",
        "track_c_panel": {
            "revenue_basis": "120000.00",
            "international_share_pct": 0.0,
        },
        "evidence": {
            "rationale": "live declined on fraud_score but new tracks clean",
            "live_hard_reasons": ["fraud_score_critical"],
            "track_b_factors": [],
        },
        "comparison_run_at": _FIXED_TS,
    }


# ---------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------


def test_round_trip_persists_every_field() -> None:
    repo = InMemoryScoringDisagreementRepository()
    record = record_disagreement(repo, **_baseline_kwargs())  # type: ignore[arg-type]

    assert isinstance(record, ScoringDisagreementRecord)
    assert record.merchant_id == _MERCHANT_A
    assert record.deal_id == _DEAL_A
    assert record.category == CATEGORY_OLD_BETTER
    assert record.legacy_fraud_score == 75
    assert record.legacy_tier == "F"
    assert record.legacy_recommendation == "decline"
    assert record.legacy_hard_declines == ["fraud_score_critical"]
    assert record.track_a_verdict == "clean"
    assert record.track_b_band == "low"
    assert record.track_c_panel == {
        "revenue_basis": "120000.00",
        "international_share_pct": 0.0,
    }
    assert record.evidence is not None
    assert record.evidence["rationale"].startswith("live declined")
    assert record.triaged_at is None
    assert record.triaged_by is None
    assert record.triage_decision is None
    assert record.triage_notes is None
    assert isinstance(record.comparison_run_at, datetime)
    assert record.comparison_run_at.tzinfo is not None
    assert len(repo.rows) == 1


def test_round_trip_accepts_null_deal_id() -> None:
    """Many shadow-comparison merchants have no decisioned deal."""
    repo = InMemoryScoringDisagreementRepository()
    kwargs = _baseline_kwargs()
    kwargs["deal_id"] = None
    record = record_disagreement(repo, **kwargs)  # type: ignore[arg-type]
    assert record.deal_id is None


def test_round_trip_accepts_null_legacy_and_track_sides() -> None:
    """``insufficient-new-data`` row has neither legacy nor new signals."""
    repo = InMemoryScoringDisagreementRepository()
    kwargs = _baseline_kwargs()
    kwargs["category"] = CATEGORY_INSUFFICIENT
    kwargs["legacy_fraud_score"] = None
    kwargs["legacy_tier"] = None
    kwargs["legacy_recommendation"] = None
    kwargs["legacy_hard_declines"] = None
    kwargs["track_a_verdict"] = None
    kwargs["track_b_band"] = None
    kwargs["track_c_panel"] = None
    record = record_disagreement(repo, **kwargs)  # type: ignore[arg-type]
    assert record.category == CATEGORY_INSUFFICIENT
    assert record.legacy_fraud_score is None
    assert record.track_a_verdict is None


# ---------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------


def test_idempotent_on_same_merchant_day_category_evidence() -> None:
    """Re-running --persist on the same day with the same evidence is a no-op."""
    repo = InMemoryScoringDisagreementRepository()
    first = record_disagreement(repo, **_baseline_kwargs())  # type: ignore[arg-type]
    second = record_disagreement(repo, **_baseline_kwargs())  # type: ignore[arg-type]
    assert first.id == second.id
    assert len(repo.rows) == 1


def test_idempotency_treats_evidence_dict_ordering_as_equal() -> None:
    """Two evidence dicts with different key order MUST hash equal —
    otherwise the operator could double-insert just by reordering fields."""
    repo = InMemoryScoringDisagreementRepository()
    kwargs_a = _baseline_kwargs()
    kwargs_a["evidence"] = {"a": 1, "b": [1, 2, 3], "c": None}
    kwargs_b = _baseline_kwargs()
    kwargs_b["evidence"] = {"c": None, "b": [1, 2, 3], "a": 1}
    first = record_disagreement(repo, **kwargs_a)  # type: ignore[arg-type]
    second = record_disagreement(repo, **kwargs_b)  # type: ignore[arg-type]
    assert first.id == second.id
    assert len(repo.rows) == 1


def test_evidence_hash_is_stable_across_key_order() -> None:
    """Directly exercise the hash helper — equal content, equal hash."""
    a = {"x": 1, "y": [2, 3], "z": {"nested": True}}
    b = {"z": {"nested": True}, "y": [2, 3], "x": 1}
    assert _evidence_hash(a) == _evidence_hash(b)


def test_evidence_hash_changes_when_content_changes() -> None:
    """Sanity: the hash distinguishes meaningfully-different evidence."""
    a = {"factors": ["intl"]}
    b = {"factors": ["nsf"]}
    assert _evidence_hash(a) != _evidence_hash(b)


def test_idempotency_distinguishes_different_categories() -> None:
    """Same merchant, same day, different category => two rows."""
    repo = InMemoryScoringDisagreementRepository()
    kwargs_a = _baseline_kwargs()
    kwargs_a["category"] = CATEGORY_OLD_BETTER
    kwargs_b = _baseline_kwargs()
    kwargs_b["category"] = CATEGORY_NEW_BETTER
    record_disagreement(repo, **kwargs_a)  # type: ignore[arg-type]
    record_disagreement(repo, **kwargs_b)  # type: ignore[arg-type]
    assert len(repo.rows) == 2


def test_idempotency_distinguishes_different_merchants() -> None:
    repo = InMemoryScoringDisagreementRepository()
    kwargs_a = _baseline_kwargs()
    kwargs_a["merchant_id"] = _MERCHANT_A
    kwargs_b = _baseline_kwargs()
    kwargs_b["merchant_id"] = _MERCHANT_B
    record_disagreement(repo, **kwargs_a)  # type: ignore[arg-type]
    record_disagreement(repo, **kwargs_b)  # type: ignore[arg-type]
    assert len(repo.rows) == 2


def test_idempotency_distinguishes_different_calendar_days() -> None:
    """Daily nightly runs land separate rows even when content is identical."""
    repo = InMemoryScoringDisagreementRepository()
    kwargs_a = _baseline_kwargs()
    kwargs_a["comparison_run_at"] = _FIXED_TS
    kwargs_b = _baseline_kwargs()
    kwargs_b["comparison_run_at"] = _FIXED_TS + timedelta(days=1)
    record_disagreement(repo, **kwargs_a)  # type: ignore[arg-type]
    record_disagreement(repo, **kwargs_b)  # type: ignore[arg-type]
    assert len(repo.rows) == 2


# ---------------------------------------------------------------------
# Category enum coverage
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "category",
    [
        CATEGORY_AGREEMENT,
        CATEGORY_NEW_BETTER,
        CATEGORY_OLD_BETTER,
        CATEGORY_AMBIGUOUS,
        CATEGORY_INSUFFICIENT,
    ],
)
def test_every_category_value_is_accepted(category: str) -> None:
    """All five CAT_* constants from the comparison script persist cleanly."""
    repo = InMemoryScoringDisagreementRepository()
    kwargs = _baseline_kwargs()
    kwargs["category"] = category
    # Vary the merchant id so all five rows land independent of idempotency.
    kwargs["merchant_id"] = uuid4()
    record = record_disagreement(repo, **kwargs)  # type: ignore[arg-type]
    assert record.category == category


def test_unknown_category_is_rejected() -> None:
    repo = InMemoryScoringDisagreementRepository()
    kwargs = _baseline_kwargs()
    kwargs["category"] = "made-up-bucket"
    with pytest.raises(ValueError, match="category must be one of"):
        record_disagreement(repo, **kwargs)  # type: ignore[arg-type]


def test_allowed_categories_set_matches_constants() -> None:
    """Defensive: ALLOWED_CATEGORIES must enumerate exactly the five CAT_*."""
    assert ALLOWED_CATEGORIES == frozenset(
        {
            CATEGORY_AGREEMENT,
            CATEGORY_NEW_BETTER,
            CATEGORY_OLD_BETTER,
            CATEGORY_AMBIGUOUS,
            CATEGORY_INSUFFICIENT,
        }
    )


def test_allowed_triage_decisions_match_spec() -> None:
    """The operator-side decision enum is one of four values."""
    assert ALLOWED_TRIAGE_DECISIONS == frozenset(
        {"accept-new", "accept-old", "both-valid", "needs-rule-change"}
    )


# ---------------------------------------------------------------------
# list_open() — triage queue
# ---------------------------------------------------------------------


def test_list_open_returns_untriaged_rows_only() -> None:
    """Once a row is marked triaged, list_open() must not surface it."""
    repo = InMemoryScoringDisagreementRepository()
    a = record_disagreement(
        repo,
        **{**_baseline_kwargs(), "merchant_id": uuid4()},  # type: ignore[arg-type]
    )
    b = record_disagreement(
        repo,
        **{**_baseline_kwargs(), "merchant_id": uuid4()},  # type: ignore[arg-type]
    )
    # Triage one of them in-place — the in-memory repo stores live refs.
    b.triaged_at = datetime.now(UTC)
    b.triaged_by = "filip"
    b.triage_decision = "accept-new"

    open_rows = repo.list_open()
    open_ids = {r.id for r in open_rows}
    assert a.id in open_ids
    assert b.id not in open_ids


def test_list_open_initial_state_returns_all_records() -> None:
    """A fresh repo with N records returns all N — none are triaged yet."""
    repo = InMemoryScoringDisagreementRepository()
    record_disagreement(repo, **{**_baseline_kwargs(), "merchant_id": uuid4()})  # type: ignore[arg-type]
    record_disagreement(repo, **{**_baseline_kwargs(), "merchant_id": uuid4()})  # type: ignore[arg-type]
    record_disagreement(repo, **{**_baseline_kwargs(), "merchant_id": uuid4()})  # type: ignore[arg-type]
    assert len(repo.list_open()) == 3


# ---------------------------------------------------------------------
# Default timestamp
# ---------------------------------------------------------------------


def test_comparison_run_at_defaults_to_now() -> None:
    """When the caller omits ``comparison_run_at``, the repo stamps it now."""
    repo = InMemoryScoringDisagreementRepository()
    kwargs = _baseline_kwargs()
    del kwargs["comparison_run_at"]
    before = datetime.now(UTC)
    record = record_disagreement(repo, **kwargs)  # type: ignore[arg-type]
    after = datetime.now(UTC)
    assert before <= record.comparison_run_at <= after


# ---------------------------------------------------------------------
# Defensive: extra kwarg rejected (Pydantic extra='forbid')
# ---------------------------------------------------------------------


def test_record_model_rejects_unknown_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ScoringDisagreementRecord(
            id=uuid4(),
            merchant_id=_MERCHANT_A,
            deal_id=None,
            comparison_run_at=_FIXED_TS,
            legacy_fraud_score=None,
            legacy_tier=None,
            legacy_recommendation=None,
            legacy_hard_declines=None,
            track_a_verdict=None,
            track_b_band=None,
            track_c_panel=None,
            category=CATEGORY_AGREEMENT,
            evidence=None,
            something_unknown="should fail",  # type: ignore[call-arg]
        )


def test_record_model_accepts_decimal_in_track_c_panel() -> None:
    """Track C carries revenue_basis as a string-serialised Decimal."""
    repo = InMemoryScoringDisagreementRepository()
    kwargs = _baseline_kwargs()
    kwargs["track_c_panel"] = {
        "revenue_basis": str(Decimal("12345.67")),
        "international_share_pct": 17.5,
    }
    record = record_disagreement(repo, **kwargs)  # type: ignore[arg-type]
    assert record.track_c_panel is not None
    assert record.track_c_panel["revenue_basis"] == "12345.67"
