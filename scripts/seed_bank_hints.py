"""One-time seeding of operator-curated extraction hints into
``bank_layouts.extraction_hints`` for known banks.

Use case: the 2026-06-16 recovery pass surfaced a clear pattern in
Bedrock's partial-extraction failures — the LLM occasionally drops the
statement period (period_start / period_end null), particularly when
the period block sits in a non-standard header location. Per-bank hint
text (operator-authored, persisted on ``bank_layouts.extraction_hints``)
is the existing layout-learning surface designed to feed exactly this
information back into the extraction prompt; once a bank crosses
``HINTS_AVAILABLE_THRESHOLD`` successful parses (3 today), the prompt
pipeline automatically appends the hint to the Bedrock system prompt
on every parse for that bank.

The initial 2026-06-16 seed (commit ``33825d0``) covered JPMorgan Chase
and TD Bank but used templated placeholder text ("Statement period
formatted MM/DD/YY to MM/DD/YY", "Running balance column labeled
'Balance'") that did NOT match the real layouts of TD Convenience
Checking or Chase Business Complete Checking. The recovery pass on
LOAD LIFT ENTERPRISE LLC (4 TD statements) and TMF TRANSPORT INC (4
Chase statements) confirmed this — those merchants' statements landed
in ``manual_review`` despite the hint being present. This revision
replaces both hints with descriptions derived from the first-page text
of the actual failing statements: full English month name + 'through'
separator for Chase, three-letter month abbreviation + tight-hyphen
range for TD, and an explicit note that NEITHER product has a per-line
running-balance column (the original hint claimed both did). No
migration, no schema change — ``bank_layouts`` is a regular Postgres
table, ``set_hints`` is an existing idempotent repository method.

Per CLAUDE.md operating-principles §1 the script is DRY-RUN by default.
Run on the box once after merge::

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    .venv/bin/python scripts/seed_bank_hints.py            # dry-run preview
    .venv/bin/python scripts/seed_bank_hints.py --apply    # actually write

Re-runnable: ``set_hints`` upserts on bank_name, so re-seeding with the
same hint text is a no-op. Edit the ``_BANK_HINTS`` dict below and
re-run when the operator authors more bank-specific guidance.

Exit codes (mirror ``scripts/track_a_historical_lookback.py``):

  * ``0`` — every bank's hint written cleanly (or, in dry-run mode,
            previewed without error).
  * ``1`` — runtime error (Supabase init failed, settings missing).
  * ``3`` — at least one bank's write raised. Operator triage required.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from aegis.audit import AuditLog, SupabaseAuditLog
from aegis.bank_layouts.repository import (
    BankLayoutWriteError,
    SupabaseBankLayoutRepository,
)

# Exit codes — keep aligned with sibling scripts.
EXIT_OK: Final[int] = 0
EXIT_RUNTIME_ERROR: Final[int] = 1
EXIT_ISSUES_FOUND: Final[int] = 3

# Actor stamp used on audit rows.
_ACTOR: Final[str] = "seed_bank_hints_script"


# Bank-name → operator-authored hint text. Bank names MUST match the
# ``StatementSummary.bank_name`` Bedrock extracts on a successful parse
# (the same names that appear in ``bank_layouts.bank_name`` after the
# auto-grow logic in ``parser/pipeline.py`` lines 392-405 fires). A
# mismatch here means the hint sits in a separate row that never gets
# matched on lookup.
#
# Hints are appended verbatim by ``_build_extraction_prompt_suffix`` in
# the parser pipeline; the pipeline frames each hint with the prefix
# "Layout hints from prior successful parses of this bank:" so write
# the hint text as a continuation of that frame (no own heading).
_BANK_HINTS: Final[dict[str, str]] = {
    "JPMorgan Chase Bank, N.A.": (
        "Statement period is in the upper area of page 1 formatted "
        "'Month DD, YYYY through Month DD, YYYY' (full English month "
        "name, the literal word 'through' as the separator, four-digit "
        "year) — e.g. 'January 31, 2026 through February 27, 2026'. "
        "The Chase Business Complete Checking layout does NOT include a "
        "per-line running-balance column; the CHECKING SUMMARY block at "
        "the top of page 1 carries Beginning Balance, Ending Balance, "
        "and totals by category (Deposits and Additions, Checks Paid, "
        "ATM & Debit Card Withdrawals, Electronic Withdrawals, Other "
        "Withdrawals, Fees). Transactions follow in named sections "
        "(DEPOSITS AND ADDITIONS, CHECKS PAID, ATM & DEBIT CARD "
        "WITHDRAWALS, ELECTRONIC WITHDRAWALS) each terminated by a "
        "'Total ...' line. Per-transaction dates are MM/DD only — the "
        "year is implicit from the statement period. The PDF embeds "
        "section-delimiter artifacts like '*start*deposits and "
        "additions' / '*end*deposits and additions' that are NOT "
        "transactions and MUST be ignored. Bank-identifier banner reads "
        "'JPMorgan Chase Bank, N.A.' with a customer-service PO Box "
        "address near the top of page 1."
    ),
    "TD Bank, N.A.": (
        "Statement period is labeled 'Statement Period:' in the upper "
        "area of page 1 formatted 'MMM DD YYYY-MMM DD YYYY' (three-"
        "letter month abbreviation, four-digit year, single hyphen with "
        "NO surrounding spaces) — e.g. 'Jan 09 2026-Feb 08 2026'. The "
        "TD Convenience Checking layout does NOT include a per-line "
        "running-balance column; the ACCOUNT SUMMARY block at the top "
        "of page 1 carries Beginning Balance, Ending Balance, and "
        "category totals (Electronic Deposits, Electronic Payments, "
        "Other Withdrawals, Service Charges, Average Collected Balance, "
        "Days in Period). DAILY ACCOUNT ACTIVITY splits transactions "
        "into named subsections (Electronic Deposits, Electronic "
        "Payments, Other Withdrawals, Service Charges, Checks Paid), "
        "each terminated by its own 'Subtotal:' line. Per-transaction "
        "dates are MM/DD only — the year is implicit from the statement "
        "period. Bank-identifier line at the bottom of page 1 reads "
        "'Bank Deposits FDIC Insured | TD Bank, N.A. | Equal Housing "
        "Lender'."
    ),
}


# ─────────────────────────────────────────────────────────────────────
# Per-bank outcome shape
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SeedOutcome:
    """One bank's seed result, ready for stdout / exit-code accounting."""

    bank_name: str
    hint_excerpt: str  # first 80 chars of the hint for the summary print
    action: str  # "would_set" | "set" | "error"
    detail: str

    @property
    def is_issue(self) -> bool:
        return self.action == "error"


