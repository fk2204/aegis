"""Build operator-facing pattern cards from a PatternAnalysis.

The parser emits ``Pattern`` dataclasses with a ``code`` (machine name),
``severity`` (0-100 contribution to fraud_score), ``detail`` (compact
one-liner) and ``source_ids`` (UUIDs into the ``transactions`` table).

The dashboard renders a card per pattern with:

* a plain-English title and description (so an underwriter reading
  cold understands what triggered),
* the severity color band,
* the parser's ``detail`` string (the actual values: counts, dollar
  amounts, dates),
* an expandable table of the contributing transactions — so the broker
  can answer "which rows raised this flag" without leaving the page.

``mca_stacking`` is deliberately excluded — it has its own richer card
(``_stacking_card.html.j2``) that breaks down per-position daily
equivalents. Including it twice would double-count visually.

``recent_account_opening`` is also excluded — it's already shown as a
hard-decline reason on the score breakdown panel and has no
transaction-level drill-down to add.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from aegis.parser.models import ClassifiedTransaction
from aegis.parser.patterns import PatternAnalysis


@dataclass(frozen=True)
class PatternCardCopy:
    """Static copy keyed by pattern code. Operator-readable language."""

    title: str
    description: str


# Pattern code -> operator-facing copy. Every code emitted by
# aegis.parser.patterns.analyze_patterns must have an entry here (except
# the two intentionally rendered elsewhere). When a new detector lands
# in patterns.py, add its copy block here in the same commit.
PATTERN_COPY: Final[dict[str, PatternCardCopy]] = {
    "duplicate_deposits_detected": PatternCardCopy(
        title="Duplicate Deposits",
        description=(
            "Same date and exact amount appears in more than one deposit row. "
            "Legitimate when a merchant has truly identical sales across "
            "channels; suspicious when it suggests fabricated rows used to "
            "inflate revenue."
        ),
    ),
    "synthetic_low_variance": PatternCardCopy(
        title="Deposits Look Synthetic",
        description=(
            "Deposit amounts cluster tightly around the mean (coefficient of "
            "variation < 15% across 10+ deposits). Real merchant cash flow "
            "varies; uniformly-sized deposits often indicate fabricated or "
            "ACH-padded statements."
        ),
    ),
    "round_number_deposits": PatternCardCopy(
        title="Round-Number Deposits",
        description=(
            "More than 75% of deposits are exact multiples of $100. Real "
            "merchant revenue rarely lands on clean multiples — this pattern "
            "often signals fabricated activity or rounded-up cash counts."
        ),
    ),
    "preloan_spike": PatternCardCopy(
        title="Pre-Loan Deposit Spike",
        description=(
            "Deposits in the last 7 or 14 days of the statement exceed 2.5x "
            "the prior-period weekly average. Common indicator that a "
            "merchant is padding the account before applying for funding. "
            "Verify against historical statements if available."
        ),
    ),
    "nsf_clustering_short": PatternCardCopy(
        title="NSF Concentration (Short Statement)",
        description=(
            "More than 3 non-sufficient-funds fees in a statement shorter "
            "than 20 days. Short-window NSF clustering is structural cash-"
            "flow stress, not a one-off accident."
        ),
    ),
    "nsf_late_concentration": PatternCardCopy(
        title="NSF Concentration (Late in Statement)",
        description=(
            "At least 3 NSF fees fell in the final 30 days of a longer "
            "statement. Indicates the merchant's cash position is "
            "deteriorating, not improving — a late-period decline is more "
            "worrying than early-period bumps that resolved."
        ),
    ),
    "wash_deposit_suspected": PatternCardCopy(
        title="Wash Deposits Suspected",
        description=(
            "Deposit and withdrawal pairs of near-equal size (within 2%) "
            "appear within 5 days of each other. Pattern of moving money in "
            "and out to inflate apparent deposit volume without real revenue."
        ),
    ),
    "paydown_mca_suspected": PatternCardCopy(
        title="MCA Paydown Pattern",
        description=(
            "Same-payee debits with monotonically descending amounts (≥5 "
            "events, ≤5% noise on the way down, ending ≤85% of the start). "
            "Suggests an existing MCA position is being paid down — often a "
            "renewal-stage merchant. Confirm with the broker."
        ),
    ),
    "deposit_velocity_spike": PatternCardCopy(
        title="Deposit Velocity Spike",
        description=(
            "A 7-day rolling window contains more than 3x the period-average "
            "daily deposit count. Different signal from a dollar-amount "
            "spike — catches merchants stuffing deposit rows to look busier "
            "than they are."
        ),
    ),
    "withdrawal_acceleration": PatternCardCopy(
        title="MCA Debit Acceleration",
        description=(
            "MCA debit count in the last 7 days is more than 1.5x the prior "
            "weekly average. Indicates new positions being stacked late in "
            "the statement — verify whether the merchant disclosed all "
            "existing MCA obligations."
        ),
    ),
    "acceleration_clause_triggered": PatternCardCopy(
        title="MCA Acceleration",
        description=(
            "A recurring MCA position broke and a single debit 5-10x larger "
            "than the prior median posted to the same payee - the funder "
            "called the loan after default. Decline class flag: the merchant "
            "defaulted on a prior funder, outside our risk appetite."
        ),
    ),
    "recent_account_opening": PatternCardCopy(
        title="Recent Account Opening",
        description=(
            "Bank account opened less than 60 days before today. Doesn't "
            "satisfy our 6-month intake baseline on its face. If the "
            "BUSINESS itself is also under 6 months, decline; if it's an "
            "established business that recently switched banks, request "
            "prior bank statements before declining."
        ),
    ),
    "payroll_absent": PatternCardCopy(
        title="Payroll Absent",
        description=(
            "No payroll-processor activity (ADP / Gusto / Paychex / etc.) "
            "across a ≥21 day period with ≥$50k revenue. Real operating "
            "businesses at this scale almost always have payroll — ask how "
            "the merchant pays employees and contractors. 1099-only is "
            "plausible; W-2 claims without a payroll trace is a red flag."
        ),
    ),
    "unauthorized_withdrawal_dispute": PatternCardCopy(
        title="Unauthorized Withdrawal Dispute",
        description=(
            "A credit row with reversal/dispute keywords ('reversal', "
            "'unauthorized', 'ach return credit') pairs with a prior MCA "
            "debit within 14 days at near-equal amount. The merchant "
            "fought (and won) a funder withdrawal — most funders won't "
            "touch a merchant who disputed a prior funder. Near-decline "
            "single-event signal. Ask directly which funder, why the "
            "dispute, and how it resolved."
        ),
    ),
    "unreconciled_internal_transfer": PatternCardCopy(
        title="Unreconciled Internal Transfer",
        description=(
            "Transfer-OUT > $500 with no matching transfer-IN (within "
            "$1, within ±3 days) in the bundle. Money leaving the visible "
            "accounts to an undisclosed account — often hosting an "
            "undisclosed MCA. Severity scales with unmatched count (15 "
            "base, +5 per additional, capped at 40). Request all bank "
            "account statements; merchants hiding accounts often hide MCAs."
        ),
    ),
    "mca_payoff_signature": PatternCardCopy(
        title="MCA Payoff Signature",
        description=(
            "Any single debit > $5,000 whose description contains a known "
            "funder token. Recently-paid-off MCA still counts in renewal-"
            "likelihood scoring even if not currently active. Look-closer "
            "signal, not a decline. Ask the merchant when this funder was "
            "paid off and confirm no balance remains."
        ),
    ),
    "customer_concentration": PatternCardCopy(
        title="Customer Concentration",
        description=(
            "Top single counterparty's share of revenue exceeds 30% "
            "(severity 10), 40% (20), or 60% (30). Single-customer "
            "dependency — lose that customer and the merchant can't "
            "service the advance. Material above 50%; over 70% pauses "
            "the deal regardless of other signals. Ask who the customer "
            "is, contract length, and whether it's renewable."
        ),
    ),
    "chargeback_velocity": PatternCardCopy(
        title="Chargeback / Refund Velocity",
        description=(
            "Debits containing chargeback or refund keywords "
            "('chargeback', 'refund', 'return ach', 'dispute', 'merchant "
            "return', 'credit reversal'). Three paths: short statements "
            "with ≥5 rows; longer statements where last-14d count > 1.5x "
            "the prior fortnight; longer statements with ≥6 total rows. "
            "Leading indicator of B2C distress and dispute risk on the "
            "funder's holdback. Cross-check against revenue scale — 1% "
            "is normal, > 3% is alarming."
        ),
    ),
    "processor_holdback_detected": PatternCardCopy(
        title="Processor Holdback Detected",
        description=(
            "≥10 deposits from a known card processor (Stripe / Square / "
            "Toast / etc.) over a ≥14 day period, with the daily-summed "
            "coefficient of variation ≥ 0.50. Variable processor payouts "
            "strongly imply an in-place MCA holdback — a funder taking a "
            "cut before payout reaches the bank. Ask directly whether "
            "any card processor payout is split or held by a funder, "
            "and cross-check against the MCA stacking count."
        ),
    ),
}


def _severity_band(severity: int) -> str:
    """Map a Pattern.severity to a CSS color band.

    Severities in aegis.parser.patterns currently range 15-50; bands
    chosen so the lowest-severity patterns (round_number_deposits=15)
    stay in 'warn' while the heaviest (wash_deposit_suspected=35,
    duplicate_deposits=30) reach 'neg'.
    """
    if severity >= 30:
        return "neg"
    if severity >= 15:
        return "warn"
    return "pos"


# Patterns rendered elsewhere — skip them in the card list.
# ``mca_stacking`` has its own richer card driven by ``StackingCard``.
# ``recent_account_opening`` used to be excluded because there was nothing
# to drill into; with the explanation panel landing in chunk 2 of the
# evidence drill-down work, it now renders as a pattern card so the
# operator playbook sits next to the flag instead of hiding inside the
# score-breakdown panel only.
_RENDERED_ELSEWHERE: Final[frozenset[str]] = frozenset({
    "mca_stacking",
})


@dataclass(frozen=True)
class PatternCard:
    code: str
    title: str
    description: str
    detail: str
    severity: int
    severity_band: str
    source_transactions: list[ClassifiedTransaction]


def build_pattern_cards(
    pattern_analysis: PatternAnalysis | None,
    transactions: list[ClassifiedTransaction],
) -> list[PatternCard]:
    """Return one card per surfaced pattern, ordered by severity descending.

    Patterns lacking copy in PATTERN_COPY are skipped silently (so a
    parser-side new detector doesn't crash the dashboard) — but `make
    check` should catch the omission via the regression test that
    asserts every code in PATTERN_COPY matches an emitted Pattern.
    """
    if pattern_analysis is None:
        return []
    by_id: dict[UUID, ClassifiedTransaction] = {t.id: t for t in transactions}
    cards: list[PatternCard] = []
    for p in pattern_analysis.patterns:
        if p.code in _RENDERED_ELSEWHERE:
            continue
        copy = PATTERN_COPY.get(p.code)
        if copy is None:
            continue
        src = [by_id[i] for i in p.source_ids if i in by_id]
        cards.append(
            PatternCard(
                code=p.code,
                title=copy.title,
                description=copy.description,
                detail=p.detail,
                severity=p.severity,
                severity_band=_severity_band(p.severity),
                source_transactions=src,
            )
        )
    cards.sort(key=lambda c: c.severity, reverse=True)
    return cards


__all__ = ["PATTERN_COPY", "PatternCard", "build_pattern_cards"]


def _emitted_pattern_codes_from_source() -> frozenset[str]:
    """AST-walk ``aegis.parser.patterns`` for every ``Pattern(code=...)``
    literal. Used by the regression test that prevents the silent-drop
    bug (a detector emits a code that ``build_pattern_cards`` doesn't
    recognize and quietly skips, hiding the card from the dossier).

    Exposed as a module-level helper so the test stays one-liner
    simple. Returns the union of every ``code=`` keyword argument
    passed to a ``Pattern(...)`` constructor in patterns.py.
    """
    import ast
    from pathlib import Path

    source_path = (
        Path(__file__).resolve().parent.parent / "parser" / "patterns.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "Pattern"):
            continue
        for kw in node.keywords:
            if kw.arg != "code":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(
                kw.value.value, str
            ):
                codes.add(kw.value.value)
    return frozenset(codes)
