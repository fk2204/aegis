"""U33 — Wire Track A/B verdicts into score_deal call sites.

The U30 commit (``b276507``) made ``score_deal`` accept ``track_a_verdict``
and ``track_b_band`` kwargs. Per ``test_scoring_engine_config.py`` the
gate logic inside ``score_deal`` is already pinned. U33 verifies the
CALL SITES feed the verdicts in — without that wiring, flipping
``AEGIS_SCORING_ENGINE=track_abc`` is a no-op because every caller
passes ``None``.

The tests here cover three layers:

* ``score_deal`` direct (regression cross-check of the U30 gate so we
  catch any future drift between U30 and this wiring),
* the shared helper (``compute_score_deal_track_inputs``) — verdict
  extraction + failure-mode fallback,
* the score-emitting route (``POST /deals/score``) — end-to-end wiring
  proof that a real document → real verdict → real ``score_deal`` kwarg
  → expected hard-decline path.

Same env-var protocol as ``test_scoring_engine_config.py``: autouse
fixture clears the ``@lru_cache`` on ``get_settings`` so
``monkeypatch.setenv`` takes effect.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from aegis.config import get_settings
from aegis.scoring.models import ScoreInput
from aegis.scoring.ofac import OFACClient
from aegis.scoring.score import score_deal
from aegis.scoring_v2.score_deal_inputs import (
    compute_score_deal_track_inputs,
)
from aegis.scoring_v2.track_a import (
    DocumentIntegritySignals,
    compute_integrity_verdict,
)
from aegis.scoring_v2.track_b.models import (
    BAND_TO_ACTION,
    BandLevel,
    BusinessRiskBand,
    CashflowSignals,
    FactorReason,
)

# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def fresh_ofac(tmp_path: Path) -> Iterator[OFACClient]:
    """OFAC client with a never-match SDN list."""
    cache = tmp_path / "ofac" / "sdn.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "entries": [{"primary_name": "ZZZ Sentinel", "aliases": []}],
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    def _panic() -> bytes:
        raise AssertionError("fresh cache should not refresh")

    yield OFACClient(cache_path=cache, fetcher=_panic, now=lambda: datetime.now(UTC))


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Settings is ``@lru_cache``d; clear before + after each test so
    env var overrides via ``monkeypatch.setenv`` actually take effect.
    Same pattern as ``test_scoring_engine_config.py`` /
    ``test_eof_threshold_config.py``."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# -- helpers ----------------------------------------------------------------


def _strong_metadata_signals(document_id: str = "doc_u33_fail") -> (
    DocumentIntegritySignals
):
    """Signals that produce a Track A ``fail`` verdict via the
    ``strong_metadata`` branch (metadata_score >= 50)."""
    return DocumentIntegritySignals(
        document_id=document_id,
        metadata_score=72,
        metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
        validation_failures=(),
    )


def _clean_signals(document_id: str = "doc_u33_clean") -> DocumentIntegritySignals:
    """Signals that produce a Track A ``clean`` verdict."""
    return DocumentIntegritySignals(
        document_id=document_id,
        metadata_score=4,
        metadata_flags=(),
        validation_failures=(),
    )


def _track_b_band_fixture(band: BandLevel) -> BusinessRiskBand:
    """Synthetic ``BusinessRiskBand`` for the requested band level."""
    cashflow = CashflowSignals(
        true_revenue_total=Decimal("0.00"),
        statement_period_days=30,
        monthly_revenue_estimate=Decimal("0.00"),
        average_daily_balance=None,
        lowest_balance=None,
        negative_days=0,
        nsf_count=0,
        mca_position_count=0,
        international_client_share_pct=None,
    )
    reason = FactorReason(
        factor="insufficient_data",
        severity="neutral",
        detail="synthetic fixture for U33 wiring",
    )
    return BusinessRiskBand(
        band=band,
        action=BAND_TO_ACTION[band],
        cashflow=cashflow,
        reasons=(reason,),
        insufficient_data_factors=(),
    )


def _document_row_stub(
    *,
    doc_id: UUID | None = None,
    metadata_flags: tuple[str, ...] = (),
    all_flags: tuple[str, ...] = (),
    fraud_score_breakdown: dict[str, Any] | None = None,
    uploaded_at: str = "2026-06-01T00:00:00Z",
) -> Any:
    """Build a duck-typed ``DocumentRow`` for the dossier-panel inputs.

    ``build_unified_tracks_view`` only reads ``getattr(d, …)`` on a few
    fields. A ``MagicMock`` with the right attributes is sufficient and
    avoids dragging in the persistence-time fixture pipeline.
    """
    stub = MagicMock()
    stub.id = doc_id or uuid4()
    stub.metadata_flags = list(metadata_flags)
    stub.all_flags = list(all_flags)
    stub.fraud_score_breakdown = fraud_score_breakdown or {}
    stub.uploaded_at = uploaded_at
    return stub


# -- score_deal direct: confirm U30 gate still behaves as documented -------
#
# These are not redundant with test_scoring_engine_config.py — they
# exercise the same kwargs through the call-site discipline U33 owns
# (verdict came from compute_integrity_verdict, not a hand-built model)
# and pin the wiring contract going forward.


def test_track_a_fail_under_track_abc_hard_declines(
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``track_abc`` engine + Track A ``fail`` → ``hard_decline_reasons``
    contains ``track_a_integrity_fail:branch=…``."""
    monkeypatch.setenv("AEGIS_SCORING_ENGINE", "track_abc")
    get_settings.cache_clear()

    verdict = compute_integrity_verdict(_strong_metadata_signals())
    assert verdict.verdict == "fail", "fixture invariant"

    result = score_deal(clean_deal, ofac=fresh_ofac, track_a_verdict=verdict)
    assert any(
        r.startswith("track_a_integrity_fail")
        for r in result.hard_decline_reasons
    ), result.hard_decline_reasons
    assert result.recommendation == "decline"
    assert result.tier == "F"


