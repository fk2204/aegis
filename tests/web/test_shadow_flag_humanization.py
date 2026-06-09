"""Tests for U18 shadow-flag humanization.

Covers the 15 shadow flag prefix families that landed in this session
(R0.2 / R1.x / R3.4 / R4.4 / R4.6 / U10 / U12 / H8 / M9 / U8) plus the
``apr_not_computable`` soft-concern humanization. The contract is:

  1. Every registered shadow flag returns a non-empty title +
     description + detail. The dossier discreet shadow section needs
     all three to render a worker-readable row.
  2. Sample evidence strings round-trip through the registered
     formatter — verifies the regexes match the emission-site format
     in ``parser/aggregate.py``, ``parser/patterns.py``,
     ``parser/validate.py``, ``parser/nsf_secondary.py``,
     ``parser/pipeline.py``, ``scoring/score.py``,
     ``merchants/cross_statement_detector.py``.
  3. Unknown flag codes degrade gracefully (no exception, fallback
     ``HumanFlag`` with the raw detail visible) — same contract as
     ``test_flag_labels.py::test_unknown_pattern_falls_back_to_raw``.

Per CLAUDE.md "Decision-boundary changes — shadow-first": none of these
flags affect tier / decline. The tests assert ``severity_band ==
'context'`` so a stray future bump to the registry is caught here
rather than on the dossier.
"""

from __future__ import annotations

import pytest

from aegis.web._flag_labels import humanize_flag
from aegis.web._pattern_cards import humanize_soft_concern

# ---------------------------------------------------------------------------
# Prefix-level smoke: each family returns non-empty title + description
# ---------------------------------------------------------------------------

# (prefix, sample_evidence) — sample_evidence is a realistic detail
# string captured from the emission-site format. The humanizer should
# accept it without raising and produce a non-empty title + description.
_SHADOW_SAMPLES: list[tuple[str, str]] = [
    # R0.2 lender-proceeds exclusion (aggregate.py)
    ("lender_proceeds_excluded", "2_$50000.00_(ONDECK|KAPITUS)"),
    (
        "lender_proceeds_excluded_row",
        "ONDECK_$25000.00_550e8400-e29b-41d4-a716-446655440000",
    ),
    # R1.1 fuzzy MCA / disguise candidates (patterns.py)
    (
        "mca_position_fuzzy_candidate",
        "KAPITUS_0.91_3_2026-01-03_2026-01-31",
    ),
    ("mca_disguise_candidate", "settlement adv_12_2"),
    # R1.3 same-day cluster (patterns.py)
    (
        "mca_same_day_cluster",
        "2026-01-15_3_(ONDECK|KAPITUS|CREDIBLY)",
    ),
    # R1.4 daily balance continuity (validate.py)
    (
        "daily_balance_continuity_break",
        "2026-01-15_expected_15234.12_actual_15234.10_diff_0.02",
    ),
    ("daily_balance_continuity_breaks_count", "3"),
    # R1.5 transaction-id gaps (validate.py)
    ("transaction_id_sequence_gap", "1024_1027_2"),
    # R1.7 ADB coverage thin (pipeline.py)
    (
        "adb_coverage_thin",
        "skip_ratio=15pct_threshold=10pct_would_route_review",
    ),
    # R1.8 NSF secondary (nsf_secondary.py)
    (
        "nsf_corroboration_missing",
        "2026-01-15_$35.00_INSUFFICIENT FUNDS FEE_would_route_review",
    ),
    (
        "nsf_low_confidence",
        "2026-01-15_$35.00_conf62_NSF FEE_would_route_review",
    ),
    # R3.4 state enforcement (score.py)
    ("state_enforcement_concern", "TX_HB700_tx_merchant_review"),
    # R4.4 industry-aware seasonality (score.py)
    (
        "seasonality_recategorized",
        "cv=0.42_naics=72_would_skip_volatility_penalty",
    ),
    (
        "seasonality_observed_but_volatility_extreme",
        "cv=0.78_naics=72_penalty_still_applied",
    ),
    # R4.6 EOF policy mismatch (score.py)
    (
        "eof_policy_mismatch",
        "scorer_declines_at_2_pipeline_routes_review",
    ),
    # H8 graduated TIB penalty (score.py)
    (
        "tib_ramp_shadow",
        "months=4_current_delta=-15_graduated_delta=-12",
    ),
    # M9 BSA structured deposits (patterns.py)
    (
        "structured_deposit_cluster",
        "3_deposits_in_14_day_window_dates=20260103,20260108,20260114",
    ),
    # U12 cross-statement detector (merchants/cross_statement_detector.py)
    (
        "duplicate_pdf_upload",
        "sha256_match_with_doc=550e8400-e29b-41d4-a716-446655440000:"
        "uploaded=2026-01-10T14:23:00+00:00",
    ),
    (
        "related_account_suspected",
        "holder=ACME LLC:existing_last4=4521:new_last4=8832",
    ),
]


