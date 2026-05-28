"""Tests for ``aegis.web._flag_labels``.

Covers the four flag-string shapes the parser emits today AND the
graceful-fallback contract for codes we haven't registered yet. The
fallback is the load-bearing piece — a future detector landing without
operator copy must not crash the chip renderer.
"""

from __future__ import annotations

import pytest

from aegis.web._flag_labels import HumanFlag, humanize_audit_action, humanize_flag

# ---------------------------------------------------------------------------
# Pattern flags ([PATTERN] prefix, "code: detail" body)
# ---------------------------------------------------------------------------


def test_wash_deposit_humanizes_pair_count() -> None:
    hf = humanize_flag(
        "[PATTERN] wash_deposit_suspected: 2 round-trip deposit/withdrawal pairs within 5 days"
    )
    assert hf.code == "wash_deposit_suspected"
    assert hf.title == "Suspected wash deposits"
    assert hf.detail == "2 pairs in 5 days"
    assert hf.category == "fabrication"
    assert hf.severity_band == "decline"
    assert hf.chip_class == "bad"


def test_mca_stacking_humanizes_position_count() -> None:
    hf = humanize_flag("[PATTERN] mca_stacking: 3 MCA position(s) detected")
    assert hf.code == "mca_stacking"
    assert hf.title == "MCA stacking"
    assert hf.detail == "3 active positions"
    assert hf.category == "stacking"
    assert hf.severity_band == "material"


def test_acceleration_humanizes_lender_and_ratio() -> None:
    raw = (
        "[PATTERN] acceleration_clause_triggered: OnDeck: latest debit "
        "$4500.00 is 7.4x median prior $612.00 - possible funder acceleration"
    )
    hf = humanize_flag(raw)
    assert hf.code == "acceleration_clause_triggered"
    assert hf.title == "MCA acceleration"
    assert "OnDeck" in hf.detail
    assert "7.4x" in hf.detail
    assert hf.severity_band == "decline"


def test_preloan_spike_renders_money_short_form() -> None:
    hf = humanize_flag(
        "[PATTERN] preloan_spike: 14d spike last_14d=$694311.35 vs avg=$25524.30"
    )
    assert hf.code == "preloan_spike"
    assert hf.title == "Pre-loan deposit spike"
    assert "694k" in hf.detail
    assert "25k" in hf.detail
    assert "14d" in hf.detail
    assert hf.category == "fabrication"


def test_recent_account_opening_humanizes_days_ago() -> None:
    hf = humanize_flag(
        "[PATTERN] recent_account_opening: statement begins 41 days before today"
    )
    assert hf.title == "New account"
    assert hf.detail == "period starts 41 days ago"
    assert hf.category == "recency"


def test_payroll_absent_humanizes_period_and_revenue() -> None:
    hf = humanize_flag(
        "[PATTERN] payroll_absent: no payroll-processor activity over 28 days with $84250 revenue"
    )
    assert hf.title == "Payroll absent"
    assert "28d" in hf.detail
    assert "84k" in hf.detail
    assert hf.severity_band == "look_closer"


def test_duplicate_deposits_humanizes_pair_count() -> None:
    hf = humanize_flag(
        "[PATTERN] duplicate_deposits_detected: 4 same-date+amount deposit pair(s)"
    )
    assert hf.title == "Duplicate deposits"
    assert hf.detail == "4 duplicate pairs"


def test_unauthorized_withdrawal_dispute_humanizes_count() -> None:
    raw = (
        "[PATTERN] unauthorized_withdrawal_dispute: 2 reversal credit(s) "
        "paired with prior MCA debit(s)"
    )
    hf = humanize_flag(raw)
    assert hf.title == "Unauthorized withdrawal dispute"
    assert hf.detail == "2 reversals vs prior MCA debit"
    assert hf.severity_band == "decline"


def test_nsf_clustering_short_humanizes() -> None:
    hf = humanize_flag("[PATTERN] nsf_clustering_short: 4 NSFs in 18 days")
    assert hf.title == "NSF concentration"
    assert hf.detail == "4 NSFs in 18 days"


def test_customer_concentration_humanizes_label_and_pct() -> None:
    hf = humanize_flag(
        "[PATTERN] customer_concentration: top counterparty = 78% of revenue (acme corp)"
    )
    assert hf.title == "Customer concentration"
    assert hf.detail == "acme corp (78%)"
    assert hf.category == "concentration"


# ---------------------------------------------------------------------------
# Metadata flags ([META] prefix)
# ---------------------------------------------------------------------------