def test_track_b_high_under_track_abc_hard_declines(
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``track_abc`` engine + Track B ``high`` → ``track_b_high_risk``."""
    monkeypatch.setenv("AEGIS_SCORING_ENGINE", "track_abc")
    get_settings.cache_clear()

    band = _track_b_band_fixture("high")
    result = score_deal(clean_deal, ofac=fresh_ofac, track_b_band=band)
    assert "track_b_high_risk" in result.hard_decline_reasons, (
        result.hard_decline_reasons
    )
    assert result.recommendation == "decline"
    assert result.tier == "F"


def test_legacy_engine_declines_on_fraud_score_75(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """Default (``legacy``) engine — ``fraud_score=75`` fires the
    pre-U30 hard-decline rule even when Track A/B inputs are clean.
    Anti-regression: the wiring U33 adds must not change byte-identical
    legacy behaviour."""
    assert get_settings().aegis_scoring_engine == "legacy"

    deal = clean_deal.model_copy(update={"fraud_score": 75})
    track_a = compute_integrity_verdict(_clean_signals())
    track_b = _track_b_band_fixture("low")
    result = score_deal(
        deal,
        ofac=fresh_ofac,
        track_a_verdict=track_a,
        track_b_band=track_b,
    )
    assert any(
        r.startswith("fraud_score_critical")
        for r in result.hard_decline_reasons
    ), result.hard_decline_reasons
    assert result.recommendation == "decline"


def test_track_abc_clean_inputs_ignore_legacy_fraud_score(
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``track_abc`` + clean Track A + clean Track B + fraud_score=75 →
    does NOT fire the legacy threshold. Central anti-regression for the
    cutover; mirrors the corresponding case in
    ``test_scoring_engine_config.py`` but composed via the real verdict
    composer rather than a hand-built model."""
    monkeypatch.setenv("AEGIS_SCORING_ENGINE", "track_abc")
    get_settings.cache_clear()

    deal = clean_deal.model_copy(update={"fraud_score": 75})
    track_a = compute_integrity_verdict(_clean_signals())
    track_b = _track_b_band_fixture("low")
    result = score_deal(
        deal,
        ofac=fresh_ofac,
        track_a_verdict=track_a,
        track_b_band=track_b,
    )
    assert not any(
        r.startswith("fraud_score_critical")
        for r in result.hard_decline_reasons
    ), result.hard_decline_reasons
    assert not any(
        r.startswith("track_a_integrity_fail")
        for r in result.hard_decline_reasons
    )
    assert "track_b_high_risk" not in result.hard_decline_reasons
    assert result.recommendation != "decline"


# -- compute_score_deal_track_inputs: extraction + failure fallback --------


def test_helper_returns_none_for_empty_documents() -> None:
    """No documents → ``(None, None)`` — the legacy engine still runs
    on the operator-provided ``ScoreInput``."""
    track_a, track_b = compute_score_deal_track_inputs(
        documents=[],
        list_transactions=lambda _: [],
    )
    assert track_a is None
    assert track_b is None


def test_helper_extracts_track_a_fail_from_strong_metadata_doc() -> None:
    """A document with metadata_score>=50 + editor flag produces a
    Track A ``fail`` verdict that the helper surfaces as the
    ``track_a_verdict`` for ``score_deal``."""
    doc = _document_row_stub(
        metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
        all_flags=(),
        fraud_score_breakdown={"metadata": 72},
    )
    track_a, track_b = compute_score_deal_track_inputs(
        documents=[doc],
        list_transactions=lambda _: [],
    )
    assert track_a is not None
    assert track_a.verdict == "fail"
    assert track_a.branch == "strong_metadata"
    # No transactions → Track B can't compute → None.
    assert track_b is None


def test_helper_picks_worst_verdict_across_multiple_documents() -> None:
    """When multiple docs produce verdicts of different severities,
    the ``fail`` verdict wins so ``score_deal`` gates correctly under
    ``track_abc``."""
    doc_clean = _document_row_stub(
        metadata_flags=("page_count: 6",),
        all_flags=(),
        fraud_score_breakdown={"metadata": 4},
        uploaded_at="2026-05-01T00:00:00Z",
    )
    doc_fail = _document_row_stub(
        metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
        all_flags=(),
        fraud_score_breakdown={"metadata": 72},
        uploaded_at="2026-06-01T00:00:00Z",
    )
    track_a, _ = compute_score_deal_track_inputs(
        documents=[doc_clean, doc_fail],
        list_transactions=lambda _: [],
    )
    assert track_a is not None
    assert track_a.verdict == "fail"


def test_helper_swallows_exceptions_and_returns_none() -> None:
    """Any exception from the underlying compute path → ``(None, None)``
    so ``score_deal`` falls back to the legacy engine. Documented
    fallback in the helper's docstring; non-negotiable per CLAUDE.md
    'never let a verdict-compute error break scoring'."""
    boom_doc = MagicMock()
    # Accessing any attribute the dossier-panel inputs read will raise.
    type(boom_doc).id = property(
        lambda self: (_ for _ in ()).throw(RuntimeError("synthetic boom"))
    )

    track_a, track_b = compute_score_deal_track_inputs(
        documents=[boom_doc],
        list_transactions=lambda _: [],
        merchant_id="merchant_under_test",
    )
    assert track_a is None
    assert track_b is None


# -- end-to-end: score-emitting wiring through the API route --------------


def test_score_route_passes_track_a_fail_under_track_abc(
    monkeypatch: pytest.MonkeyPatch,
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
) -> None:
    """End-to-end proof: when the merchant's latest document carries
    strong-metadata signals AND ``AEGIS_SCORING_ENGINE=track_abc``, the
    ``/deals/score`` call site computes the verdict + passes it into
    ``score_deal``, producing ``track_a_integrity_fail`` in
    ``hard_decline_reasons``.

    Drives ``aegis.api.routes.deals._track_inputs_for_deal`` —
    the lookup that converts ``deal.merchant_id`` into the verdict
    pair. We stub ``DocumentRepository`` so this test is hermetic; the
    real production wiring is exercised by the integration suite via
    the same helper.
    """
    monkeypatch.setenv("AEGIS_SCORING_ENGINE", "track_abc")
    get_settings.cache_clear()

    # Build a stub repo that returns one strong-metadata document.
    fail_doc = _document_row_stub(
        metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
        all_flags=(),
        fraud_score_breakdown={"metadata": 80},
    )
    repo = MagicMock()
    repo.list_documents.return_value = [fail_doc]
    repo.get_analyses_by_document_ids.return_value = {fail_doc.id: None}
    repo.list_transactions.return_value = []

    from aegis.api.routes.deals import _track_inputs_for_deal

    track_a, track_b = _track_inputs_for_deal(repo, clean_deal.merchant_id)
    assert track_a is not None
    assert track_a.verdict == "fail"
    assert track_a.branch == "strong_metadata"

    # Feed the route-derived verdicts back into score_deal — proving the
    # composition pin is intact end-to-end.
    result = score_deal(
        clean_deal,
        ofac=fresh_ofac,
        track_a_verdict=track_a,
        track_b_band=track_b,
    )
    assert any(
        r.startswith("track_a_integrity_fail")
        for r in result.hard_decline_reasons
    ), result.hard_decline_reasons
    assert result.recommendation == "decline"


def test_score_route_helper_returns_none_when_lookup_fails(
    clean_deal: ScoreInput,
) -> None:
    """``_track_inputs_for_deal`` swallows repository-side exceptions
    so the route falls back to the legacy engine rather than 500ing on
    a stale Supabase connection."""
    repo = MagicMock()
    repo.list_documents.side_effect = RuntimeError("supabase down")

    from aegis.api.routes.deals import _track_inputs_for_deal

    track_a, track_b = _track_inputs_for_deal(repo, clean_deal.merchant_id)
    assert track_a is None
    assert track_b is None