@pytest.mark.parametrize("code,detail", _SHADOW_SAMPLES)
def test_shadow_flag_has_title_and_description(code: str, detail: str) -> None:
    """Every registered shadow flag returns title + description + detail."""
    hf = humanize_flag(f"{code}:{detail}")

    assert hf.title, f"{code} returned empty title"

    # The non-empty description IS the registration proof — the
    # unknown-code fallback returns description="". A few hand-authored
    # titles happen to coincide with the de-snake-cased form of the
    # code (e.g. "state_enforcement_concern" -> "State enforcement
    # concern"), so the description field is the unambiguous gate.
    assert hf.description, (
        f"{code} returned empty description — fell through to the "
        f"unknown-code fallback path. Add a registry entry."
    )
    assert hf.category == "shadow", (
        f"{code} category should be 'shadow' (got {hf.category!r}). "
        f"The dossier shadow-signals section filters on this."
    )
    assert hf.severity_band == "context", (
        f"{code} severity_band should be 'context' (got "
        f"{hf.severity_band!r}). Shadow flags must not drive decline-"
        f"class chip coloring."
    )

    # Detail must be non-empty for samples that carry a payload; the
    # formatter either rewrites the detail into worker-readable form
    # OR falls back to the raw string. Either is acceptable, but it
    # must NOT be empty.
    assert hf.detail, (
        f"{code} formatter returned empty detail for sample {detail!r}"
    )


# ---------------------------------------------------------------------------
# Per-family detail round-trips: assert the formatter produced text
# that meaningfully captures the evidence string.
# ---------------------------------------------------------------------------


def test_mca_position_fuzzy_candidate_renders_funder_count_and_similarity() -> None:
    hf = humanize_flag(
        "mca_position_fuzzy_candidate:KAPITUS_0.91_3_2026-01-03_2026-01-31"
    )
    assert "KAPITUS" in hf.detail
    assert "3" in hf.detail
    assert "91" in hf.detail


def test_mca_disguise_candidate_renders_term_and_cadence() -> None:
    hf = humanize_flag("mca_disguise_candidate:settlement adv_12_2")
    assert "settlement adv" in hf.detail
    assert "12" in hf.detail
    assert "2d" in hf.detail


def test_mca_same_day_cluster_renders_funders_and_date() -> None:
    hf = humanize_flag(
        "mca_same_day_cluster:2026-01-15_3_(ONDECK|KAPITUS|CREDIBLY)"
    )
    assert "2026-01-15" in hf.detail
    assert "3" in hf.detail
    assert "ONDECK" in hf.detail
    assert "KAPITUS" in hf.detail


def test_daily_balance_continuity_break_renders_date_and_diff() -> None:
    hf = humanize_flag(
        "daily_balance_continuity_break:"
        "2026-01-15_expected_15234.12_actual_15234.10_diff_0.02"
    )
    assert "2026-01-15" in hf.detail
    assert "0.02" in hf.detail


def test_daily_balance_continuity_breaks_count_renders_count() -> None:
    hf = humanize_flag("daily_balance_continuity_breaks_count:5")
    assert "5" in hf.detail


