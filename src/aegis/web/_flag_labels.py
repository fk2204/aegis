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
from typing import Final, Literal

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
    "unknown",         # fallback for new codes without registration
]

SeverityBand = Literal["decline", "material", "look_closer", "context"]


@dataclass(frozen=True)
class HumanFlag:
    """Renderable shape for a single flag chip.

    ``code`` stays the raw identifier so audit, log, and ``data-flag-code``
    attributes keep their machine-readable form. The other fields are
    operator-facing.
    """

    code: str
    title: str
    detail: str
    category: CategoryName
    severity_band: SeverityBand

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
    m = re.match(r"(\d+) reversal credit", raw)
    if m:
        n = int(m.group(1))
        return _plural(n, "reversal vs prior MCA debit")
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
        return f"${_short_money(m.group('amt'))} in {m.group('win')}d vs ${_short_money(m.group('avg'))}/wk avg"
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
        return f"{m.group('payee').strip()} ({m.group(1)}%)"
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
        payee = m.group("payee").strip()
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


# --- helpers ---------------------------------------------------------------


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


_FLAG_REGISTRY: Final[dict[str, _FlagSpec]] = {
    # === Stacking & funder position ====================================
    "mca_stacking":                 _FlagSpec("MCA stacking", "stacking", "material", _fmt_mca_stacking),
    "mca_payoff_signature":         _FlagSpec("Recent MCA payoff", "stacking", "look_closer", _fmt_mca_payoff),
    "paydown_mca_suspected":        _FlagSpec("MCA paydown pattern", "stacking", "material", _fmt_paydown_mca),
    "withdrawal_acceleration":      _FlagSpec("MCA debit acceleration", "stacking", "material", _fmt_withdrawal_acceleration),
    "acceleration_clause_triggered": _FlagSpec("MCA acceleration", "stacking", "decline", _fmt_acceleration_clause),
    "unauthorized_withdrawal_dispute": _FlagSpec("Unauthorized withdrawal dispute", "stacking", "decline", _fmt_unauthorized_withdrawal_dispute),

    # === Revenue fabrication ===========================================
    "wash_deposit_suspected":       _FlagSpec("Suspected wash deposits", "fabrication", "decline", _fmt_wash_deposit),
    "duplicate_deposits_detected":  _FlagSpec("Duplicate deposits", "fabrication", "material", _fmt_duplicate_deposits),
    "synthetic_low_variance":       _FlagSpec("Deposits look synthetic", "fabrication", "material", _fmt_synthetic_low_variance),
    "round_number_deposits":        _FlagSpec("Round-number deposits", "fabrication", "material", _fmt_round_number_deposits),
    "preloan_spike":                _FlagSpec("Pre-loan deposit spike", "fabrication", "material", _fmt_preloan_spike),
    "deposit_velocity_spike":       _FlagSpec("Deposit velocity spike", "fabrication", "material", _fmt_deposit_velocity_spike),

    # === Cashflow stress ===============================================
    "nsf_clustering_short":         _FlagSpec("NSF concentration", "stress", "material", _fmt_nsf_clustering_short),
    "nsf_late_concentration":       _FlagSpec("Late NSF concentration", "stress", "material", _fmt_nsf_late_concentration),
    "payroll_absent":               _FlagSpec("Payroll absent", "stress", "look_closer", _fmt_payroll_absent),

    # === Concentration & fundamentals ==================================
    "customer_concentration":       _FlagSpec("Customer concentration", "concentration", "material", _fmt_customer_concentration),
    "processor_holdback_detected":  _FlagSpec("Processor holdback suspected", "concentration", "material", _fmt_processor_holdback),
    "chargeback_velocity":          _FlagSpec("Chargeback velocity", "concentration", "material", _fmt_chargeback_velocity),

    # === Hidden account ================================================
    "unreconciled_internal_transfer": _FlagSpec("Unreconciled internal transfer", "hidden_account", "material", _fmt_unreconciled_internal_transfer),

    # === Recency / account age =========================================
    "recent_account_opening":       _FlagSpec("New account", "recency", "material", _fmt_recent_account_opening),

    # === PDF tampering (metadata layer) ================================
    "incremental_saves":            _FlagSpec("PDF saved incrementally", "tampering", "decline", _fmt_incremental_saves),
    "editor_detected":              _FlagSpec("PDF editor detected", "tampering", "material", _fmt_editor_detected),
    "personal_author":              _FlagSpec("Personal author on PDF", "tampering", "material", _fmt_personal_author),
    "stripped_metadata":            _FlagSpec("PDF metadata stripped", "tampering", "material", None),
    "page_size_inconsistency":      _FlagSpec("Page size mismatch", "tampering", "decline", _fmt_page_size_inconsistency),
    "xref_offset_mismatch":         _FlagSpec("PDF xref offset mismatch", "tampering", "material", None),
    "font_inconsistency":           _FlagSpec("Font inconsistency", "tampering", "material", _fmt_font_inconsistency),
    "page_layer_anomaly":           _FlagSpec("Page layer anomaly", "tampering", "look_closer", _fmt_page_layer_anomaly),
    # ``modified_*_after_creation`` is matched dynamically below — the
    # minute count is baked into the code rather than the detail.

    # === Aggregate soft signals ========================================
    "top_counterparty_concentration": _FlagSpec("Top customer", "soft", "context", _fmt_top_counterparty_concentration),
    "payroll_cadence":              _FlagSpec("Payroll cadence", "soft", "context", _fmt_payroll_cadence),
    "nsf_on_negative_days":         _FlagSpec("NSFs on negative-balance days", "soft", "context", _fmt_nsf_on_negative_days),
    "adb_partial_coverage":         _FlagSpec("ADB coverage gap", "soft", "context", _fmt_adb_partial_coverage),

    # === Classifier confidence =========================================
    "classification_confidence_below_floor": _FlagSpec(
        "Transaction classifier confidence low", "soft", "look_closer", _fmt_classification_confidence_below_floor,
    ),

    # === Infrastructure (META prefix non-pattern) ======================
    "ocr_fallback_used":            _FlagSpec("OCR fallback used", "soft", "context", None),
    "per_page_routing_used":        _FlagSpec("Per-page routing used", "soft", "context", None),
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
        return HumanFlag(
            code=code,
            title="PDF modified after creation",
            detail=f"{n} {'min' if unit == 'min' else 'h'} after creation",
            category="tampering",
            severity_band="material" if unit == "min" else "decline",
        )

    spec = _FLAG_REGISTRY.get(code)
    if spec is not None:
        detail = spec.formatter(raw_detail) if spec.formatter else raw_detail
        return HumanFlag(
            code=code,
            title=spec.title,
            detail=detail,
            category=spec.category,
            severity_band=spec.severity_band,
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


def humanize_audit_action(action: str, details: dict | None = None) -> str:
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
    "HumanFlag",
    "SeverityBand",
    "humanize_audit_action",
    "humanize_flag",
]