def _excerpt(hint: str, length: int = 80) -> str:
    flat = " ".join(hint.split())
    if len(flat) <= length:
        return flat
    return flat[: length - 1] + "…"


def _seed_one(
    *,
    bank_name: str,
    hint: str,
    repo: SupabaseBankLayoutRepository,
    audit: AuditLog,
    apply_writes: bool,
) -> SeedOutcome:
    """Seed one bank's hint. Dry-run reports what would be set."""
    excerpt = _excerpt(hint)
    if not apply_writes:
        return SeedOutcome(
            bank_name=bank_name,
            hint_excerpt=excerpt,
            action="would_set",
            detail=f"DRY-RUN — would call set_hints({bank_name!r}, …)",
        )

    try:
        row = repo.set_hints(bank_name=bank_name, hints=hint)
    except BankLayoutWriteError as exc:
        return SeedOutcome(
            bank_name=bank_name,
            hint_excerpt=excerpt,
            action="error",
            detail=f"set_hints failed: {type(exc).__name__}: {exc}",
        )

    # Audit row — gives the operator a durable record of WHEN the hints
    # landed and WHAT was set, distinct from ``set_hints`` running
    # again later via the operator UI.
    audit.record(
        actor=_ACTOR,
        action="bank_layouts.hints_seeded",
        subject_type="bank_layout",
        subject_id=row.id,
        details={
            "bank_name": bank_name,
            "hint_length": len(hint),
            "hint_excerpt": excerpt,
            "successful_parses_at_seed": row.successful_parses,
        },
    )
    return SeedOutcome(
        bank_name=bank_name,
        hint_excerpt=excerpt,
        action="set",
        detail=(f"hints written; row id={row.id} successful_parses={row.successful_parses}"),
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Seed operator-authored extraction_hints into bank_layouts for "
            "known banks. DRY-RUN by default; pass --apply to write."
        )
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Actually call SupabaseBankLayoutRepository.set_hints per bank "
            "and write a bank_layouts.hints_seeded audit row. Default is "
            "dry-run (print the bank + hint excerpt that WOULD be seeded)."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        repo = SupabaseBankLayoutRepository()
        audit: AuditLog = SupabaseAuditLog()
    except Exception as exc:
        print(
            f"ERROR: could not initialise dependencies: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    outcomes: list[SeedOutcome] = []
    for bank_name, hint in _BANK_HINTS.items():
        outcomes.append(
            _seed_one(
                bank_name=bank_name,
                hint=hint,
                repo=repo,
                audit=audit,
                apply_writes=args.apply,
            )
        )

    # Stdout report — one human-readable line per bank.
    for o in outcomes:
        print(
            f"  {o.bank_name:40s}  action={o.action:10s}  {o.detail}",
        )

    issues = sum(1 for o in outcomes if o.is_issue)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"# mode={mode} banks={len(outcomes)} issues={issues}",
        file=sys.stderr,
    )
    return EXIT_ISSUES_FOUND if issues > 0 else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())


# Mark unused imports as kept-for-future-use without ruff noise:
_ = UUID
