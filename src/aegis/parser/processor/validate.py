"""Deterministic validation gate for processor statements.

One identity, $0.01 tolerance, no retry on failure (same firewall
discipline as the bank-statement validator):

    sum(gross_charge) - sum(refund) - sum(chargeback) - sum(fee)
        == sum(payout) +/- $0.01

When the identity holds, the document proceeds. When it fails, the
processor pipeline routes the doc to ``manual_review`` with a
``processor_math_failed`` failure code — the operator handles it,
not the LLM. Retrying extraction on a math gap means asking the LLM
to fudge until it ties out, which defeats the gate.

Side-checks (also fail-closed):

- Every line item must have ``source_page`` + ``source_line``. The
  schema's ``ge=1`` constraint guards against zeros, but we re-check
  here so the audit-trail invariant is enforced at the validation
  layer too.
- Period dates: ``period_start <= period_end`` and a reasonable span
  (≤ 90 days). A 9-month "statement" is a misrouted document.
- Printed totals tie out: each of ``gross_volume``, ``refunds_total``,
  ``chargebacks_total``, ``fees_total``, ``payouts_total`` matches
  the sum of its line-item kind +/- $0.01. This catches LLM
  hallucinations where the printed summary is correct but the
  extracted line items don't cover the period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Final

from aegis.parser.processor.models import (
    ExtractedProcessorStatement,
    ProcessorLineItem,
    ProcessorLineKind,
)

# $0.01 reconciliation tolerance — matches the bank-statement validator
# and ledger.io rounding-error assumptions. Anything beyond a penny is
# a real gap, not float noise.
TOLERANCE: Final[Decimal] = Decimal("0.01")

# Hard upper bound on a single statement's covered period. Stripe and
# Square both bill monthly; a 90-day window is the loosest plausible
# real-world shape (quarterly export). Wider than that → misrouted
# document.
MAX_PERIOD_DAYS: Final[int] = 90


@dataclass
class ProcessorValidationResult:
    """Validation gate outcome.

    ``passed`` is True iff ``failures`` is empty. Warnings are surfaced
    on the dossier as informational but don't gate parse status —
    e.g. a 1-cent rounding drift that's within tolerance still emits
    a heads-up warning so the operator notices systematic bias.
    """

    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_processor(
    statement: ExtractedProcessorStatement,
) -> ProcessorValidationResult:
    """Run the processor validation gate.

    Pure function. No IO. No LLM. Idempotent.
    """
    failures: list[str] = []
    warnings: list[str] = []

    # 1) Period sanity.
    period_start = statement.summary.period_start
    period_end = statement.summary.period_end
    if period_end < period_start:
        failures.append(
            f"period_inverted: period_end {period_end} < period_start {period_start}"
        )
    else:
        span_days = (period_end - period_start).days + 1
        if span_days > MAX_PERIOD_DAYS:
            failures.append(
                f"period_too_long: {span_days} days exceeds max {MAX_PERIOD_DAYS}"
            )

    # 2) Source attribution on every row (defense in depth — Pydantic
    # ge=1 should already enforce this, but checking explicitly produces
    # a single sharp error when the audit trail is incomplete).
    for i, row in enumerate(statement.transactions):
        if row.source_page < 1 or row.source_line < 1:
            failures.append(
                f"missing_source_attribution: row[{i}] kind={row.kind} "
                f"page={row.source_page} line={row.source_line}"
            )

    # 3) Per-kind tie-out against the printed summary.
    summed = _sum_by_kind(statement.transactions)
    _check_tie_out(
        "gross_volume",
        statement.summary.gross_volume,
        summed["gross_charge"],
        failures,
        warnings,
    )
    _check_tie_out(
        "refunds_total",
        statement.summary.refunds_total,
        summed["refund"],
        failures,
        warnings,
    )
    _check_tie_out(
        "chargebacks_total",
        statement.summary.chargebacks_total,
        summed["chargeback"],
        failures,
        warnings,
    )
    _check_tie_out(
        "fees_total",
        statement.summary.fees_total,
        summed["fee"],
        failures,
        warnings,
    )
    _check_tie_out(
        "payouts_total",
        statement.summary.payouts_total,
        summed["payout"],
        failures,
        warnings,
    )

    # 4) The identity: gross - refunds - chargebacks - fees == payouts.
    expected_payouts = (
        summed["gross_charge"]
        - summed["refund"]
        - summed["chargeback"]
        - summed["fee"]
    )
    gap = abs(expected_payouts - summed["payout"])
    if gap > TOLERANCE:
        failures.append(
            f"processor_math_failed: gross - refunds - chargebacks - fees = "
            f"{expected_payouts} but payouts = {summed['payout']} (gap {gap})"
        )

    return ProcessorValidationResult(
        passed=not failures,
        failures=failures,
        warnings=warnings,
    )


def _sum_by_kind(
    rows: list[ProcessorLineItem],
) -> dict[ProcessorLineKind, Decimal]:
    """Sum line-item amounts by kind. Missing kinds default to $0.00."""
    totals: dict[ProcessorLineKind, Decimal] = {
        "gross_charge": Decimal("0.00"),
        "refund": Decimal("0.00"),
        "chargeback": Decimal("0.00"),
        "fee": Decimal("0.00"),
        "payout": Decimal("0.00"),
        "adjustment": Decimal("0.00"),
    }
    for r in rows:
        totals[r.kind] += r.amount
    return totals


def _check_tie_out(
    label: str,
    printed: Decimal,
    summed: Decimal,
    failures: list[str],
    warnings: list[str],
) -> None:
    """Compare a printed total against summed line items.

    Beyond tolerance → fail. Inside tolerance but non-zero gap → warn
    so a systematic rounding bias surfaces over multiple statements.
    """
    gap = abs(printed - summed)
    if gap > TOLERANCE:
        failures.append(
            f"reconciliation_failed_{label}: printed={printed} summed={summed} gap={gap}"
        )
    elif gap > Decimal("0"):
        warnings.append(
            f"{label}_drift: printed={printed} summed={summed} gap={gap}"
        )


# timedelta is imported but only used implicitly via date subtraction;
# the explicit import keeps mypy happy if downstream code wants to
# touch the dataclass fields and needs the same type alias.
_ = timedelta


__all__ = [
    "MAX_PERIOD_DAYS",
    "TOLERANCE",
    "ProcessorValidationResult",
    "validate_processor",
]