def test_transaction_id_sequence_gap_renders_range() -> None:
    hf = humanize_flag("transaction_id_sequence_gap:1024_1027_2")
    assert "1024" in hf.detail
    assert "1027" in hf.detail
    assert "2" in hf.detail


def test_adb_coverage_thin_renders_ratio_and_threshold() -> None:
    hf = humanize_flag(
        "adb_coverage_thin:skip_ratio=15pct_threshold=10pct_would_route_review"
    )
    assert "15" in hf.detail
    assert "10" in hf.detail


def test_nsf_corroboration_missing_renders_date_and_amount() -> None:
    hf = humanize_flag(
        "nsf_corroboration_missing:"
        "2026-01-15_$35.00_INSUFFICIENT FUNDS FEE_would_route_review"
    )
    assert "2026-01-15" in hf.detail
    assert "35" in hf.detail
    # The "would_route_review" tail is implementation detail — it
    # must NOT leak into the rendered chip text.
    assert "would_route_review" not in hf.detail


def test_nsf_low_confidence_renders_confidence_score() -> None:
    hf = humanize_flag(
        "nsf_low_confidence:2026-01-15_$35.00_conf62_NSF FEE_would_route_review"
    )
    assert "62" in hf.detail
    assert "would_route_review" not in hf.detail


def test_state_enforcement_concern_tx_hb700_humanized() -> None:
    hf = humanize_flag("state_enforcement_concern:TX_HB700_tx_merchant_review")
    # Raw form has "TX_HB700_tx_merchant_review" — humanized must read
    # cleaner than the snake_case identifier.
    assert "TX_HB700_tx_merchant_review" != hf.detail
    assert "Texas" in hf.detail or "TX" in hf.detail
    assert "HB 700" in hf.detail or "HB700" in hf.detail


def test_state_enforcement_concern_fl_ga_humanized() -> None:
    hf = humanize_flag("state_enforcement_concern:FL_GA_advance_fee_prohibition")
    assert "FL" in hf.detail or "Florida" in hf.detail
    assert "advance-fee" in hf.detail.lower() or "advance fee" in hf.detail.lower()


def test_seasonality_recategorized_renders_cv_and_naics() -> None:
    hf = humanize_flag(
        "seasonality_recategorized:cv=0.42_naics=72_would_skip_volatility_penalty"
    )
    assert "0.42" in hf.detail
    assert "72" in hf.detail


def test_seasonality_extreme_renders_cv_and_naics() -> None:
    hf = humanize_flag(
        "seasonality_observed_but_volatility_extreme:"
        "cv=0.78_naics=72_penalty_still_applied"
    )
    assert "0.78" in hf.detail
    assert "72" in hf.detail


def test_eof_policy_mismatch_humanized_to_sentence() -> None:
    hf = humanize_flag(
        "eof_policy_mismatch:scorer_declines_at_2_pipeline_routes_review"
    )
    # The raw detail has snake_case identifier tokens; the rendered
    # form should read as a sentence.
    assert "scorer" in hf.detail.lower()
    assert "review" in hf.detail.lower()
    assert "_" not in hf.detail


def test_tib_ramp_shadow_renders_months_and_deltas() -> None:
    hf = humanize_flag(
        "tib_ramp_shadow:months=4_current_delta=-15_graduated_delta=-12"
    )
    assert "4" in hf.detail
    assert "-15" in hf.detail
    assert "-12" in hf.detail


def test_structured_deposit_cluster_renders_count_and_window() -> None:
    hf = humanize_flag(
        "structured_deposit_cluster:"
        "3_deposits_in_14_day_window_dates=20260103,20260108,20260114"
    )
    assert "3" in hf.detail
    assert "14" in hf.detail
    assert "20260103" in hf.detail


def test_duplicate_pdf_upload_renders_upload_timestamp() -> None:
    hf = humanize_flag(
        "duplicate_pdf_upload:"
        "sha256_match_with_doc=550e8400-e29b-41d4-a716-446655440000:"
        "uploaded=2026-01-10T14:23:00+00:00"
    )
    assert "2026-01-10" in hf.detail
    # UUIDs are noise on a chip — they should NOT appear in the
    # rendered detail text.
    assert "550e8400" not in hf.detail


