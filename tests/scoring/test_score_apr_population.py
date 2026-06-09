"""M6 — ``ScoreResult.apr`` is populated for live deals.

Until M6, ``models.py`` declared ``apr: Decimal | None = None`` but the
score path never wrote it. The R4.2 work computed APR per-funder via
``compute_estimated_terms``; this fills the deal-level gap so the dossier
can show a single "AEGIS-recommended APR" alongside the per-funder grid.

Tests:
- soft-score path with valid recommended terms → ``apr`` is a positive
  ``Decimal``, and hand-verified to be within a tight band of a
  known-answer reconstruction.
- soft-score path with a degenerate factor (close enough to break
  ``calculate_apr``'s root bracket) → ``apr`` is ``None`` and the
  ``apr_not_computable`` soft_concern fires (no silent zero).
- hard-decline path → ``apr is None`` and no soft_concern (decline
  short-circuit does not produce a meaningful recommendation).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from aegis.compliance.apr import calculate_apr
from aegis.scoring.models import ScoreInput
from aegis.scoring.ofac import OFACClient
from aegis.scoring.score import score_deal


@pytest.fixture
def clean_ofac(tmp_path: Path) -> OFACClient:
    cache = tmp_path / "ofac.json"
    cache.write_text(
        json.dumps(
            {
                "entries": [{"primary_name": "ZZZ Should Not Match", "aliases": []}],
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    def _now() -> datetime:
        return datetime.now(UTC)

    def _must_not_call() -> bytes:
        raise AssertionError("OFAC fetcher should not be called when cache is fresh")

    return OFACClient(cache_path=cache, fetcher=_must_not_call, now=_now)


def test_apr_populated_on_soft_score_path(
    clean_deal: ScoreInput, clean_ofac: OFACClient
) -> None:
    """A scored deal with valid recommended terms must populate apr.

    Hand-verified: reconstruct the same payment stream that the scorer
    synthesizes and call ``calculate_apr`` directly. Result must match
    ``ScoreResult.apr`` exactly — both go through the same actuarial
    Reg Z Appendix J path with the same daily payments.
    """
    result = score_deal(clean_deal, ofac=clean_ofac)

    assert result.tier in {"A", "B", "C"}, f"unexpected tier {result.tier}"
    assert result.apr is not None, "apr should populate on soft-score path"
    assert result.apr > 0, f"apr must be positive, got {result.apr}"
    # MCA APRs run roughly 30%-200%. Sanity-band the result so a
    # regression that breaks the optimizer (e.g. produces a 1bp APR)
    # would fail loudly.
    assert Decimal("0.10") <= result.apr <= Decimal("3.0"), (
        f"apr {result.apr} outside plausible MCA band 10%-300%"
    )
    assert not any(
        c.startswith("apr_not_computable") for c in result.soft_concerns
    ), "should not fire apr_not_computable when apr is populated"

    # Hand-verify against a direct calculate_apr call with the same
    # synthesized payment stream the scorer used.
    suggested = result.suggested_max_advance
    factor = result.recommended_factor_rate
    payback = result.estimated_payback_days
    assert payback is not None and payback > 0

    total_repayment = (suggested * factor).quantize(Decimal("0.01"))
    daily_payment = (total_repayment / Decimal(payback)).quantize(Decimal("0.01"))
    disbursement = date(2026, 1, 1)
    payments = [
        (disbursement + timedelta(days=offset), daily_payment)
        for offset in range(1, payback + 1)
    ]
    expected_apr = calculate_apr(suggested, payments, disbursement).quantize(
        Decimal("0.0001")
    )
    assert result.apr == expected_apr, (
        f"scorer apr {result.apr} != hand-computed {expected_apr}"
    )


def test_apr_none_with_soft_concern_when_optimizer_cannot_bracket(
    clean_deal: ScoreInput,
    clean_ofac: OFACClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force ``calculate_apr`` to raise; scorer must catch, set apr=None,
    and append the ``apr_not_computable`` soft_concern.

    The cleanest way to exercise the catch path is to monkeypatch
    ``calculate_apr`` inside the score module so the production code
    path is unchanged but raises predictably. (Picking a "naturally
    degenerate" factor like 1.0 would short-circuit through
    ``_apr_inputs_present`` because that gate enforces factor > 1.0;
    that's correct production behavior — we don't synthesize a stream
    for a degenerate factor — but it bypasses the failure-mode we want
    to test here.)
    """
    from aegis.compliance.apr import APRCalculationError
    from aegis.scoring import score as score_module

    def _always_fail(*_args: object, **_kwargs: object) -> Decimal:
        raise APRCalculationError("brentq failed to converge: simulated")

    monkeypatch.setattr(score_module, "calculate_apr", _always_fail)

    result = score_deal(clean_deal, ofac=clean_ofac)

    assert result.apr is None, (
        f"apr should be None when optimizer cannot bracket; got {result.apr}"
    )
    assert any(c.startswith("apr_not_computable") for c in result.soft_concerns), (
        f"expected apr_not_computable soft_concern, got {result.soft_concerns}"
    )


def test_apr_none_on_hard_decline(
    clean_deal: ScoreInput, clean_ofac: OFACClient
) -> None:
    """Hard-decline path returns apr=None — no need to compute on a declined deal.

    Hard-decline produces ``suggested_max_advance=0`` so even the
    soft-score formula would refuse to synthesize a stream; the
    short-circuit return path doesn't call the APR helper at all.
    """
    # Force a hard decline: 8 active MCA positions trips
    # stacking_exceeds_limit before any other rule.
    declined = clean_deal.model_copy(update={"mca_positions": 8})
    result = score_deal(declined, ofac=clean_ofac)

    assert result.recommendation == "decline"
    assert result.apr is None, "declined deals should not carry an apr"
    # Hard-decline path must not surface apr_not_computable — the field
    # is None for "we didn't compute" reasons, not "we tried and failed."
    assert not any(
        c.startswith("apr_not_computable") for c in result.soft_concerns
    ), "hard-decline path should not append apr_not_computable"
