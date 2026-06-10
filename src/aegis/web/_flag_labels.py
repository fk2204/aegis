"""Plain-language labels for the raw flag strings the parser emits.

Every ``DocumentRow.all_flags`` entry is a string built by
``aegis.parser.pipeline._collect_flags`` and looks like one of:

    [META] incremental_saves: 2 EOF markers
    [PATTERN] wash_deposit_suspected: 2 round-trip deposit/withdrawal pairs within 5 days
    [AGGREGATE] top_counterparty_concentration:78%_(acme corp)
    [MATH] reconciliation_failed_deposit
    [CONFIDENCE] classification_confidence_below_floor: avg=56 floor=60
    [COMPOUND] fraud_cluster_triangulated

The strings are developer identifiers — workers reading them on the
Today / Review Queue / dossier chips have no idea what
``top_counterparty_concentration:104%_(payward interactive,)`` means
without opening the glossary in another tab.

This module is the single source of truth for translating a raw flag
string into a ``HumanFlag`` carrying:

* ``code``           — raw identifier (kept for audit, debugging, data-flag-code)
* ``title``          — human title rendered on the chip
* ``detail``         — brief human detail rendered alongside the title
* ``category``       — one of the 9 glossary categories (drives card grouping)
* ``severity_band``  — ``decline | material | look_closer | context`` (drives chip color)

Add a new detector? Register it in ``_FLAG_REGISTRY`` below in the same
commit that lands the detector. Unknown codes fall back gracefully —
the raw code becomes the title and the raw detail passes through — so a
forgotten registration never crashes the dashboard.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final, Literal

# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------

CategoryName = Literal[
    "stacking",        # MCA position / paydown / acceleration / dispute
    "fabrication",     # wash / duplicates / synthetic / round / preloan / velocity
    "stress",          # NSF clustering / payroll absent
    "concentration",   # customer concentration / processor holdback
    "hidden_account",  # unreconciled internal transfer
    "recency",         # recent account opening
    "tampering",       # all metadata flags + META infra
    "soft",            # aggregate soft signals (informational)
    "composite",       # ai_generated_score, cluster triangulation
    "math",            # validation gate failures
    "shadow",          # shadow-mode detectors (R0.2 / R1.x / U10 / U12 / H8 / M9)
    "unknown",         # fallback for new codes without registration
]

SeverityBand = Literal["decline", "material", "look_closer", "context"]


@dataclass(frozen=True)
class FlagSourceTransaction:
    """One source transaction backing a flag's chip drill-down.

    Chunk-3 presentation DTO. Carries the minimum fields the
    ``_chip_drilldown.html.j2`` partial renders, plus ``document_id`` +
    ``filename`` so the Today attention-queue's merchant-level chip
    expanders can tag each row with which upload contributed it. On the
    Review Queue (one doc per card) every row in a single chip shares
    the same filename; on Today (one merchant, N docs) the column lets
    workers scan which document a contributing row came from.
    """

    posted_date: date
    description: str
    amount: Decimal
    source_page: int
    source_line: int
    document_id: str
    filename: str


@dataclass(frozen=True)
class HumanFlag:
    """Renderable shape for a single flag chip.

    ``code`` stays the raw identifier so audit, log, and ``data-flag-code``
    attributes keep their machine-readable form. The other fields are
    operator-facing.

    ``cluster_signals`` is populated only for ``fraud_cluster_triangulated``
    — it carries the constituent ``HumanFlag`` objects parsed out of the
    cluster's detail string (``4_signals_a,b,c,d``) so the chip template
    can render them as an inline expander. ``None`` for every other flag.

    ``source_transactions`` is the chunk-3 drill-down hook. Populated by
    ``categorize_flags(pattern_index=...)`` for flag codes that match a
    ``Pattern`` in the doc's ``AnalysisRow.pattern_analysis`` cache
    (migration 032). ``None`` → chip renders as a plain span without
    drill-down (tampering, soft signals, confidence layer, and any code
    not emitted by ``analyze_patterns()`` fall through this branch).
    """

    code: str
    title: str
    detail: str
    category: CategoryName
    severity_band: SeverityBand
    cluster_signals: list[HumanFlag] | None = None
    source_transactions: list[FlagSourceTransaction] | None = None
    description: str = ""
    """One-sentence explanation of what fired and what the operator does next.

    Populated for the U18 shadow-flag families (R0.2 / R1.x / U10 / U12 /
    H8 / M9) so the discreet "Operator review signals (shadow mode)"
    section on the dossier can render a worker-readable rationale next
    to the title + detail. Empty string for the legacy chip-only flags
    where the title + detail already say what the chip means."""

    @property
    def chip_class(self) -> str:
        """CSS chip modifier matching this flag's severity band.

        The existing dossier / Today CSS palette uses ``bad / warn / info``
        for the three saturated levels and bare ``chip`` for muted
        context. Mapping the abstract band names here keeps the templates
        from re-encoding the lookup.
        """
        return _BAND_TO_CHIP_CLASS[self.severity_band]


_BAND_TO_CHIP_CLASS: Final[dict[SeverityBand, str]] = {
    "decline": "bad",
    "material": "warn",
    "look_closer": "info",
    "context": "",
}


# ---------------------------------------------------------------------------
# Per-flag detail formatters
#
# Each formatter receives the raw detail substring (the part after the
# ``code:`` delimiter, stripped) and returns a brief human phrase. They
# all guard against unexpected input shapes by returning the raw string
# verbatim when their regex misses — a detector emitting a slightly
# different detail format degrades to readable raw text rather than
# crashing the chip.
# ---------------------------------------------------------------------------


def _plural(n: int, word: str, plural: str | None = None) -> str:
    """Return ``"1 word"`` / ``"N words"`` with explicit plural override."""
    if n == 1:
        return f"{n} {word}"
    return f"{n} {plural or (word + 's')}"


def _fmt_mca_stacking(raw: str) -> str:
    # "3 MCA position(s) detected"
    m = re.match(r"(\d+) MCA position", raw)
    if m:
        n = int(m.group(1))
        return _plural(n, "active position")
    return raw


def _fmt_mca_payoff(raw: str) -> str:
    # "2 lump-sum debit(s) > $5k to known MCA funder"
    m = re.match(r"(\d+) lump-sum debit", raw)
    if m:
        n = int(m.group(1))
        return _plural(n, "lump-sum payoff > $5k")
    return raw


def _fmt_paydown_mca(raw: str) -> str:
    # "5 debits with descending amounts"
    m = re.match(r"(\d+) debits with descending amounts", raw)
    if m:
        return f"{m.group(1)} debits trending down"
    return raw


def _fmt_withdrawal_acceleration(raw: str) -> str:
    # "last-7d MCA debits: 8 vs prior weekly avg 3.0"
    m = re.search(r"last-7d MCA debits:\s*(\d+)\s*vs prior weekly avg\s*([\d.]+)", raw)
    if m:
        return f"{m.group(1)} debits last 7d vs {m.group(2)}/wk prior"
    return raw


def _fmt_acceleration_clause(raw: str) -> str:
    # "OnDeck: latest debit $4500 is 7.4x median prior $612 — possible funder acceleration"
    m = re.match(
        r"(?P<lender>[^:]+):\s*latest debit \$(?P<latest>[\d.,]+)\s*is\s*(?P<ratio>[\d.]+)x",
        raw,
    )
    if m:
        return f"{m.group('lender')}: ${m.group('latest')} is {m.group('ratio')}x prior"
    return raw


def _fmt_unauthorized_withdrawal_dispute(raw: str) -> str:
    # "2 reversal credit(s) paired with prior MCA debit(s)"
    # _plural() pluralizes the LAST word, which would turn this into
    # "2 reversal vs prior MCA debits" — wrong end of the phrase. Spell
    # the pluralization out so "reversal" pluralizes, "debit" stays
    # singular.
    m = re.match(r"(\d+) reversal credit", raw)
    if m:
        n = int(m.group(1))
        word = "reversal" if n == 1 else "reversals"
        return f"{n} {word} vs prior MCA debit"
    return raw


def _fmt_wash_deposit(raw: str) -> str:
    # "2 round-trip deposit/withdrawal pairs within 5 days"
    m = re.match(r"(\d+) round-trip", raw)
    if m:
        n = int(m.group(1))
        return f"{_plural(n, 'pair')} in 5 days"
    return raw


def _fmt_duplicate_deposits(raw: str) -> str:
    # "4 same-date+amount deposit pair(s)"
    m = re.match(r"(\d+) same-date\+amount deposit pair", raw)
    if m:
        n = int(m.group(1))
        return f"{_plural(n, 'duplicate pair')}"
    return raw


def _fmt_synthetic_low_variance(raw: str) -> str:
    # "CV=12.3% across 18 deposits"
    m = re.match(r"CV=([\d.]+)%\s*across\s*(\d+)\s*deposits", raw)
    if m:
        return f"CV {m.group(1)}% across {m.group(2)} deposits"
    return raw


def _fmt_round_number_deposits(raw: str) -> str:
    # "78% of deposits are exact $100 multiples"
    m = re.match(r"(\d+)% of deposits", raw)
    if m:
        return f"{m.group(1)}% land on $100 multiples"
    return raw


def _fmt_preloan_spike(raw: str) -> str:
    # "7d spike last_week=$X vs avg=$Y"  OR  "14d spike last_14d=$X vs avg=$Y"
    m = re.match(
        r"(?P<win>\d+)d spike last_(?:week|14d)=\$(?P<amt>[\d.,]+)\s*vs avg=\$(?P<avg>[\d.,]+)",
        raw,
    )
    if m:
        amt = _short_money(m.group("amt"))
        avg = _short_money(m.group("avg"))
        return f"${amt} in {m.group('win')}d vs ${avg}/wk avg"
    return raw


def _fmt_nsf_clustering_short(raw: str) -> str:
    # "4 NSFs in 18 days"
    m = re.match(r"(\d+) NSFs in (\d+) days", raw)
    if m:
        return f"{m.group(1)} NSFs in {m.group(2)} days"
    return raw


def _fmt_nsf_late_concentration(raw: str) -> str:
    # "3 of 5 NSFs in last 30 days"
    m = re.match(r"(\d+) of (\d+) NSFs in last 30 days", raw)
    if m:
        return f"{m.group(1)} of {m.group(2)} NSFs in last 30d"
    return raw


def _fmt_recent_account_opening(raw: str) -> str:
    # "statement begins 41 days before today"
    m = re.match(r"statement begins (\d+) days before today", raw)
    if m:
        return f"period starts {m.group(1)} days ago"
    return raw


def _fmt_deposit_velocity_spike(raw: str) -> str:
    # "7d window ending 2026-05-20: 32 deposits vs baseline 12.5/wk"
    m = re.search(r":\s*(\d+)\s*deposits vs baseline\s*([\d.]+)/wk", raw)
    if m:
        return f"{m.group(1)} deposits in 7d vs {m.group(2)}/wk baseline"
    return raw


def _fmt_unreconciled_internal_transfer(raw: str) -> str:
    # "2 transfer-out leg(s) > $500 with no matching transfer-in — possible hidden account"
    m = re.match(r"(\d+) transfer-out", raw)
    if m:
        n = int(m.group(1))
        return _plural(n, "unmatched transfer-out > $500")
    return raw


def _fmt_customer_concentration(raw: str) -> str:
    # "top counterparty = 78% of revenue (acme corp)"
    m = re.match(r"top counterparty\s*=\s*(\d+)%\s*of revenue\s*\((?P<payee>[^)]*)\)", raw)
    if m:
        payee = _title_case_payee(m.group("payee"))
        return f"{payee} ({m.group(1)}%)"
    return raw


def _fmt_chargeback_velocity(raw: str) -> str:
    # variants:
    # "5 chargeback/refund debits in 18 days"
    # "5 chargeback/refund debits over period"
    # "last-14d chargebacks: 8 vs prior 2.0/fortnight"
    m = re.match(r"(\d+) chargeback/refund debits", raw)
    if m:
        return f"{m.group(1)} chargebacks/refunds"
    m = re.match(r"last-14d chargebacks:\s*(\d+)", raw)
    if m:
        return f"{m.group(1)} chargebacks in last 14d"
    return raw


def _fmt_processor_holdback(raw: str) -> str:
    # "10 processor payouts; daily CV=64% — possible MCA holdback in force"
    m = re.match(r"(\d+) processor payouts;\s*daily CV=(\d+)%", raw)
    if m:
        return f"processor CV {m.group(2)}% over {m.group(1)} payouts"
    return raw


def _fmt_fraud_cluster_triangulation(raw: str) -> str:
    # "4_signals_mca_stacking,wash_deposit_suspected,paydown_mca_suspected,withdrawal_acceleration"
    # The constituent code list stays out of the chip text — the inline
    # expander (driven by HumanFlag.cluster_signals) renders the
    # humanized signal names. Chip text just carries the count so
    # workers see "Fraud cluster triangulated · 4 contributing signals"
    # at a glance.
    m = re.match(r"^(\d+)_signals_", raw)
    if m:
        n = int(m.group(1))
        return _plural(n, "contributing signal")
    return raw


def _fmt_payroll_absent(raw: str) -> str:
    # "no payroll-processor activity over 28 days with $84250 revenue"
    m = re.match(r"no payroll-processor activity over (\d+) days with \$(\d+)", raw)
    if m:
        return f"no payroll in {m.group(1)}d at ${_short_money(m.group(2))} revenue"
    return raw


# --- metadata layer (PDF tampering) -----------------------------------------


def _fmt_incremental_saves(raw: str) -> str:
    # "2 EOF markers"
    m = re.match(r"(\d+) EOF markers", raw)
    if m:
        return f"{m.group(1)} EOF markers"
    return raw


def _fmt_editor_detected(raw: str) -> str:
    # detail is the producer name verbatim — already human
    return raw


def _fmt_personal_author(raw: str) -> str:
    return f"author '{raw}'"


def _fmt_page_size_inconsistency(raw: str) -> str:
    # "612x792, 612x1008"
    return f"mixed page sizes ({raw})"


def _fmt_font_inconsistency(raw: str) -> str:
    # "3 page(s) have no font overlap"
    m = re.match(r"(\d+) page", raw)
    if m:
        n = int(m.group(1))
        return f"{_plural(n, 'page')} with no font overlap"
    return raw


def _fmt_page_layer_anomaly(raw: str) -> str:
    # "2 page(s) have an off-mode /Contents stream count"
    m = re.match(r"(\d+) page", raw)
    if m:
        n = int(m.group(1))
        return f"{_plural(n, 'page')} with off-mode content stream"
    return raw


# --- aggregate soft signals -------------------------------------------------


def _fmt_top_counterparty_concentration(raw: str) -> str:
    # "78%_(payee)"  (aggregate.py format)
    m = re.match(r"(\d+)%_\((?P<payee>[^)]*)\)", raw)
    if m:
        payee = _title_case_payee(m.group("payee"))
        return f"{payee} ({m.group(1)}%)"
    return raw


def _fmt_payroll_cadence(raw: str) -> str:
    # "weekly_15%_of_revenue" | "biweekly_12%_of_revenue" | "irregular" | "irregular_count_1"
    if raw == "irregular_count_1":
        return "single payroll event"
    m = re.match(r"(?P<cadence>[a-z]+)_(\d+)%_of_revenue", raw)
    if m:
        return f"{m.group('cadence')}, {m.group(2)}% of revenue"
    # bare cadence (no revenue pct)
    if raw in {"weekly", "biweekly", "monthly", "irregular"}:
        return raw
    return raw


def _fmt_nsf_on_negative_days(raw: str) -> str:
    # "N_of_M"
    m = re.match(r"(\d+)_of_(\d+)", raw)
    if m:
        return f"{m.group(1)} of {m.group(2)} NSFs on negative days"
    return raw


def _fmt_adb_partial_coverage(raw: str) -> str:
    # "N/M"
    m = re.match(r"(\d+)/(\d+)", raw)
    if m:
        return f"ADB covered {m.group(1)} of {m.group(2)} days"
    return raw


# --- confidence layer -------------------------------------------------------


def _fmt_classification_confidence_below_floor(raw: str) -> str:
    # "avg=56 floor=60"
    m = re.search(r"avg=(\d+)\s*floor=(\d+)", raw)
    if m:
        return f"avg {m.group(1)} vs {m.group(2)} required"
    return raw


# --- U18 shadow-flag families (R0.2 / R1.x / R3.4 / R4.4 / R4.6 / U10 /
#       U12 / H8 / M9 / R0.4 H7) ----------------------------------------
#
# Detail-string formats are documented at the emission site; the
# formatters below mirror those formats line-for-line. When a regex
# misses (parser-side format drift) the formatters return ``raw``
# verbatim — same fallback contract the legacy formatters use, so
# shadow chips stay readable even when the parser ships a new field.


def _fmt_lender_proceeds_excluded(raw: str) -> str:
    # "{count}_${total}_({name1|name2|...})" — emitted by aggregate.py
    # when an LLM-misclassified MCA / SBA / LOC deposit is filtered out
    # of true_revenue. ``names`` are joined with ``|`` to keep them
    # parseable; we render them comma-joined for the chip text.
    m = re.match(r"(\d+)_\$([\d.,]+)_\((?P<names>[^)]*)\)", raw)
    if m:
        count = int(m.group(1))
        total = _short_money(m.group(2))
        names_raw = m.group("names")
        names = ", ".join(n.strip() for n in names_raw.split("|") if n.strip())
        if names:
            return f"{_plural(count, 'deposit')} excluded · ${total} · {names}"
        return f"{_plural(count, 'deposit')} excluded · ${total}"
    return raw


def _fmt_lender_proceeds_excluded_row(raw: str) -> str:
    # "{matched_name}_${amount}_{source_id}" — one per excluded row.
    # Detail-only flag; surfaced on the audit CSV and the shadow dossier
    # panel for drill-down. UUIDs are noise on a chip; strip them.
    m = re.match(r"(?P<name>[^_]+(?:_[^_$]+)*)_\$(?P<amount>[\d.,]+)_(?P<uuid>[^_]+)$", raw)
    if m:
        amount = _short_money(m.group("amount"))
        return f"{m.group('name')} · ${amount}"
    return raw


def _fmt_mca_position_fuzzy_candidate(raw: str) -> str:
    # "{funder}_{ratio}_{count}_{first_iso}_{last_iso}" — emitted by
    # patterns._detect_fuzzy_mca_candidates. ``ratio`` is the best
    # fuzzy-match similarity (0..1) rendered as ``0.91`` etc.
    m = re.match(
        r"(?P<funder>[^_]+)_(?P<ratio>[\d.]+)_(?P<count>\d+)_"
        r"(?P<first>\d{4}-\d{2}-\d{2})_(?P<last>\d{4}-\d{2}-\d{2})$",
        raw,
    )
    if m:
        ratio_pct = round(float(m.group("ratio")) * 100)
        return (
            f"{m.group('funder')} · {m.group('count')} hits · "
            f"{ratio_pct}% similarity · {m.group('first')}→{m.group('last')}"
        )
    return raw


def _fmt_mca_disguise_candidate(raw: str) -> str:
    # "{term}_{count}_{median_int}" — emitted by
    # patterns._detect_disguise_candidates. ``median_int`` is the median
    # spacing between debits in days, rounded to int.
    m = re.match(r"(?P<term>.+?)_(?P<count>\d+)_(?P<median>\d+)$", raw)
    if m:
        return (
            f"'{m.group('term')}' · {m.group('count')} debits · "
            f"{m.group('median')}d median cadence"
        )
    return raw


def _fmt_mca_same_day_cluster(raw: str) -> str:
    # "{iso_date}_{funder_count}_({A|B|C})" — emitted by
    # patterns._detect_same_day_cluster. Funder names joined with ``|``.
    m = re.match(
        r"(?P<date>\d{4}-\d{2}-\d{2})_(?P<count>\d+)_\((?P<funders>[^)]*)\)$",
        raw,
    )
    if m:
        funders = ", ".join(
            f.strip() for f in m.group("funders").split("|") if f.strip()
        )
        return f"{m.group('date')} · {m.group('count')} funders · {funders}"
    return raw


def _fmt_daily_balance_continuity_break(raw: str) -> str:
    # "{iso_date}_expected_{x}_actual_{y}_diff_{d}" — emitted by
    # validate._shadow_check_daily_balance_continuity. All three monies
    # are Decimal strings. Tight chip text — full numbers stay in the
    # description.
    m = re.match(
        r"(?P<date>\d{4}-\d{2}-\d{2})_expected_(?P<exp>[-\d.]+)"
        r"_actual_(?P<act>[-\d.]+)_diff_(?P<diff>[-\d.]+)$",
        raw,
    )
    if m:
        return f"{m.group('date')} · off by ${m.group('diff')}"
    return raw


def _fmt_daily_balance_continuity_breaks_count(raw: str) -> str:
    # "{N}" — summary count emitted alongside the per-day breaks.
    m = re.match(r"(\d+)$", raw)
    if m:
        return _plural(int(m.group(1)), "day off-by-cents")
    return raw


def _fmt_transaction_id_sequence_gap(raw: str) -> str:
    # "{from}_{to}_{missing}" — emitted by
    # validate._shadow_check_transaction_id_sequence_gaps.
    m = re.match(r"(?P<frm>\d+)_(?P<to>\d+)_(?P<missing>\d+)$", raw)
    if m:
        return (
            f"id {m.group('frm')}→{m.group('to')} · "
            f"{_plural(int(m.group('missing')), 'row')} missing"
        )
    return raw


def _fmt_adb_coverage_thin(raw: str) -> str:
    # "skip_ratio={n}pct_threshold={t}pct_would_route_review" — emitted
    # by pipeline._adb_coverage_thin_flag.
    m = re.match(
        r"skip_ratio=(?P<ratio>\d+)pct_threshold=(?P<thresh>\d+)pct",
        raw,
    )
    if m:
        return (
            f"{m.group('ratio')}% of days skipped "
            f"(threshold {m.group('thresh')}%)"
        )
    return raw


def _fmt_nsf_corroboration_missing(raw: str) -> str:
    # "{iso_date}_${amount}_{snippet}_would_route_review" — emitted by
    # nsf_secondary. ``snippet`` is a short description fragment.
    m = re.match(
        r"(?P<date>\d{4}-\d{2}-\d{2})_\$(?P<amount>[\d.,]+)_(?P<rest>.+)$",
        raw,
    )
    if m:
        amount = _short_money(m.group("amount"))
        snippet = m.group("rest").replace("_would_route_review", "").strip("_")
        if snippet:
            return f"{m.group('date')} · ${amount} · {snippet}"
        return f"{m.group('date')} · ${amount}"
    return raw


def _fmt_nsf_low_confidence(raw: str) -> str:
    # "{iso_date}_${amount}_conf{N}_{snippet}_would_route_review"
    m = re.match(
        r"(?P<date>\d{4}-\d{2}-\d{2})_\$(?P<amount>[\d.,]+)"
        r"_conf(?P<conf>\d+)_(?P<rest>.+)$",
        raw,
    )
    if m:
        amount = _short_money(m.group("amount"))
        snippet = m.group("rest").replace("_would_route_review", "").strip("_")
        if snippet:
            return (
                f"{m.group('date')} · ${amount} · "
                f"classifier {m.group('conf')}% · {snippet}"
            )
        return f"{m.group('date')} · ${amount} · classifier {m.group('conf')}%"
    return raw


def _fmt_state_enforcement_concern(raw: str) -> str:
    # Three known suffixes:
    #   TX_HB700_tx_merchant_review
    #   FL_GA_advance_fee_prohibition
    #   FL_GA_advance_fee_prohibition_for_this_funder
    # Spell them out rather than de-snake-casing programmatically so the
    # chip reads cleanly.
    if raw == "TX_HB700_tx_merchant_review":
        return "Texas HB 700 — merchant review"
    if raw == "FL_GA_advance_fee_prohibition":
        return "FL / GA advance-fee prohibition"
    if raw == "FL_GA_advance_fee_prohibition_for_this_funder":
        return "FL / GA advance-fee prohibition (this funder)"
    return raw


def _fmt_seasonality_recategorized(raw: str) -> str:
    # "cv={cv}_naics={naics}_would_skip_volatility_penalty"
    m = re.match(
        r"cv=(?P<cv>[\d.]+)_naics=(?P<naics>[^_]+)",
        raw,
    )
    if m:
        return f"CV {m.group('cv')} · NAICS {m.group('naics')} · seasonal"
    return raw


def _fmt_seasonality_observed_but_volatility_extreme(raw: str) -> str:
    # "cv={cv}_naics={naics}_penalty_still_applied"
    m = re.match(
        r"cv=(?P<cv>[\d.]+)_naics=(?P<naics>[^_]+)",
        raw,
    )
    if m:
        return f"CV {m.group('cv')} · NAICS {m.group('naics')} · extreme"
    return raw


def _fmt_eof_policy_mismatch(raw: str) -> str:
    # Exactly one known detail today:
    # "scorer_declines_at_2_pipeline_routes_review"
    if raw == "scorer_declines_at_2_pipeline_routes_review":
        return "scorer declines at 2 EOF; pipeline routes to review"
    return raw


def _fmt_tib_ramp_shadow(raw: str) -> str:
    # "months={N}_current_delta={X}_graduated_delta={Y}" — both deltas
    # are signed integers (e.g. -15, -8, -5, -2, 0).
    m = re.match(
        r"months=(?P<m>\d+)_current_delta=(?P<cur>-?\d+)"
        r"_graduated_delta=(?P<grad>-?\d+)$",
        raw,
    )
    if m:
        return (
            f"{m.group('m')} mo · current {m.group('cur')} → "
            f"graduated {m.group('grad')}"
        )
    return raw


def _fmt_structured_deposit_cluster(raw: str) -> str:
    # "N_deposits_in_14_day_window_dates=YYYYMMDD,YYYYMMDD,..."
    m = re.match(
        r"(?P<n>\d+)_deposits_in_(?P<window>\d+)_day_window_dates=(?P<dates>.+)$",
        raw,
    )
    if m:
        dates = m.group("dates").split(",")
        return (
            f"{m.group('n')} in-band deposits · {m.group('window')}d window · "
            f"first {dates[0]}"
        )
    return raw


def _fmt_duplicate_pdf_upload(raw: str) -> str:
    # "sha256_match_with_doc={uuid}:uploaded={iso}[:total_prior_copies={n}]"
    # Built by joining ``:``-separated key=value parts. UUIDs are noise;
    # surface the upload timestamp + copy count.
    parts = raw.split(":")
    pieces: dict[str, str] = {}
    for part in parts:
        if "=" in part:
            k, v = part.split("=", 1)
            pieces[k.strip()] = v.strip()
    uploaded = pieces.get("uploaded", "")
    copies = pieces.get("total_prior_copies", "")
    if uploaded and copies:
        return f"prior upload {uploaded} · {copies} prior copies"
    if uploaded:
        return f"prior upload {uploaded}"
    return raw


def _fmt_related_account_suspected(raw: str) -> str:
    # "holder={name}:existing_last4={a,b}:new_last4={c}"
    pieces: dict[str, str] = {}
    for part in raw.split(":"):
        if "=" in part:
            k, v = part.split("=", 1)
            pieces[k.strip()] = v.strip()
    holder = pieces.get("holder", "")
    existing = pieces.get("existing_last4", "")
    new_last4 = pieces.get("new_last4", "")
    if holder and existing and new_last4:
        return f"{holder} · prior last4 {existing} · new last4 {new_last4}"
    return raw


# --- U30 scoring-engine cutover (score.py + scoring_v2) --------------
#
# Detail-string formats are emitted by ``_check_hard_declines`` in
# scoring/score.py:
#   scoring_engine_active:legacy
#   scoring_engine_active:track_abc
#   track_a_integrity_review:branch={branch}
#   track_a_integrity_fail:branch={branch}
#   track_b_elevated_risk        (no detail body)
#   track_b_high_risk            (no detail body)
#
# Track A ``branch`` values come from scoring_v2/track_a/compute.py:
# ``strong_metadata``, ``drift_plus_editor``, ``medium_corroborated``,
# ``drift_alone``, ``clean``. Humanize them at the display layer rather
# than re-encoding the list at emission so the scorer stays oblivious to
# UI copy.


_TRACK_A_BRANCH_LABELS: Final[dict[str, str]] = {
    "strong_metadata": "strong metadata signal",
    "drift_plus_editor": "reconciliation drift + editor",
    "medium_corroborated": "medium signal corroborated",
    "drift_alone": "reconciliation drift alone",
    "clean": "clean",
}


def _fmt_scoring_engine_active(raw: str) -> str:
    # "legacy" / "track_abc" — the active scoring engine. Render the
    # engine name verbatim; the description carries the operator
    # context (which fields drive declines under that engine).
    engine = raw.strip()
    if engine == "legacy":
        return "legacy (fraud_score)"
    if engine == "track_abc":
        return "track_abc (Track A + Track B)"
    return raw


def _fmt_track_a_branch(raw: str) -> str:
    # "branch={branch}" — extract the branch name and humanize via the
    # known-branch map. Unknown branches degrade to the raw token so a
    # future scoring_v2 change still renders something readable.
    m = re.match(r"branch=(?P<branch>.+)$", raw)
    if not m:
        return raw
    branch = m.group("branch").strip()
    return _TRACK_A_BRANCH_LABELS.get(branch, branch)


# --- helpers ---------------------------------------------------------------


def _title_case_payee(label: str) -> str:
    """Title-case a payee label for display.

    Bank ACH descriptors arrive uppercase; ``_clean_payee_label`` in
    aggregate.py lowercases them for case-insensitive bucketing. That's
    right for the math, but the lowercase form reads like sloppy data on
    a chip — "payward interactive" looks unprofessional. Title-casing
    only at the display layer keeps bucketing case-insensitive while the
    worker sees "Payward Interactive".

    Empty / whitespace input passes through so the formatters can guard
    on the same fallback contract they already use.
    """
    cleaned = label.strip()
    if not cleaned:
        return cleaned
    return cleaned.title()


def _short_money(raw_amount: str) -> str:
    """Compress ``"694311.35"`` -> ``"694k"`` for chip display.

    Strips commas, parses an int, then renders k/M for large amounts.
    Falls back to the raw string when parsing fails (keeps the chip
    readable rather than blank on unexpected input).
    """
    cleaned = raw_amount.replace(",", "").replace("$", "")
    try:
        amount = int(float(cleaned))
    except ValueError:
        return raw_amount
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if amount >= 1_000:
        return f"{amount // 1000}k"
    return str(amount)


# ---------------------------------------------------------------------------
# Registry — one entry per known flag code
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FlagSpec:
    title: str
    category: CategoryName
    severity_band: SeverityBand
    formatter: Callable[[str], str] | None
    description: str = ""


def _spec(
    title: str,
    category: CategoryName,
    severity_band: SeverityBand,
    formatter: Callable[[str], str] | None,
    description: str = "",
) -> _FlagSpec:
    """Tiny wrapper so the registry entries stay readable under ruff's
    100-char line limit.

    ``description`` is the operator-facing one-sentence rationale the
    discreet "Operator review signals (shadow mode)" dossier section
    renders next to title + detail for the U18 shadow-flag families.
    Empty string for legacy chip-only flags where the title + detail
    suffice."""
    return _FlagSpec(
        title=title,
        category=category,
        severity_band=severity_band,
        formatter=formatter,
        description=description,
    )


_FLAG_REGISTRY: Final[dict[str, _FlagSpec]] = {
    # === Stacking & funder position ====================================
    "mca_stacking": _spec(
        "MCA stacking", "stacking", "material", _fmt_mca_stacking,
    ),
    "mca_payoff_signature": _spec(
        "Recent MCA payoff", "stacking", "look_closer", _fmt_mca_payoff,
    ),
    "paydown_mca_suspected": _spec(
        "MCA paydown pattern", "stacking", "material", _fmt_paydown_mca,
    ),
    "withdrawal_acceleration": _spec(
        "MCA debit acceleration", "stacking", "material",
        _fmt_withdrawal_acceleration,
    ),
    "acceleration_clause_triggered": _spec(
        "MCA acceleration", "stacking", "decline", _fmt_acceleration_clause,
    ),
    "unauthorized_withdrawal_dispute": _spec(
        "Unauthorized withdrawal dispute", "stacking", "decline",
        _fmt_unauthorized_withdrawal_dispute,
    ),

    # === Revenue fabrication ===========================================
    "wash_deposit_suspected": _spec(
        "Suspected wash deposits", "fabrication", "decline", _fmt_wash_deposit,
    ),
    "duplicate_deposits_detected": _spec(
        "Duplicate deposits", "fabrication", "material",
        _fmt_duplicate_deposits,
    ),
    "synthetic_low_variance": _spec(
        "Deposits look synthetic", "fabrication", "material",
        _fmt_synthetic_low_variance,
    ),
    "round_number_deposits": _spec(
        "Round-number deposits", "fabrication", "material",
        _fmt_round_number_deposits,
    ),
    "preloan_spike": _spec(
        "Pre-loan deposit spike", "fabrication", "material", _fmt_preloan_spike,
    ),
    "deposit_velocity_spike": _spec(
        "Deposit velocity spike", "fabrication", "material",
        _fmt_deposit_velocity_spike,
    ),

    # === Cashflow stress ===============================================
    "nsf_clustering_short": _spec(
        "NSF concentration", "stress", "material", _fmt_nsf_clustering_short,
    ),
    "nsf_late_concentration": _spec(
        "Late NSF concentration", "stress", "material",
        _fmt_nsf_late_concentration,
    ),
    "payroll_absent": _spec(
        "Payroll absent", "stress", "look_closer", _fmt_payroll_absent,
    ),

    # === Concentration & fundamentals ==================================
    "customer_concentration": _spec(
        "Customer concentration", "concentration", "material",
        _fmt_customer_concentration,
    ),
    "processor_holdback_detected": _spec(
        "Processor holdback suspected", "concentration", "material",
        _fmt_processor_holdback,
    ),
    "chargeback_velocity": _spec(
        "Chargeback velocity", "concentration", "material",
        _fmt_chargeback_velocity,
    ),

    # === Hidden account ================================================
    "unreconciled_internal_transfer": _spec(
        "Unreconciled internal transfer", "hidden_account", "material",
        _fmt_unreconciled_internal_transfer,
    ),

    # === Recency / account age =========================================
    "recent_account_opening": _spec(
        "New account", "recency", "material", _fmt_recent_account_opening,
    ),

    # === PDF tampering (metadata layer) ================================
    "incremental_saves": _spec(
        "PDF saved incrementally", "tampering", "decline",
        _fmt_incremental_saves,
    ),
    "editor_detected": _spec(
        "PDF editor detected", "tampering", "material", _fmt_editor_detected,
    ),
    "personal_author": _spec(
        "Personal author on PDF", "tampering", "material", _fmt_personal_author,
    ),
    "stripped_metadata": _spec(
        "PDF metadata stripped", "tampering", "material", None,
    ),
    "page_size_inconsistency": _spec(
        "Page size mismatch", "tampering", "decline",
        _fmt_page_size_inconsistency,
    ),
    "xref_offset_mismatch": _spec(
        "PDF xref offset mismatch", "tampering", "material", None,
    ),
    "font_inconsistency": _spec(
        "Font inconsistency", "tampering", "material", _fmt_font_inconsistency,
    ),
    "page_layer_anomaly": _spec(
        "Page layer anomaly", "tampering", "look_closer", _fmt_page_layer_anomaly,
    ),
    # ``modified_*_after_creation`` is matched dynamically below — the
    # minute count is baked into the code rather than the detail.

    # === Aggregate soft signals ========================================
    "top_counterparty_concentration": _spec(
        "Top customer", "soft", "context",
        _fmt_top_counterparty_concentration,
    ),
    "payroll_cadence": _spec(
        "Payroll cadence", "soft", "context", _fmt_payroll_cadence,
    ),
    "nsf_on_negative_days": _spec(
        "NSFs on negative-balance days", "soft", "context",
        _fmt_nsf_on_negative_days,
    ),
    "adb_partial_coverage": _spec(
        "ADB coverage gap", "soft", "context", _fmt_adb_partial_coverage,
    ),

    # === Classifier confidence =========================================
    "classification_confidence_below_floor": _spec(
        "Transaction classifier confidence low", "soft", "look_closer",
        _fmt_classification_confidence_below_floor,
    ),

    # === Infrastructure (META prefix non-pattern) ======================
    "ocr_fallback_used": _spec(
        "OCR fallback used", "soft", "context", None,
    ),
    "per_page_routing_used": _spec(
        "Per-page routing used", "soft", "context", None,
    ),

    # === Composite — triangulation across multiple patterns ============
    # Detail shape is ``"N_signals_code1,code2,..."`` (codes are the
    # contributing Pattern codes). The cluster-signal parser below
    # attaches the humanized constituents onto the HumanFlag so the
    # template renders them as an inline expander.
    "fraud_cluster_triangulated": _spec(
        "Fraud cluster triangulated", "composite", "decline",
        _fmt_fraud_cluster_triangulation,
    ),

    # === U18 shadow-mode families ======================================
    # Severity band is ``context`` so the chips render muted; every entry
    # carries a one-sentence description for the discreet "Operator
    # review signals (shadow mode)" dossier section. None of these
    # contribute to fraud_score / hard-decline / tier — shadow only.

    # -- R0.2 lender-proceeds exclusion (aggregate.py) -------------------
    "lender_proceeds_excluded": _spec(
        "Lender proceeds excluded from revenue", "shadow", "context",
        _fmt_lender_proceeds_excluded,
        description=(
            "Deposits classified as MCA / SBA / LOC funder proceeds were "
            "filtered out of true_revenue. Verify the funder names match "
            "the merchant's disclosed obligations and that the exclusion "
            "isn't double-counting a legitimate refund."
        ),
    ),
    "lender_proceeds_excluded_row": _spec(
        "Lender proceeds excluded — row", "shadow", "context",
        _fmt_lender_proceeds_excluded_row,
        description=(
            "Per-row evidence for the lender-proceeds exclusion above. "
            "One entry per excluded transaction; the source-id ties back "
            "to the audit CSV."
        ),
    ),

    # -- R1.1 fuzzy MCA-funder candidates (patterns.py) ------------------
    "mca_position_fuzzy_candidate": _spec(
        "Possible MCA stacking — descriptor variant", "shadow", "context",
        _fmt_mca_position_fuzzy_candidate,
        description=(
            "Debits with descriptors that fuzzy-match a known MCA funder "
            "name (similarity score ≥85%) without an exact substring hit. "
            "Likely a typo / abbreviation variant of a real position; "
            "confirm whether the merchant disclosed this funder."
        ),
    ),
    "mca_disguise_candidate": _spec(
        "Possible MCA — generic descriptor cadence", "shadow", "context",
        _fmt_mca_disguise_candidate,
        description=(
            "A product-neutral phrase ('settlement advance', 'revenue "
            "based financing', etc.) appears on 10+ debits with ≤2-day "
            "median spacing — the cadence of a real MCA holdback hiding "
            "behind generic language. Confirm with the merchant."
        ),
    ),

    # -- R1.3 same-day funder cluster (patterns.py) ----------------------
    "mca_same_day_cluster": _spec(
        "Multiple funders same day", "shadow", "context",
        _fmt_mca_same_day_cluster,
        description=(
            "Three or more distinct MCA funders debited on the same "
            "business day — strong indicator of late-stage stacking. "
            "Cross-check against the disclosed position list."
        ),
    ),

    # -- R1.4 daily balance continuity (validate.py) ---------------------
    "daily_balance_continuity_break": _spec(
        "Daily balance off by cents", "shadow", "context",
        _fmt_daily_balance_continuity_break,
        description=(
            "A day's expected closing balance and the next day's opening "
            "balance disagree by ≥$0.01 (the routing-level check uses "
            "$1.00). Often a benign rounding artifact, but can flag "
            "surgical row swaps that shift cents without breaking the "
            "looser gate."
        ),
    ),
    "daily_balance_continuity_breaks_count": _spec(
        "Daily balance — break count", "shadow", "context",
        _fmt_daily_balance_continuity_breaks_count,
        description=(
            "Summary count of per-day continuity breaks across the "
            "statement. One break is usually noise; a cluster is worth "
            "a second look."
        ),
    ),

    # -- R1.5 transaction-id gaps (validate.py) --------------------------
    "transaction_id_sequence_gap": _spec(
        "Transaction-id sequence gap", "shadow", "context",
        _fmt_transaction_id_sequence_gap,
        description=(
            "A populated sequential id / reference / confirmation column "
            "skips one or more numbers. Possible evidence of deleted "
            "rows in the source PDF — verify against an alternate "
            "statement format if available."
        ),
    ),

    # -- R1.7 ADB coverage thin (pipeline.py) ----------------------------
    "adb_coverage_thin": _spec(
        "Average daily balance — thin coverage", "shadow", "context",
        _fmt_adb_coverage_thin,
        description=(
            "More than 10% of days in the statement window are missing "
            "a daily-balance anchor, so the avg_daily_balance metric is "
            "computed over too few days to be trusted. Under the "
            "shadow-mode policy this would route the doc to manual "
            "review; ship it past once the operator has corpus-validated "
            "the threshold."
        ),
    ),

    # -- R1.8 NSF secondary (nsf_secondary.py) ---------------------------
    "nsf_corroboration_missing": _spec(
        "NSF lacks corroboration", "shadow", "context",
        _fmt_nsf_corroboration_missing,
        description=(
            "An NSF-fee row fired but the surrounding evidence (negative "
            "running balance, same-day or day-1 chargeback / return "
            "token) is absent. Could be a misclassification — confirm "
            "the row is a real NSF before letting it drive the NSF "
            "count metrics."
        ),
    ),
    "nsf_low_confidence": _spec(
        "NSF — classifier low confidence", "shadow", "context",
        _fmt_nsf_low_confidence,
        description=(
            "An NSF-fee row was emitted with a classification confidence "
            "below 80. Same row may also fire 'NSF lacks corroboration' "
            "(independent signals). Spot-check the description before "
            "trusting the NSF count."
        ),
    ),

    # -- R3.4 state-by-state enforcement signals (score.py) --------------
    "state_enforcement_concern": _spec(
        "State enforcement concern", "shadow", "context",
        _fmt_state_enforcement_concern,
        description=(
            "Merchant state + funder profile lands on a known regulatory "
            "watchlist (TX HB 700 merchant review, FL / GA advance-fee "
            "prohibition). Operator-side review hint only; no tier or "
            "recommendation change."
        ),
    ),

    # -- R4.4 industry-aware seasonality (score.py) ----------------------
    "seasonality_recategorized": _spec(
        "Seasonality — penalty would be skipped", "shadow", "context",
        _fmt_seasonality_recategorized,
        description=(
            "Revenue CV is high but the merchant's NAICS prefix is on "
            "the known-seasonal list AND the CV sits inside the seasonal "
            "ceiling. Under the proposed policy the volatility penalty "
            "would be skipped for this deal; current scoring still "
            "applies the penalty until the operator flips the rule live."
        ),
    ),
    "seasonality_observed_but_volatility_extreme": _spec(
        "Seasonality — penalty still applied", "shadow", "context",
        _fmt_seasonality_observed_but_volatility_extreme,
        description=(
            "Merchant is in a known-seasonal industry, but the revenue "
            "CV exceeds even the seasonal ceiling. The volatility "
            "penalty stays in force; the shadow flag documents that "
            "seasonality was considered and rejected."
        ),
    ),

    # -- R4.6 EOF policy mismatch (score.py) -----------------------------
    "eof_policy_mismatch": _spec(
        "EOF policy mismatch", "shadow", "context",
        _fmt_eof_policy_mismatch,
        description=(
            "The legacy scorer hard-declines at >1 EOF marker while the "
            "pipeline treats 2 EOFs as a review-routing signal. Flag "
            "documents the divergence so the operator can flip the "
            "scorer side via config without re-deploy."
        ),
    ),

    # -- H8 graduated TIB penalty (score.py) -----------------------------
    "tib_ramp_shadow": _spec(
        "Time-in-business — graduated penalty", "shadow", "context",
        _fmt_tib_ramp_shadow,
        description=(
            "Documents what a graduated TIB penalty (-15 / -8 / -5 / -2 / "
            "0 across 3-23 months) would deduct for this merchant vs. "
            "the live -15 / -8 / 0 bands. Operator validates against the "
            "corpus before flipping severity via config."
        ),
    ),

    # -- M9 BSA structured-deposit cluster (patterns.py) -----------------
    "structured_deposit_cluster": _spec(
        "Possible deposit structuring", "shadow", "context",
        _fmt_structured_deposit_cluster,
        description=(
            "Three or more deposits in the $8,500 - $9,999.99 band "
            "within a 14-day rolling window — the FinCEN textbook "
            "structuring pattern (31 USC §5324). Cash-only caveat: "
            "AEGIS can't distinguish cash from wires from a bank "
            "statement row. Drill into the source rows before treating "
            "as a real signal."
        ),
    ),

    # -- U12 cross-statement detector (merchants/cross_statement_detector.py)
    "duplicate_pdf_upload": _spec(
        "Duplicate PDF upload", "shadow", "context",
        _fmt_duplicate_pdf_upload,
        description=(
            "Same SHA-256 already uploaded for this merchant. The second "
            "parse re-computes aggregates against byte-identical data — "
            "the dashboard then shows 2x deposits for that period. "
            "Reconcile uploads before trusting any merchant-aggregated "
            "metric."
        ),
    ),
    "related_account_suspected": _spec(
        "Related account suspected", "shadow", "context",
        _fmt_related_account_suspected,
        description=(
            "Same legal account holder appears with a new last-4 — "
            "either an undisclosed sibling account ('revenue hide') or "
            "an MCA-debit hideout ('solvency hide'). Request all bank "
            "account statements before submitting."
        ),
    ),

    # === U30 scoring-engine cutover (score.py) =========================
    # CLAUDE.md scoring-discipline: document integrity (Track A) and
    # business risk (Track B) stay separate forever; the engine cutover
    # is the operator-validated flip from the legacy blended fraud_score
    # to the three-track design. Each scoring pass emits which engine
    # fired so the dossier shows the active posture.

    # -- engine annotation (always emitted) ------------------------------
    "scoring_engine_active": _spec(
        "Scoring engine", "shadow", "context", _fmt_scoring_engine_active,
        description=(
            "Records which scoring engine produced this result. "
            "'legacy' uses the blended fraud_score >= 65 hard-decline "
            "rule; 'track_abc' makes fraud_score informational and "
            "moves the decline path to Track A (document integrity) + "
            "Track B (business risk). Flip via AEGIS_SCORING_ENGINE in "
            "/etc/aegis/aegis.env — no code deploy."
        ),
    ),

    # -- Track A review verdict (soft annotation) ------------------------
    "track_a_integrity_review": _spec(
        "Document integrity — review", "shadow", "context",
        _fmt_track_a_branch,
        description=(
            "Track A flagged the document for review but not auto-"
            "decline. The parse pipeline routes review verdicts to "
            "manual_review elsewhere; the scorer annotates the branch "
            "that fired (e.g. reconciliation drift alone, medium "
            "metadata signal corroborated) so the operator can see the "
            "specific integrity concern without re-opening the model."
        ),
    ),

    # -- Track B elevated band (soft annotation) -------------------------
    "track_b_elevated_risk": _spec(
        "Business risk — elevated band", "shadow", "context", None,
        description=(
            "Track B placed the deal in the 'elevated' business-risk "
            "band — measurably weaker than the 'standard' baseline but "
            "below the auto-decline 'high' band. Underwriter call: "
            "consider tighter pricing or stricter stipulations before "
            "submission rather than treating it as a clean deal."
        ),
    ),

    # -- Track A fail verdict (hard decline under track_abc) -------------
    # Decline severity — but stays in the shadow chip pool until
    # AEGIS_SCORING_ENGINE flips to track_abc, at which point the same
    # code appears in ScoreResult.hard_decline_reasons. Render at the
    # decline severity band so the chip is unambiguously bad when the
    # operator sees it on a track_abc deal.
    "track_a_integrity_fail": _spec(
        "Document integrity — fail (decline)", "shadow", "decline",
        _fmt_track_a_branch,
        description=(
            "Track A's near-binary integrity gate failed. Under the "
            "track_abc engine this is a hard decline reason; the "
            "branch identifies which integrity signal triggered "
            "(strong metadata, drift + editor, drift alone, etc.). "
            "Statement should not be submitted to funders."
        ),
    ),

    # -- Track B high band (hard decline under track_abc) ----------------
    "track_b_high_risk": _spec(
        "Business risk — high (decline)", "shadow", "decline", None,
        description=(
            "Track B placed the deal in the 'high' business-risk band. "
            "Under the track_abc engine this is a hard decline reason "
            "(matches BAND_TO_ACTION's review_decline_default). The "
            "business cannot reasonably support repayment given the "
            "merged cashflow + concentration + history signals."
        ),
    ),
}

# Default category + band when a recognized prefix carries an unknown code.
_PREFIX_DEFAULTS: Final[dict[str, tuple[CategoryName, SeverityBand]]] = {
    "META":       ("tampering", "material"),
    "MATH":       ("math", "material"),
    "PATTERN":    ("unknown", "material"),
    "AGGREGATE":  ("soft", "context"),
    "CONFIDENCE": ("soft", "look_closer"),
    "COMPOUND":   ("composite", "decline"),
    "WARN":       ("soft", "look_closer"),
}

# Modified-after-creation has the minute count in the *code* itself
# (``modified_120min_after_creation``) so a regex catches every variant.
_MODIFIED_AFTER_CREATION_RE: Final[re.Pattern[str]] = re.compile(
    r"^modified_(\d+)(min|h)_after_creation$"
)


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------


_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^\[(?P<prefix>[A-Z]+)\]\s*(?P<body>.*)$")

# Cluster detail shape: "N_signals_code1,code2,..."  (codes is the comma
# list).  Compiled once because the cluster chip is hot on dashboard
# renders where multiple triangulated merchants land in the queue.
_CLUSTER_DETAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d+_signals_(?P<codes>.+)$"
)


def _parse_cluster_signals(raw_detail: str) -> list[HumanFlag] | None:
    """Parse a ``fraud_cluster_triangulated`` detail string into the list of
    humanized constituent ``HumanFlag`` objects.

    Detail shape (emitted by ``pipeline._fraud_cluster_triangulation``):
    ``"N_signals_code1,code2,..."``. Each constituent is humanized as
    if it had arrived as ``[PATTERN] <code>`` (no detail body, so the
    constituent chip carries only its registered title + category +
    severity band — no count or amount, those would need the parser's
    per-pattern detail which the cluster string drops).

    Returns ``None`` when the detail doesn't match the expected shape so
    the chip degrades to a plain detail-text render rather than crashing
    or attaching half-data.

    Defensive: skips any constituent equal to
    ``"fraud_cluster_triangulated"`` to prevent infinite recursion. The
    pipeline never emits this shape — the guard exists so a future
    refactor that broke the contract surfaces as a missing chip rather
    than a stack overflow.
    """
    m = _CLUSTER_DETAIL_RE.match(raw_detail.strip())
    if not m:
        return None
    codes = [c.strip() for c in m.group("codes").split(",") if c.strip()]
    signals: list[HumanFlag] = []
    for code in codes:
        if code == "fraud_cluster_triangulated":
            continue
        signals.append(humanize_flag(f"[PATTERN] {code}"))
    return signals or None


def humanize_flag(raw: str) -> HumanFlag:
    """Return a ``HumanFlag`` for a raw ``all_flags`` entry.

    Unknown codes fall back to a usable shape (raw code as title, raw
    detail passed through, default category/band from the prefix) so a
    forgotten registration never crashes chip rendering.
    """
    if not isinstance(raw, str) or not raw.strip():
        return HumanFlag(
            code="",
            title="(empty flag)",
            detail="",
            category="unknown",
            severity_band="context",
        )

    prefix, body = _split_prefix(raw)
    code, raw_detail = _split_code_detail(body)

    # Pre-empt the dynamic metadata format.
    mod_match = _MODIFIED_AFTER_CREATION_RE.match(code)
    if mod_match:
        n, unit = mod_match.group(1), mod_match.group(2)
        # Title carries the action ("PDF modified"); detail carries the
        # timing ("120 min after creation"). Earlier the title repeated
        # "after creation" so the chip read as
        # "PDF modified after creation · 120 min after creation".
        return HumanFlag(
            code=code,
            title="PDF modified",
            detail=f"{n} {'min' if unit == 'min' else 'h'} after creation",
            category="tampering",
            severity_band="material" if unit == "min" else "decline",
        )

    spec = _FLAG_REGISTRY.get(code)
    if spec is not None:
        detail = spec.formatter(raw_detail) if spec.formatter else raw_detail
        cluster_signals: list[HumanFlag] | None = None
        if code == "fraud_cluster_triangulated":
            cluster_signals = _parse_cluster_signals(raw_detail)
        return HumanFlag(
            code=code,
            title=spec.title,
            detail=detail,
            category=spec.category,
            severity_band=spec.severity_band,
            cluster_signals=cluster_signals,
            description=spec.description,
        )

    # Unknown code — graceful fallback. Title comes from the code itself
    # (de-snake-cased so it's at least readable); detail is the raw tail
    # so the operator still sees the parser-emitted text.
    default_category, default_band = _PREFIX_DEFAULTS.get(
        prefix or "", ("unknown", "context")
    )
    return HumanFlag(
        code=code,
        title=_humanize_unknown_code(code),
        detail=raw_detail,
        category=default_category,
        severity_band=default_band,
    )


def _split_prefix(raw: str) -> tuple[str | None, str]:
    """Strip ``[CATEGORY] `` if present. Returns (prefix or None, body)."""
    m = _PREFIX_RE.match(raw)
    if m is None:
        return None, raw.strip()
    return m.group("prefix"), m.group("body").strip()


def _split_code_detail(body: str) -> tuple[str, str]:
    """Split ``code: detail`` or ``code:detail`` into (code, detail).

    Falls back to ``(body, "")`` for bare codes with no detail
    (``stripped_metadata``, ``xref_offset_mismatch``, etc.).
    """
    if ":" in body:
        code, detail = body.split(":", 1)
        return code.strip(), detail.strip()
    return body.strip(), ""


def _humanize_unknown_code(code: str) -> str:
    """De-snake-case an unknown code so the chip is at least readable.

    ``brand_new_detector_v2`` -> ``Brand new detector v2``. Used only for
    codes without a ``_FLAG_REGISTRY`` entry — the registered titles are
    hand-authored.
    """
    if not code:
        return "(unknown flag)"
    return code.replace("_", " ").strip().capitalize()


# ---------------------------------------------------------------------------
# Audit feed humanizer
# ---------------------------------------------------------------------------


def humanize_audit_action(
    action: str, details: dict[str, Any] | None = None
) -> str:
    """Operator-readable label for an audit row action.

    Same fallback contract as ``humanize_flag``: unknown actions render
    as their raw identifier so a new audit code never crashes the feed.

    Proposal 2 extends the submission action with funder names pulled
    from ``details.funder_names`` so the dashboard reads "recorded
    submission to OnDeck, Credibly" instead of just "recorded submission
    to funders". The audit row itself keeps the raw action string for
    downstream replay scripts and the funnel counter.
    """
    if action == "deal.submit_to_funders":
        names = (details or {}).get("funder_names") if details else None
        if isinstance(names, list) and names:
            joined = ", ".join(str(n) for n in names if n)
            if joined:
                return f"recorded submission to {joined}"
        return "recorded submission to funders"
    return action


__all__ = [
    "CategoryName",
    "FlagSourceTransaction",
    "HumanFlag",
    "SeverityBand",
    "humanize_audit_action",
    "humanize_flag",
]
