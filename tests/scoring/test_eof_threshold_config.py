"""AEGIS_EOF_THRESHOLD env var — config-driven EOF hard-decline threshold.

Covers the R4.6 reconciliation flag flip: instead of hardcoding the scorer
at ``eof_markers > 1`` (declines at 2+), the threshold reads from
``settings.aegis_eof_threshold``. Default ``1`` preserves the legacy
behavior; ``2`` lifts the policy to align with pipeline routing
(``docs/AUDIT_2026_05_10.md`` line 46: "2 EOFs → review, 3+ →
manual_review"); ``3`` lifts further.

Per CLAUDE.md "Decision-boundary changes — shadow-first": the flip is
a config / env var change, not a code deploy. These tests pin both
threshold variants AND the shadow-flag text that documents which policy
is active, so the operator can audit posture without grepping config.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from aegis.config import get_settings
from aegis.scoring.models import ScoreInput
from aegis.scoring.ofac import OFACClient
from aegis.scoring.score import score_deal

# -- fixtures ----------------------------------------------------------------


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
    """Settings is ``@lru_cache``d; clear before + after each test so env
    var overrides via ``monkeypatch.setenv`` actually take effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# -- default threshold (legacy behavior preserved) --------------------------


def test_default_threshold_1_eof_2_hard_declines(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """Default ``AEGIS_EOF_THRESHOLD=1`` — eof_markers=2 hard-declines.

    Regression guard: with the env var unset the scorer must behave
    byte-identically to the pre-config-flag implementation. This is the
    same assertion shape as the existing R4.6 test in
    ``tests/scoring/test_score_r3_r4.py`` to make the regression
    relationship explicit.
    """
    # No monkeypatch.setenv → default of 1 applies.
    assert get_settings().aegis_eof_threshold == 1
    deal = clean_deal.model_copy(update={"eof_markers": 2})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(
        r.startswith("incremental_pdf_saves") for r in result.hard_decline_reasons
    ), f"expected incremental_pdf_saves hard decline; got {result.hard_decline_reasons}"
    assert result.recommendation == "decline"
    assert result.tier == "F"
    # Legacy shadow flag — mismatch posture.
    assert (
        "eof_policy_mismatch:scorer_declines_at_2_pipeline_routes_review"
        in result.shadow_flags
    )


def test_default_threshold_1_eof_1_no_decline_no_flag(
    clean_deal: ScoreInput, fresh_ofac: OFACClient
) -> None:
    """Default threshold — eof_markers=1 (boundary) does not fire the rule."""
    assert get_settings().aegis_eof_threshold == 1
    result = score_deal(clean_deal, ofac=fresh_ofac)
    assert not any(
        r.startswith("incremental_pdf_saves") for r in result.hard_decline_reasons
    )
    assert not any(f.startswith("eof_policy_") for f in result.shadow_flags)


# -- lifted to 2 (aligned with pipeline) ------------------------------------


def test_threshold_2_eof_2_does_not_hard_decline(
    monkeypatch: pytest.MonkeyPatch,
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
) -> None:
    """``AEGIS_EOF_THRESHOLD=2`` — eof_markers=2 no longer hard-declines.

    Verifies the lift lands: a 2-EOF statement that would have been
    declined under the legacy threshold now passes the gate.
    """
    monkeypatch.setenv("AEGIS_EOF_THRESHOLD", "2")
    get_settings.cache_clear()
    assert get_settings().aegis_eof_threshold == 2

    deal = clean_deal.model_copy(update={"eof_markers": 2})
    result = score_deal(deal, ofac=fresh_ofac)
    assert not any(
        r.startswith("incremental_pdf_saves") for r in result.hard_decline_reasons
    ), (
        f"eof_markers=2 must not hard-decline at threshold=2; "
        f"got {result.hard_decline_reasons}"
    )
    # And neither shadow flag fires below the threshold.
    assert not any(f.startswith("eof_policy_") for f in result.shadow_flags)


def test_threshold_2_eof_3_hard_declines_with_aligned_flag(
    monkeypatch: pytest.MonkeyPatch,
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
) -> None:
    """``AEGIS_EOF_THRESHOLD=2`` — eof_markers=3 hard-declines + emits ``aligned``.

    The aligned flag confirms the operator-side flip to pipeline-policy
    parity ("3+ → manual_review" matches "decline at 3+").
    """
    monkeypatch.setenv("AEGIS_EOF_THRESHOLD", "2")
    get_settings.cache_clear()

    deal = clean_deal.model_copy(update={"eof_markers": 3})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(
        r.startswith("incremental_pdf_saves") for r in result.hard_decline_reasons
    ), f"expected incremental_pdf_saves hard decline; got {result.hard_decline_reasons}"
    assert result.recommendation == "decline"
    assert result.tier == "F"
    # Aligned shadow flag — flip succeeded.
    assert "eof_policy_aligned:scorer_declines_at_3_threshold=2" in result.shadow_flags
    # Mismatch flag must NOT fire under the lift.
    assert not any(
        f.startswith("eof_policy_mismatch:") for f in result.shadow_flags
    )


# -- lifted to 3 (further lift) ---------------------------------------------


def test_threshold_3_eof_3_does_not_hard_decline(
    monkeypatch: pytest.MonkeyPatch,
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
) -> None:
    """``AEGIS_EOF_THRESHOLD=3`` — eof_markers=3 no longer hard-declines."""
    monkeypatch.setenv("AEGIS_EOF_THRESHOLD", "3")
    get_settings.cache_clear()
    assert get_settings().aegis_eof_threshold == 3

    deal = clean_deal.model_copy(update={"eof_markers": 3})
    result = score_deal(deal, ofac=fresh_ofac)
    assert not any(
        r.startswith("incremental_pdf_saves") for r in result.hard_decline_reasons
    ), (
        f"eof_markers=3 must not hard-decline at threshold=3; "
        f"got {result.hard_decline_reasons}"
    )
    assert not any(f.startswith("eof_policy_") for f in result.shadow_flags)


def test_threshold_3_eof_4_hard_declines_with_aligned_flag(
    monkeypatch: pytest.MonkeyPatch,
    clean_deal: ScoreInput,
    fresh_ofac: OFACClient,
) -> None:
    """``AEGIS_EOF_THRESHOLD=3`` — eof_markers=4 declines + aligned flag.

    Flag text must reflect the threshold value, not a hardcoded constant.
    """
    monkeypatch.setenv("AEGIS_EOF_THRESHOLD", "3")
    get_settings.cache_clear()

    deal = clean_deal.model_copy(update={"eof_markers": 4})
    result = score_deal(deal, ofac=fresh_ofac)
    assert any(
        r.startswith("incremental_pdf_saves") for r in result.hard_decline_reasons
    )
    assert result.recommendation == "decline"
    # Aligned flag pins both the active threshold and the next-decline value.
    assert "eof_policy_aligned:scorer_declines_at_4_threshold=3" in result.shadow_flags


# -- pydantic field bounds ---------------------------------------------------


def test_threshold_below_ge_bound_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AEGIS_EOF_THRESHOLD=0`` violates ``ge=1`` and fails settings load.

    Guards against an operator typo that would disable the EOF gate entirely.
    """
    monkeypatch.setenv("AEGIS_EOF_THRESHOLD", "0")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()


def test_threshold_above_le_bound_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AEGIS_EOF_THRESHOLD=11`` violates ``le=10`` and fails settings load."""
    monkeypatch.setenv("AEGIS_EOF_THRESHOLD", "11")
    get_settings.cache_clear()
    with pytest.raises(ValidationError):
        get_settings()