def test_incremental_saves_humanizes() -> None:
    hf = humanize_flag("[META] incremental_saves: 2 EOF markers")
    assert hf.code == "incremental_saves"
    assert hf.title == "PDF saved incrementally"
    assert hf.detail == "2 EOF markers"
    assert hf.category == "tampering"
    assert hf.severity_band == "decline"


def test_editor_detected_passes_producer_through_as_detail() -> None:
    hf = humanize_flag("[META] editor_detected: Foxit PhantomPDF")
    assert hf.title == "PDF editor detected"
    assert hf.detail == "Foxit PhantomPDF"
    assert hf.severity_band == "material"


def test_personal_author_wraps_name_in_label() -> None:
    hf = humanize_flag("[META] personal_author: John Smith")
    assert hf.title == "Personal author on PDF"
    assert hf.detail == "author 'John Smith'"


def test_stripped_metadata_has_no_detail() -> None:
    hf = humanize_flag("[META] stripped_metadata")
    assert hf.code == "stripped_metadata"
    assert hf.title == "PDF metadata stripped"
    assert hf.detail == ""
    assert hf.category == "tampering"


def test_modified_after_creation_dynamic_format() -> None:
    """Minute count is baked into the *code* (e.g. ``modified_120min_after_creation``)
    so a regex catches every variant rather than the registry needing
    one entry per integer."""
    hf = humanize_flag("[META] modified_120min_after_creation")
    assert hf.title == "PDF modified after creation"
    assert "120" in hf.detail
    assert "min" in hf.detail
    assert hf.category == "tampering"


def test_page_size_inconsistency_humanizes_sizes_list() -> None:
    hf = humanize_flag("[META] page_size_inconsistency: 612x792, 612x1008")
    assert hf.title == "Page size mismatch"
    assert "612x792" in hf.detail
    assert hf.severity_band == "decline"


def test_font_inconsistency_humanizes_page_count() -> None:
    hf = humanize_flag(
        "[META] font_inconsistency: 3 page(s) have no font overlap"
    )
    assert hf.title == "Font inconsistency"
    assert "3 pages" in hf.detail


# ---------------------------------------------------------------------------
# Aggregate soft signals ([AGGREGATE] prefix, "code:detail" body)
# ---------------------------------------------------------------------------


def test_top_counterparty_concentration_humanizes_payee_and_pct() -> None:
    hf = humanize_flag(
        "[AGGREGATE] top_counterparty_concentration:78%_(payward interactive)"
    )
    assert hf.code == "top_counterparty_concentration"
    assert hf.title == "Top customer"
    assert hf.detail == "payward interactive (78%)"
    assert hf.category == "soft"
    assert hf.severity_band == "context"
    # context band -> no chip color class (bare ``chip``)
    assert hf.chip_class == ""


def test_payroll_cadence_humanizes_with_revenue_pct() -> None:
    hf = humanize_flag(
        "[AGGREGATE] payroll_cadence:biweekly_12%_of_revenue"
    )
    assert hf.title == "Payroll cadence"
    assert hf.detail == "biweekly, 12% of revenue"


def test_payroll_cadence_irregular_single_event() -> None:
    hf = humanize_flag(
        "[AGGREGATE] payroll_cadence:irregular_count_1"
    )
    assert hf.detail == "single payroll event"


def test_nsf_on_negative_days_humanizes_ratio() -> None:
    hf = humanize_flag("[AGGREGATE] nsf_on_negative_days:3_of_7")
    assert hf.title == "NSFs on negative-balance days"
    assert hf.detail == "3 of 7 NSFs on negative days"


def test_adb_partial_coverage_humanizes() -> None:
    hf = humanize_flag("[AGGREGATE] adb_partial_coverage:7/30")
    assert hf.title == "ADB coverage gap"
    assert "7" in hf.detail
    assert "30" in hf.detail


# ---------------------------------------------------------------------------
# Confidence + math + infra
# ---------------------------------------------------------------------------


def test_classification_confidence_below_floor_humanizes() -> None:
    hf = humanize_flag(
        "[CONFIDENCE] classification_confidence_below_floor: avg=56 floor=60"
    )
    assert hf.title == "Transaction classifier confidence low"
    assert hf.detail == "avg 56 vs 60 required"


def test_ocr_fallback_used_humanizes() -> None:
    hf = humanize_flag("[META] ocr_fallback_used")
    assert hf.title == "OCR fallback used"
    assert hf.detail == ""
    assert hf.category == "soft"
    assert hf.severity_band == "context"


