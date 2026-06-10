"""AEGIS_SCORING_ENGINE env var — Step 2 cutover kill-switch (audit B2).

Audit finding B2: two scoring engines live in production. Legacy
``fraud_score >= 65`` owns the hard-decline path in
``aegis.scoring.score`` (see ``FRAUD_SCORE_HARD_DECLINE`` — aliased to
the parser's ``HARD_DECLINE_THRESHOLD`` per audit §A.2 fix). Track A/B/C
(``aegis.scoring_v2``) run in shadow only — their verdicts surface on
the dossier panel but no live decision reads them.

Step 2 of the 3-track redesign flips A/B/C live. Per CLAUDE.md
"Decision-boundary changes — shadow-first" + ``docs/REMAINING_WORK.md``
Step 2 cutover plan: the flip is a config / env var change, not a code
deploy. ``AEGIS_SCORING_ENGINE`` is that flip.

These tests pin both engines' behavior so the operator (a) can confirm
the legacy default is byte-identical to the pre-config-flag scorer and
(b) the ``track_abc`` flip wires Track A ``fail`` and Track B ``high``
through to ``hard_decline_reasons`` while making the legacy
``fraud_score`` threshold informational.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from aegis.config import get_settings
from aegis.scoring.models import ScoreInput
from aegis.scoring.ofac import OFACClient
from aegis.scoring.score import score_deal
from aegis.scoring_v2.track_a import (
    DocumentIntegritySignals,
    IntegrityVerdict,
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
    """OFAC client with a fresh empty SDN list — never matches."""
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
    Same pattern as ``test_eof_threshold_config.py``."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# -- helpers ----------------------------------------------------------------


def _track_a_fail_verdict() -> IntegrityVerdict:
    """Build a real Track A ``fail`` verdict via ``compute_integrity_verdict``.

    Strong-metadata branch — ``metadata_score >= 50`` fires regardless
    of corroboration. Mirrors the shape of an A&R KM-style integrity
    failure exactly as the dossier panel would produce it.
    """
    signals = DocumentIntegritySignals(
        document_id="doc_test_strong",
        metadata_score=72,
        metadata_flags=("editor_detected: iText 2.1.7 by 1T3XT",),
        validation_failures=(),
    )
    verdict = compute_integrity_verdict(signals)
    assert verdict.verdict == "fail", "fixture invariant: must be fail"
    return verdict


def _track_a_clean_verdict() -> IntegrityVerdict:
    """Real ``clean`` verdict — no signals fire."""
    signals = DocumentIntegritySignals(
        document_id="doc_test_clean",
        metadata_score=8,
        metadata_flags=(),
        validation_failures=(),
    )
    verdict = compute_integrity_verdict(signals)
    assert verdict.verdict == "clean", "fixture invariant: must be clean"
    return verdict


def _track_b_band(band: BandLevel) -> BusinessRiskBand:
    """Build a Track B ``BusinessRiskBand`` with a controlled band.

    Constructs the Pydantic model directly with the minimal cashflow
    payload required by ``CashflowSignals``. Track B's auto-decline
    surface in ``score.py`` reads ``band`` + ``BAND_TO_ACTION``; the
    underlying reasons / cashflow are not consulted by the gate, only
    surfaced on the dossier.
    """
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
        detail="synthetic fixture for engine flip test",
    )
    return BusinessRiskBand(
        band=band,
        action=BAND_TO_ACTION[band],
        cashflow=cashflow,
        reasons=(reason,),
        insufficient_data_factors=(),
    )


# -- shadow flag presence on both engines -----------------------------------


def test_legacy_engine_emits_active_engine_shadow_flag(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """Default engine — ``scoring_engine_active:legacy`` always lands on
    ``shadow_flags`` so the operator can audit posture without grepping
    config (mirrors the EOF-policy shadow-flag pattern)."""
    assert get_settings().aegis_scoring_engine == "legacy"
    result = score_deal(clean_deal, ofac=fresh_ofac)
    assert "scoring_engine_active:legacy" in result.shadow_flags


def test_track_abc_engine_emits_active_engine_shadow_flag(
    monkeypatch: pytest.MonkeyPatch,
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
) -> None:
    """``AEGIS_SCORING_ENGINE=track_abc`` — shadow flag reflects the lift."""
    monkeypatch.setenv("AEGIS_SCORING_ENGINE", "track_abc")
    get_settings.cache_clear()
    assert get_settings().aegis_scoring_engine == "track_abc"
    result = score_deal(clean_deal, ofac=fresh_ofac)
    assert "scoring_engine_active:track_abc" in result.shadow_flags
    # And the legacy flag does NOT also fire — the flag is a single
    # source of truth for the active engine.
    assert "scoring_engine_active:legacy" not in result.shadow_flags


# -- legacy engine: fraud_score still gates ---------------------------------


def test_legacy_engine_fraud_score_75_hard_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """Default ``legacy`` engine — ``fraud_score=75`` fires the
    pre-config-flag rule (``fraud_score_critical``). Regression guard:
    with the env var unset the scorer behaves byte-identically to
    pre-this-commit on the fraud_score path."""
    assert get_settings().aegis_scoring_engine == "legacy"
    deal = clean_deal.model_copy(update={"fraud_score": 75})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(
        r.startswith("fraud_score_critical") for r in result.hard_decline_reasons
    ), (
        f"expected fraud_score_critical hard decline under legacy; "
        f"got {result.hard_decline_reasons}"
    )
    assert result.recommendation == "decline"
    assert result.tier == "F"


def test_legacy_engine_ignores_track_a_fail(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """Legacy engine — a Track A ``fail`` verdict passed in does NOT
    fire ``track_a_integrity_fail``. The legacy engine consults only
    ``fraud_score`` for the live decline path; A/B/C remain shadow.

    Cross-check: ``clean_deal`` has ``fraud_score=10``, so the deal
    should pass the gate entirely (Track A is ignored)."""
    assert get_settings().aegis_scoring_engine == "legacy"
    track_a = _track_a_fail_verdict()
    result = score_deal(clean_deal, ofac=fresh_ofac, track_a_verdict=track_a)
    assert not any(
        r.startswith("track_a_integrity_fail") for r in result.hard_decline_reasons
    ), (
        f"legacy engine must not consume Track A; "
        f"got {result.hard_decline_reasons}"
    )
    assert result.recommendation != "decline"


# -- track_abc engine: Track A + Track B drive the gate ---------------------


def test_track_abc_engine_track_a_fail_hard_declines(
    monkeypatch: pytest.MonkeyPatch,
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
) -> None:
    """``track_abc`` — Track A ``fail`` promotes to a hard-decline reason
    with the branch name attached for evidence routing."""
    monkeypatch.setenv("AEGIS_SCORING_ENGINE", "track_abc")
    get_settings.cache_clear()

    track_a = _track_a_fail_verdict()
    result = score_deal(clean_deal, ofac=fresh_ofac, track_a_verdict=track_a)
    assert any(
        r.startswith("track_a_integrity_fail") for r in result.hard_decline_reasons
    ), (
        f"expected track_a_integrity_fail hard decline under track_abc; "
        f"got {result.hard_decline_reasons}"
    )
    assert result.recommendation == "decline"
    assert result.tier == "F"
    # Branch surfaced for downstream audit / display.
    fail_reason = next(
        r for r in result.hard_decline_reasons
        if r.startswith("track_a_integrity_fail")
    )
    assert "branch=strong_metadata" in fail_reason


def test_track_abc_engine_track_b_high_hard_declines(
    monkeypatch: pytest.MonkeyPatch,
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
) -> None:
    """``track_abc`` — Track B ``high`` band fires ``track_b_high_risk``."""
    monkeypatch.setenv("AEGIS_SCORING_ENGINE", "track_abc")
    get_settings.cache_clear()

    track_b = _track_b_band("high")
    result = score_deal(clean_deal, ofac=fresh_ofac, track_b_band=track_b)
    assert "track_b_high_risk" in result.hard_decline_reasons, (
        f"expected track_b_high_risk hard decline; "
        f"got {result.hard_decline_reasons}"
    )
    assert result.recommendation == "decline"
    assert result.tier == "F"


def test_track_abc_engine_clean_inputs_no_decline_despite_high_fraud_score(
    monkeypatch: pytest.MonkeyPatch,
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
) -> None:
    """``track_abc`` — clean Track A + clean Track B + ``fraud_score=75``
    does NOT decline. The legacy fraud_score threshold is informational
    under track_abc; the Track A/B verdicts are the live gate.

    This is the central anti-regression of the cutover: today a high
    blended fraud_score auto-declines; after the flip, only Track A/B
    can auto-decline."""
    monkeypatch.setenv("AEGIS_SCORING_ENGINE", "track_abc")
    get_settings.cache_clear()

    deal = clean_deal.model_copy(update={"fraud_score": 75})
    track_a = _track_a_clean_verdict()
    track_b = _track_b_band("low")
    result = score_deal(
        deal,
        ofac=fresh_ofac,
        track_a_verdict=track_a,
        track_b_band=track_b,
    )
    assert not any(
        r.startswith("fraud_score_critical") for r in result.hard_decline_reasons
    ), (
        f"track_abc must NOT fire legacy fraud_score_critical; "
        f"got {result.hard_decline_reasons}"
    )
    assert not any(
        r.startswith("track_a_integrity_fail") for r in result.hard_decline_reasons
    )
    assert "track_b_high_risk" not in result.hard_decline_reasons
    assert result.recommendation != "decline"


def test_track_abc_engine_track_a_review_annotates_no_decline(
    monkeypatch: pytest.MonkeyPatch,
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
) -> None:
    """``track_abc`` — Track A ``review`` is a soft annotation (shadow
    flag), not a hard decline. Pipeline-side ``manual_review`` routing
    is owned by the parse path; the scorer just records the signal."""
    monkeypatch.setenv("AEGIS_SCORING_ENGINE", "track_abc")
    get_settings.cache_clear()

    # Build a ``drift_alone`` review via the real composer.
    signals = DocumentIntegritySignals(
        document_id="doc_review",
        metadata_score=12,
        metadata_flags=("page_count: 6",),
        validation_failures=(
            "reconciliation_failed_period: expected -263.89 got 236.11",
        ),
    )
    track_a = compute_integrity_verdict(signals)
    assert track_a.verdict == "review", "fixture invariant"

    result = score_deal(clean_deal, ofac=fresh_ofac, track_a_verdict=track_a)
    assert not any(
        r.startswith("track_a_integrity_fail") for r in result.hard_decline_reasons
    )
    assert any(
        f.startswith("track_a_integrity_review:") for f in result.shadow_flags
    ), (
        f"expected track_a_integrity_review shadow flag; "
        f"got {result.shadow_flags}"
    )


def test_track_abc_engine_track_b_elevated_annotates_no_decline(
    monkeypatch: pytest.MonkeyPatch,
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
) -> None:
    """``track_abc`` — Track B ``elevated`` is annotation only.

    Per ``BAND_TO_ACTION``, ``elevated`` maps to ``review_neutral``
    (operator triage, no auto-decline). Only ``high`` is auto-decline."""
    monkeypatch.setenv("AEGIS_SCORING_ENGINE", "track_abc")
    get_settings.cache_clear()

    track_b = _track_b_band("elevated")
    result = score_deal(clean_deal, ofac=fresh_ofac, track_b_band=track_b)
    assert "track_b_high_risk" not in result.hard_decline_reasons
    assert "track_b_elevated_risk" in result.shadow_flags


# -- pydantic literal bounds ------------------------------------------------


def test_invalid_engine_value_rejected_at_settings_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pydantic ``Literal`` validates the env value at boot — an
    operator typo (``"trakc_abc"``) fails closed rather than silently
    falling back to legacy. Same protection pattern as
    ``test_eof_threshold_config``'s out-of-bound checks."""
    monkeypatch.setenv("AEGIS_SCORING_ENGINE", "trakc_abc")
    get_settings.cache_clear()
    with pytest.raises(Exception):  # noqa: B017 — pydantic ValidationError
        get_settings()
