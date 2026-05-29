"""Tests for ``aegis.web._attention_card`` — the structured shape
shared by the Today and Review Queue card layouts (chunk A of Proposal
4 redesign).

Covers:

* ``categorize_flags`` splits decline-class to its own bucket and
  groups the rest by glossary category in fixed display order.
* Deduplication by code happens on first-seen order.
* Unknown flag codes still flow into a usable bucket (graceful fallback
  matching ``humanize_flag``'s contract).
* ``derive_fraud_band`` mirrors router._fraud_band thresholds (35 / 65).
"""

import pytest

from aegis.web._attention_card import (
    CategorizedFlags,
    categorize_flags,
    derive_fraud_band,
)

# ---------------------------------------------------------------------------
# categorize_flags
# ---------------------------------------------------------------------------


def test_categorize_decline_class_lifted_to_its_own_bucket() -> None:
    """Decline-band flags (e.g. wash_deposit_suspected, acceleration)
    move out of the category-grouped section into ``decline_class``."""
    raw = [
        "[PATTERN] wash_deposit_suspected: 2 round-trip pairs within 5 days",
        "[PATTERN] mca_stacking: 3 MCA position(s) detected",
        "[PATTERN] paydown_mca_suspected: 5 debits with descending amounts",
    ]
    result = categorize_flags(raw)

    assert [hf.code for hf in result.decline_class] == ["wash_deposit_suspected"]
    assert list(result.by_category.keys()) == ["stacking"]
    assert [hf.code for hf in result.by_category["stacking"]] == [
        "mca_stacking",
        "paydown_mca_suspected",
    ]


def test_categorize_deduplicates_by_code_first_seen_wins() -> None:
    """If the same code appears twice (different docs across the
    merchant's queue), keep the first occurrence."""
    raw = [
        "[PATTERN] mca_stacking: 1 MCA position(s) detected",
        "[PATTERN] mca_stacking: 3 MCA position(s) detected",  # dupe code
        "[PATTERN] paydown_mca_suspected: 5 debits with descending amounts",
    ]
    result = categorize_flags(raw)

    codes = [hf.code for hf in result.by_category["stacking"]]
    assert codes == ["mca_stacking", "paydown_mca_suspected"]
    # The first occurrence's detail wins.
    assert "1 active position" in result.by_category["stacking"][0].detail


def test_categorize_preserves_display_order_across_categories() -> None:
    """Categories render in the declared display order even when the
    input gives them in some other order. Fabrication before tampering
    before soft, etc."""
    raw = [
        "[AGGREGATE] top_counterparty_concentration:60%_(acme)",     # soft
        "[META] page_layer_anomaly: 2 page(s) have an off-mode /Contents stream count",  # tampering
        "[PATTERN] preloan_spike: 7d spike last_week=$100 vs avg=$10",  # fabrication
        "[PATTERN] mca_stacking: 1 MCA position(s) detected",           # stacking
    ]
    result = categorize_flags(raw)

    assert list(result.by_category.keys()) == [
        "stacking",
        "fabrication",
        "tampering",
        "soft",
    ]


def test_categorize_drops_empty_buckets() -> None:
    """A category with zero flags is not present in by_category at all,
    so the chunk-B template can ``{% for cat, flags in
    flags.by_category.items() %}`` without emitting empty groups."""
    raw = [
        "[PATTERN] mca_stacking: 1 MCA position(s) detected",
    ]
    result = categorize_flags(raw)

    assert list(result.by_category.keys()) == ["stacking"]
    assert "fabrication" not in result.by_category
    assert "soft" not in result.by_category


def test_categorize_unknown_code_lands_in_default_bucket() -> None:
    """A future detector landing without registration produces a
    HumanFlag with category='unknown' (or the prefix default). The
    categorizer keeps it in a usable bucket rather than dropping it."""
    raw = [
        "[PATTERN] brand_new_detector_v2: 3 events over 14 days",
    ]
    result = categorize_flags(raw)

    # PATTERN prefix default category is 'unknown'; the code lands in
    # by_category['unknown'].
    assert "unknown" in result.by_category
    assert [hf.code for hf in result.by_category["unknown"]] == [
        "brand_new_detector_v2"
    ]


def test_categorize_empty_input_returns_empty_categorized_flags() -> None:
    result = categorize_flags([])

    assert isinstance(result, CategorizedFlags)
    assert result.is_empty
    assert result.total_count == 0
    assert result.decline_class == []
    assert result.by_category == {}


def test_categorize_total_count_sums_decline_and_categories() -> None:
    raw = [
        "[PATTERN] wash_deposit_suspected: 2 round-trip pairs within 5 days",       # decline
        "[PATTERN] mca_stacking: 1 MCA position(s) detected",                       # material
        "[AGGREGATE] top_counterparty_concentration:60%_(acme)",                    # context
    ]
    result = categorize_flags(raw)

    assert result.total_count == 3
    assert not result.is_empty


# ---------------------------------------------------------------------------
# derive_fraud_band
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "score,expected_band",
    [
        (None, "unknown"),
        (0, "clear"),
        (34, "clear"),
        (35, "review"),
        (64, "review"),
        (65, "decline"),
        (100, "decline"),
    ],
)
def test_derive_fraud_band_thresholds(score: int | None, expected_band: str) -> None:
    assert derive_fraud_band(score) == expected_band