def test_math_validation_failure_falls_back_to_prefix_defaults() -> None:
    """MATH-prefixed validation failures aren't individually registered;
    they should land in the math category with the prefix default band."""
    hf = humanize_flag("[MATH] reconciliation_failed_deposit")
    assert hf.code == "reconciliation_failed_deposit"
    assert hf.category == "math"
    assert hf.severity_band == "material"
    # title is the de-snake-cased code
    assert "Reconciliation" in hf.title


# ---------------------------------------------------------------------------
# Graceful fallback — the load-bearing contract
# ---------------------------------------------------------------------------


def test_unknown_code_does_not_crash_and_falls_back_usable() -> None:
    """A future detector lands without operator copy — chip rendering
    must continue to work. The raw code becomes the title (de-snake-
    cased so it's at least readable), raw detail passes through, and the
    category/band default sensibly off the prefix."""
    hf = humanize_flag("[PATTERN] brand_new_detector_v2: 3 events over 14 days")
    assert isinstance(hf, HumanFlag)
    assert hf.code == "brand_new_detector_v2"
    assert hf.title == "Brand new detector v2"
    assert hf.detail == "3 events over 14 days"
    assert hf.category == "unknown"
    assert hf.severity_band == "material"


def test_unknown_code_in_aggregate_prefix_uses_soft_category() -> None:
    hf = humanize_flag("[AGGREGATE] surprise_signal:42_widgets")
    assert hf.code == "surprise_signal"
    assert hf.category == "soft"
    assert hf.severity_band == "context"


def test_flag_with_no_prefix_falls_back_safely() -> None:
    hf = humanize_flag("just_a_bare_code")
    assert hf.code == "just_a_bare_code"
    assert hf.category == "unknown"
    assert hf.detail == ""


def test_empty_and_whitespace_strings_dont_crash() -> None:
    assert humanize_flag("").title == "(empty flag)"
    assert humanize_flag("   ").title == "(empty flag)"


def test_unexpected_detail_format_degrades_to_raw_text() -> None:
    """If a detector emits a slightly different detail shape than the
    formatter expects, the chip must show the raw text rather than the
    formatter raising — graceful degradation, not crashes."""
    # The formatter for wash_deposit expects "N round-trip ..."; feed it
    # something else and ensure raw text passes through.
    hf = humanize_flag(
        "[PATTERN] wash_deposit_suspected: surprising new detail format"
    )
    assert hf.title == "Suspected wash deposits"
    assert hf.detail == "surprising new detail format"


# ---------------------------------------------------------------------------
# Audit action humanizer
# ---------------------------------------------------------------------------


def test_submit_action_with_funder_names_renders_with_names() -> None:
    label = humanize_audit_action(
        "deal.submit_to_funders",
        details={"funder_names": ["OnDeck", "Credibly"]},
    )
    assert label == "recorded submission to OnDeck, Credibly"


def test_submit_action_without_details_falls_back_to_generic() -> None:
    assert (
        humanize_audit_action("deal.submit_to_funders")
        == "recorded submission to funders"
    )


def test_submit_action_with_empty_funder_names_falls_back() -> None:
    label = humanize_audit_action(
        "deal.submit_to_funders", details={"funder_names": []}
    )
    assert label == "recorded submission to funders"


def test_unknown_audit_action_falls_back_to_raw_identifier() -> None:
    """A new audit action without a registered label keeps the raw
    identifier so the feed shows *something* the operator can grep
    rather than going blank."""
    assert humanize_audit_action("merchant.new_event") == "merchant.new_event"
    assert humanize_audit_action("close.lead.sync_attempted") == "close.lead.sync_attempted"


# ---------------------------------------------------------------------------
# Chip class mapping (severity_band -> CSS modifier)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_class",
    [
        # decline -> bad (red)
        ("[PATTERN] wash_deposit_suspected: 2 round-trip pairs", "bad"),
        ("[META] incremental_saves: 2 EOF markers", "bad"),
        # material -> warn (amber)
        ("[PATTERN] mca_stacking: 1 MCA position(s) detected", "warn"),
        ("[PATTERN] preloan_spike: 7d spike last_week=$1 vs avg=$1", "warn"),
        # look_closer -> info (accent)
        ("[PATTERN] payroll_absent: no payroll over 21 days with $50000", "info"),
        ("[PATTERN] mca_payoff_signature: 1 lump-sum debit(s)", "info"),
        # context -> "" (no chip modifier, bare chip color)
        ("[AGGREGATE] top_counterparty_concentration:50%_(acme)", ""),
        ("[AGGREGATE] payroll_cadence:weekly_20%_of_revenue", ""),
    ],
)
def test_severity_band_maps_to_chip_class(raw: str, expected_class: str) -> None:
    hf = humanize_flag(raw)
    assert hf.chip_class == expected_class