def test_duplicate_pdf_upload_includes_copy_count_when_present() -> None:
    hf = humanize_flag(
        "duplicate_pdf_upload:"
        "sha256_match_with_doc=550e8400-e29b-41d4-a716-446655440000:"
        "uploaded=2026-01-10T14:23:00+00:00:"
        "total_prior_copies=3"
    )
    assert "3" in hf.detail


def test_related_account_suspected_renders_holder_and_last4s() -> None:
    hf = humanize_flag(
        "related_account_suspected:"
        "holder=ACME LLC:existing_last4=4521:new_last4=8832"
    )
    assert "ACME LLC" in hf.detail
    assert "4521" in hf.detail
    assert "8832" in hf.detail


def test_lender_proceeds_excluded_renders_count_total_names() -> None:
    hf = humanize_flag(
        "lender_proceeds_excluded:2_$50000.00_(ONDECK|KAPITUS)"
    )
    assert "2" in hf.detail
    assert "ONDECK" in hf.detail
    assert "KAPITUS" in hf.detail


def test_lender_proceeds_excluded_row_strips_uuid() -> None:
    hf = humanize_flag(
        "lender_proceeds_excluded_row:"
        "ONDECK_$25000.00_550e8400-e29b-41d4-a716-446655440000"
    )
    assert "ONDECK" in hf.detail
    assert "25" in hf.detail
    assert "550e8400" not in hf.detail


# ---------------------------------------------------------------------------
# Soft-concern path — apr_not_computable is emitted into
# ``score_result.soft_concerns``, not the parser's flag list. It rides
# the ``humanize_soft_concern`` path, not ``humanize_flag``.
# ---------------------------------------------------------------------------


def test_apr_not_computable_humanizes_as_soft_concern() -> None:
    hr = humanize_soft_concern(
        "apr_not_computable: optimizer could not bracket a root for "
        "the recommended terms"
    )
    assert hr.code == "apr_not_computable"
    assert hr.title, "apr_not_computable returned empty title"
    fallback_title = "Apr not computable"
    assert hr.title != fallback_title, (
        "apr_not_computable fell through to the unknown-code "
        "fallback path. Add a SOFT_CONCERN_COPY entry."
    )
    assert hr.description, "apr_not_computable returned empty description"
    assert "APR" in hr.title or "apr" in hr.title.lower()


# ---------------------------------------------------------------------------
# Fallback contract — unknown shadow codes must NOT crash the chip
# renderer. Same fallback contract ``humanize_flag`` already provides
# for unregistered pattern codes.
# ---------------------------------------------------------------------------


def test_unknown_shadow_code_falls_back_to_de_snake_cased_title() -> None:
    hf = humanize_flag("brand_new_shadow_detector_v2:foo_bar_baz")
    # No exception, no crash. The fallback title is the de-snake-cased
    # code; the detail is the raw payload so the operator can still
    # see what fired.
    assert hf.title == "Brand new shadow detector v2"
    assert hf.detail == "foo_bar_baz"


def test_empty_flag_string_returns_empty_flag_placeholder() -> None:
    hf = humanize_flag("")
    assert hf.title == "(empty flag)"
    assert hf.severity_band == "context"


def test_shadow_flag_with_prefix_strips_prefix() -> None:
    """``humanize_flag`` accepts the ``[SHADOW]`` / ``[WARN]`` prefixes
    that the pipeline + validate layers prepend."""
    hf_shadow = humanize_flag(
        "[SHADOW] adb_coverage_thin:skip_ratio=15pct_threshold=10pct"
    )
    hf_warn = humanize_flag(
        "[WARN] daily_balance_continuity_breaks_count:3"
    )
    assert hf_shadow.code == "adb_coverage_thin"
    assert hf_shadow.category == "shadow"
    assert hf_warn.code == "daily_balance_continuity_breaks_count"
    assert hf_warn.category == "shadow"
