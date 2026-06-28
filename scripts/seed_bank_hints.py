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
from typing import Any, Final, cast
from uuid import UUID

from aegis.audit import AuditLog, SupabaseAuditLog
from aegis.bank_layouts.auto_hints import (
    generate_hints_from_parse_result,
    merge_hints,
)
from aegis.bank_layouts.repository import (
    HINTS_AVAILABLE_THRESHOLD,
    BankLayoutWriteError,
    SupabaseBankLayoutRepository,
)
from aegis.db import get_supabase

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
    "Bank of America, N.A.": (
        "Statement period appears on page 1 formatted 'for Month D, "
        "YYYY to Month D, YYYY' (full English month name, day, four-"
        "digit year, the literal word 'to' as separator) — e.g. 'for "
        "April 1, 2026 to April 30, 2026'. The account number appears "
        "on page 1 next to the period line. Account summary block is "
        "labeled 'Account summary' and carries Beginning balance, "
        "Deposits and other credits, Withdrawals and other debits, "
        "Checks, Service fees, Ending balance. Transactions split into "
        "two named sections — 'Deposits and other credits' and "
        "'Withdrawals and other debits' — with columns Date, "
        "Description, Amount. The Bank of America layout does NOT "
        "include a per-line running-balance column; daily ledger "
        "balances appear instead on the last page in a 3-column grid. "
        "Service fees (overdraft / NSF) are listed in their own "
        "separate table, not interleaved with the transaction sections."
    ),
    "Third Coast Bank": (
        "Statement period appears in the top-right area of page 1 as "
        "two separate lines: 'Last statement: Month D, YYYY' and 'This "
        "statement: Month D, YYYY' (full English month name, day, four-"
        "digit year on each line). Summary block is labeled 'Select "
        "Free Checking Business' and carries Beginning balance, Total "
        "additions, Total subtractions, Ending balance, Average "
        "balance, Avg collected balance. Transactions split across "
        "three named sections in this order: 'CHECKS' (checks paid), "
        "'DEBITS' (columns Date, Description, Subtractions), and "
        "'CREDITS' (columns Date, Description, Additions). Each DEBITS "
        "and CREDITS row occupies TWO lines — transaction type on the "
        "first line, detail on the second — and each row is prefixed "
        "with a single tick mark (the apostrophe character) before the "
        "date. The Third Coast layout does NOT include a per-line "
        "running-balance column; daily balances appear in a separate "
        "'DAILY BALANCES' table at the end of the statement. Deposit-"
        "type descriptions seen on this layout include 'Rtp Credit', "
        "'ACH Credit', and plain 'Deposit'."
    ),
    # 'Bank of America' (short form) is the same institution as 'Bank of
    # America, N.A.' above — Bedrock occasionally emits the short string
    # depending on which page the bank-name token is parsed from. Hint
    # text is duplicated verbatim so the bank_layouts lookup hits a real
    # row regardless of which variant comes out of extraction; full
    # canonicalization belongs in the parser, separate scope.
    "Bank of America": (
        "Statement period appears on page 1 formatted 'for Month D, "
        "YYYY to Month D, YYYY' (full English month name, day, four-"
        "digit year, the literal word 'to' as separator) — e.g. 'for "
        "April 1, 2026 to April 30, 2026'. The account number appears "
        "on page 1 next to the period line. Account summary block is "
        "labeled 'Account summary' and carries Beginning balance, "
        "Deposits and other credits, Withdrawals and other debits, "
        "Checks, Service fees, Ending balance. Transactions split into "
        "two named sections — 'Deposits and other credits' and "
        "'Withdrawals and other debits' — with columns Date, "
        "Description, Amount. The Bank of America layout does NOT "
        "include a per-line running-balance column; daily ledger "
        "balances appear instead on the last page in a 3-column grid. "
        "Service fees (overdraft / NSF) are listed in their own "
        "separate table, not interleaved with the transaction sections."
    ),
    "First National Bank of Bosque County": (
        "Statement period is implicit from the 'Account Summary' table "
        "near the top of page 1 — Beginning Balance row carries the "
        "period-start date (MM/DD/YYYY, four-digit year) and Ending "
        "Balance row carries the period-end date in the same format. "
        "Bank-identifier banner reads 'First National Bank of Bosque "
        "County, PO Box 278, Valley Mills, TX 76689' on page 1 and "
        "again in the error-reporting footer block. The 'Summary of "
        "Accounts' table at the top lists each account by type "
        "('Business Checking'), account number masked as XXXXX####, "
        "and ending balance. Per-account Account Summary block carries "
        "Beginning Balance, '# Credit(s) This Period' with sum, '# "
        "Debit(s) This Period' with sum, Ending Balance, and Service "
        "Charges as a separate negative line. The Account Activity "
        "section DOES include a per-line running-balance column with "
        "columns Post Date, Description, Debits, Credits, Balance — "
        "use the Balance column to cross-check transaction-by-"
        "transaction running total. Per-transaction dates are full "
        "MM/DD/YYYY. ACH descriptions are two-line ('External Deposit "
        "INTUIT 93553463 - DEPOSIT' first line, trace ID second line) "
        "and must be merged client-side."
    ),
    "Flagstar": (
        "Statement period is implicit from the 'Account Summary' table "
        "near the top of page 1 — Beginning Balance row carries the "
        "period-start date (MM/DD/YYYY, four-digit year) and Ending "
        "Balance row carries the period-end date in the same format. "
        "Bank-identifier banner reads the branch and mailing address "
        "(e.g. 'Branch Name HICKSVILLE', '102 Duffy Avenue, "
        "Hicksville, NY 11801') with 'www.flagstar.com' next to the "
        "customer-service line at the top of page 1. The 'Summary of "
        "Accounts' table lists account type ('Standard Business "
        "Checking'), account number masked as XXXXXX####, and ending "
        "balance. Per-account Account Summary block carries Beginning "
        "Balance, '# Credit(s) This Period' with sum, '# Debit(s) "
        "This Period' with sum, and Ending Balance. The Flagstar "
        "layout does NOT include a per-line running-balance column; "
        "transactions split into named sections in this order: "
        "'Deposits', 'Other Credits', 'Electronic Debits', 'Other "
        "Debits', 'Checks Cleared', each ending with a 'N item(s) "
        "totaling $X' tally. Within sections columns are Date, "
        "Description, Amount only. Per-transaction dates are full "
        "MM/DD/YYYY. The header also surfaces an Interest Summary "
        "block (APY, Interest Days, Interest Earned / Paid) and a "
        "Credit(s)/Debits(s) This Period sub-breakdown by channel "
        "(Deposits, ACH Credits, Lockbox, Incoming Funds Transfer, "
        "Checks, ACH Debits, Outgoing Funds Transfer, etc.) — those "
        "are summaries, not the actual transactions."
    ),
    "Middlesex Federal Savings, F.A.": (
        "This is a Novo Platform fintech statement layered on top of "
        "Middlesex Federal Savings, F.A. as the underlying chartered "
        "bank — the layout is Novo's, not a traditional Middlesex "
        "Federal layout. The Novo footer 'Novo Platform Inc. (844) "
        "260 - 6800' appears at the bottom of every page; the bank-"
        "identifier banner near the top of page 1 reads 'Deposit "
        "account services provided by: Middlesex Federal Savings, "
        "F.A., Member FDIC, 1 College Avenue, Somerville, MA 02144'. "
        "Statement Date is labeled 'Statement Date' and formatted "
        "'MMM DD, YYYY thru MMM DD, YYYY' (three-letter month "
        "abbreviation, comma after day, four-digit year, the literal "
        "word 'thru' as the separator) — e.g. 'Feb 01, 2026 thru "
        "Feb 28, 2026'. The summary block carries Starting Balance, "
        "Income, Expenses, Ending Balance (in that order, four labels "
        "only). Transactions are presented as a single flat table — "
        "no named subsections — with columns Date, Description, Type "
        "(e.g. 'EFT Credit', 'POS Withdrawal', 'ATM Withdrawal', "
        "'External Deposit', 'External Withdrawal'), Amount (signed; "
        "withdrawals are negative). The Novo layout does NOT include "
        "a per-line running-balance column. Per-transaction dates are "
        "'MMM DD' (three-letter month + day, no year) — year is "
        "implicit from the Statement Date period. Account Number is "
        "masked 'XXXX ####'."
    ),
    "VyStar Credit Union": (
        "Statement period is labeled 'Statement Period:' on page 1 "
        "formatted 'MM/DD/YYYY - MM/DD/YYYY' (two-digit month and "
        "day, four-digit year, single hyphen with surrounding "
        "spaces) — e.g. '02/06/2026 - 03/05/2026'. A separate "
        "'Statement Date' line carries the period-end date in the "
        "same MM/DD/YYYY format, and a 'Member Number' line follows. "
        "Bank-identifier banner is the customer-service phone block "
        "near the top of page 1 — 'For assistance, call our Contact "
        "Center at 904-777-6000 or 800-445-6289' and the "
        "'vystarcu.org' / 'VyChat' references; payment address is "
        "'PO Box 45085, Jacksonville, FL 32232'. The 'Summary of "
        "Accounts' table lists each account by type ('Sm Business "
        "Checking Acct', 'Business Savings Account'), account number "
        "masked XXXXXXXX####, and balance. Per-account 'Balance "
        "Summary' / 'Account Summary' block carries Beginning Balance "
        "as of MM/DD/YY, '+ Deposits and Credits (N)' with sum, "
        "'- Withdrawals and Debits (N)' with sum, Ending Balance as "
        "of MM/DD/YY, plus interest fields. The 'Transactional "
        "Detail' section DOES include a per-line running-balance "
        "column with columns Date, Description, Deposits, "
        "Withdrawals, Balance — use Balance to cross-check the "
        "running total. Per-transaction dates are MM/DD only — year "
        "is implicit from the statement period. POS descriptions are "
        "multi-line (purchase line, then 'Seq# ... Date ... Time ...' "
        "metadata line) and must be merged client-side."
    ),
    # ────────────────────────────────────────────────────────────────
    # 2026-06-28 seed pass — 14 major US banks added with bank-
    # identifier-only hints. These hints contain ONLY operator-verified
    # facts (header keyword strings + ABA routing number) and DELIBERATELY
    # do NOT describe statement layout (column headers, period format,
    # summary block structure, transaction sections). Layout-specific
    # fields will be added on a per-bank basis once we have real
    # successful parses to derive from — per CLAUDE.md operating-
    # principles §4 funder-seeding sub-rule, inventing industry-typical
    # layout defaults that look right but match nothing is worse than
    # leaving them out. Activation is gated by HINTS_AVAILABLE_THRESHOLD
    # (3 successful parses for source='manual') so these primed rows
    # have no immediate effect on parse routing until 3 organic parses
    # land per bank.
    # ────────────────────────────────────────────────────────────────
    "Wells Fargo Bank, N.A.": (
        "Bank-identifier banner is 'Wells Fargo Bank, N.A.' with the "
        "'wellsfargo.com' domain near the top of page 1; the header may "
        "also appear in all-caps as 'WELLS FARGO BANK'. ABA routing "
        "number is 121042882. Statement-layout specifics (period "
        "format, summary block labels, transaction section headers, "
        "running-balance column presence) are NOT yet authored — wait "
        "for a real successful parse to derive them rather than "
        "inventing industry-typical defaults."
    ),
    "Capital One, N.A.": (
        "Bank-identifier banner is 'Capital One' with the 'capitalone.com' "
        "domain near the top of page 1; the header may also appear in "
        "all-caps as 'CAPITAL ONE'. ABA routing number is 051405515. "
        "Statement-layout specifics (period format, summary block labels, "
        "transaction section headers, running-balance column presence) "
        "are NOT yet authored — wait for a real successful parse to "
        "derive them rather than inventing industry-typical defaults."
    ),
    "U.S. Bank National Association": (
        "Bank-identifier banner is 'U.S. Bank' with the 'usbank.com' "
        "domain near the top of page 1; the header may also appear in "
        "all-caps as 'US BANK'. ABA routing number is 091000022. "
        "Statement-layout specifics (period format, summary block labels, "
        "transaction section headers, running-balance column presence) "
        "are NOT yet authored — wait for a real successful parse to "
        "derive them rather than inventing industry-typical defaults."
    ),
    "Citibank, N.A.": (
        "Bank-identifier banner is 'Citibank' with the 'citi.com' "
        "domain near the top of page 1; the header may also appear in "
        "all-caps as 'CITIBANK'. ABA routing number is 021000089. "
        "Statement-layout specifics (period format, summary block labels, "
        "transaction section headers, running-balance column presence) "
        "are NOT yet authored — wait for a real successful parse to "
        "derive them rather than inventing industry-typical defaults."
    ),
    "Truist Bank": (
        "Bank-identifier banner is 'Truist' with the 'truist.com' "
        "domain near the top of page 1; legacy statements from the "
        "pre-merger predecessors may still surface 'BB&T' or 'SunTrust' "
        "in headers or footers. ABA routing number is 053101121. "
        "Statement-layout specifics (period format, summary block labels, "
        "transaction section headers, running-balance column presence) "
        "are NOT yet authored — wait for a real successful parse to "
        "derive them rather than inventing industry-typical defaults."
    ),
    "Regions Bank": (
        "Bank-identifier banner is 'Regions' with the 'regions.com' "
        "domain near the top of page 1; the header may also appear in "
        "all-caps as 'REGIONS BANK'. ABA routing number is 062000019. "
        "Statement-layout specifics (period format, summary block labels, "
        "transaction section headers, running-balance column presence) "
        "are NOT yet authored — wait for a real successful parse to "
        "derive them rather than inventing industry-typical defaults."
    ),
    "Fifth Third Bank, N.A.": (
        "Bank-identifier banner is 'Fifth Third' with the '53.com' "
        "domain near the top of page 1; the header may also appear in "
        "all-caps as 'FIFTH THIRD'. ABA routing number is 042000314. "
        "Statement-layout specifics (period format, summary block labels, "
        "transaction section headers, running-balance column presence) "
        "are NOT yet authored — wait for a real successful parse to "
        "derive them rather than inventing industry-typical defaults."
    ),
    "The Huntington National Bank": (
        "Bank-identifier banner is 'Huntington' with the 'huntington.com' "
        "domain near the top of page 1; the header may also appear in "
        "all-caps as 'HUNTINGTON'. ABA routing number is 044000024. "
        "Statement-layout specifics (period format, summary block labels, "
        "transaction section headers, running-balance column presence) "
        "are NOT yet authored — wait for a real successful parse to "
        "derive them rather than inventing industry-typical defaults."
    ),
    "BMO Bank N.A.": (
        "Bank-identifier banner is 'BMO' or 'BMO Bank' with the 'bmo.com' "
        "domain near the top of page 1; legacy statements from the "
        "pre-rebrand period may still surface 'BMO HARRIS'. ABA routing "
        "number is 071000288. Statement-layout specifics (period format, "
        "summary block labels, transaction section headers, running-"
        "balance column presence) are NOT yet authored — wait for a real "
        "successful parse to derive them rather than inventing industry-"
        "typical defaults."
    ),
    "Citizens Bank, N.A.": (
        "Bank-identifier banner is 'Citizens' with the 'citizensbank.com' "
        "domain near the top of page 1; the header may also appear in "
        "all-caps as 'CITIZENS BANK'. ABA routing number is 011500010. "
        "Statement-layout specifics (period format, summary block labels, "
        "transaction section headers, running-balance column presence) "
        "are NOT yet authored — wait for a real successful parse to "
        "derive them rather than inventing industry-typical defaults."
    ),
    "KeyBank National Association": (
        "Bank-identifier banner is 'KeyBank' with the 'key.com' "
        "domain near the top of page 1; the header may also appear in "
        "all-caps as 'KEYBANK'. ABA routing number is 041001039. "
        "Statement-layout specifics (period format, summary block labels, "
        "transaction section headers, running-balance column presence) "
        "are NOT yet authored — wait for a real successful parse to "
        "derive them rather than inventing industry-typical defaults."
    ),
    "Manufacturers and Traders Trust Company": (
        "Bank-identifier banner is 'M&T Bank' with the 'mtb.com' "
        "domain near the top of page 1; the header may also appear in "
        "all-caps as 'M&T BANK'. The legal name on the chartered entity "
        "is 'Manufacturers and Traders Trust Company'. ABA routing "
        "number is 022000046. Statement-layout specifics (period format, "
        "summary block labels, transaction section headers, running-"
        "balance column presence) are NOT yet authored — wait for a real "
        "successful parse to derive them rather than inventing industry-"
        "typical defaults."
    ),
    "Santander Bank, N.A.": (
        "Bank-identifier banner is 'Santander' with the "
        "'santanderbank.com' domain near the top of page 1; the header "
        "may also appear in all-caps as 'SANTANDER'. ABA routing number "
        "is 011075150. Statement-layout specifics (period format, "
        "summary block labels, transaction section headers, running-"
        "balance column presence) are NOT yet authored — wait for a real "
        "successful parse to derive them rather than inventing industry-"
        "typical defaults."
    ),
    "TD Bank": (
        "Short-form bank identifier; the chartered entity name 'TD Bank, "
        "N.A.' is already covered by a separate row in this seed dict "
        "with full layout authoring. Bank-identifier banner is 'TD Bank' "
        "with the 'tdbank.com' domain near the top of page 1; the header "
        "may also appear in all-caps as 'TD BANK'. ABA routing number "
        "for TD Bank, N.A. (US chartered entity) is 031101266. "
        "Statement-layout specifics for the short-form variant follow "
        "the same TD Convenience Checking layout described on the "
        "'TD Bank, N.A.' row — see that row for full structural hints. "
        "Full canonicalization between 'TD Bank' and 'TD Bank, N.A.' "
        "belongs in the parser, separate scope."
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


def _bump_parse_count_one(
    *,
    bank_name: str,
    target_count: int,
    repo: SupabaseBankLayoutRepository,
    audit: AuditLog,
    apply_writes: bool,
) -> SeedOutcome:
    """Bump ``successful_parses`` to at least ``target_count`` (GREATEST).

    Operator-authorized backfill so a bank with a sparse parse history
    crosses ``HINTS_AVAILABLE_THRESHOLD`` and starts using its authored
    extraction_hints on the next parse. Never lowers the count — the
    write is a no-op if the existing value already meets / exceeds the
    target.
    """
    excerpt = f"target={target_count}"
    existing = repo.find_by_bank_name(bank_name)
    if existing is None:
        return SeedOutcome(
            bank_name=bank_name,
            hint_excerpt=excerpt,
            action="error",
            detail=(
                f"no bank_layouts row for {bank_name!r}; create one via the "
                "hints-seeding path or via a real successful parse first."
            ),
        )

    if existing.successful_parses >= target_count:
        return SeedOutcome(
            bank_name=bank_name,
            hint_excerpt=excerpt,
            action="set",
            detail=(
                f"no change — successful_parses={existing.successful_parses} "
                f"already ≥ target={target_count} (GREATEST is a no-op)"
            ),
        )

    print(
        f"Bumping successful_parses for {bank_name} to {target_count} "
        f"(operator-authorized backfill — not a real parse count)"
    )

    if not apply_writes:
        return SeedOutcome(
            bank_name=bank_name,
            hint_excerpt=excerpt,
            action="would_set",
            detail=(
                f"DRY-RUN — would set successful_parses to {target_count} "
                f"on row id={existing.id} "
                f"(current={existing.successful_parses})"
            ),
        )

    try:
        result = (
            get_supabase()
            .table("bank_layouts")
            .update({"successful_parses": target_count})
            .eq("id", str(existing.id))
            .execute()
        )
    except Exception as exc:  # pragma: no cover — runtime/network only
        return SeedOutcome(
            bank_name=bank_name,
            hint_excerpt=excerpt,
            action="error",
            detail=f"UPDATE failed: {type(exc).__name__}: {exc}",
        )

    updated_rows = cast(list[dict[str, Any]], result.data or [])
    if not updated_rows:
        return SeedOutcome(
            bank_name=bank_name,
            hint_excerpt=excerpt,
            action="error",
            detail=f"UPDATE returned no row for id={existing.id}",
        )

    new_count = int(updated_rows[0].get("successful_parses") or 0)
    audit.record(
        actor=_ACTOR,
        action="bank_layouts.successful_parses_bumped",
        subject_type="bank_layout",
        subject_id=existing.id,
        details={
            "bank_name": bank_name,
            "previous_successful_parses": existing.successful_parses,
            "new_successful_parses": new_count,
            "target_count": target_count,
            "hints_available_threshold": HINTS_AVAILABLE_THRESHOLD,
            "note": "operator-authorized backfill — not a real parse count",
        },
    )
    return SeedOutcome(
        bank_name=bank_name,
        hint_excerpt=excerpt,
        action="set",
        detail=(
            f"bumped successful_parses {existing.successful_parses} → "
            f"{new_count} (row id={existing.id})"
        ),
    )


def _generate_from_existing(
    *,
    repo: SupabaseBankLayoutRepository,
    audit: AuditLog,
    apply_writes: bool,
    batch_size: int = 5,
    sleep_between_batches_s: float = 1.0,
) -> list[SeedOutcome]:
    """Retroactive auto-hint generation across the proceed-doc corpus.

    Walks every doc at ``parse_status='proceed'`` with a non-null
    ``storage_path`` (sealed pdf_store blob present), decrypts page 1
    via ``SupabasePdfStoreRepository.fetch_plaintext``, calls
    ``generate_hints_from_parse_result``, and writes auto hints (merged
    with any existing hints) for the doc's bank.

    Doc-side caveats:
      * Multi-doc-per-bank: the first doc seen per bank yields the
        initial hint. Subsequent docs MERGE — ``merge_hints``
        deduplicates so repeated observations don't bloat the row.
      * ``parse_result`` shim — the script doesn't re-run the pipeline
        on each doc (too expensive), so the ``running_balance`` signal
        is inferred from the analyses table's per-doc transaction count
        instead. When that signal isn't available the auto-hint just
        omits the running-balance sentence.
      * Banks without any proceed doc never appear here. That's
        intentional — a bank with only failed parses has no
        confirmed-correct extraction to derive hints from.

    Returns one SeedOutcome per bank (not per doc) so the operator-
    facing summary is bank-level. Batching paces the pdf_store fetch
    path; we throttle by docs-processed not banks.
    """
    import asyncio

    import pymupdf

    from aegis.pdf_store import SupabasePdfStoreRepository

    pdf_store = SupabasePdfStoreRepository()

    # Pull docs in id order so the batching is deterministic; LIMIT
    # high enough to cover the current prod corpus (≤500 docs).
    rows_resp = (
        get_supabase()
        .table("documents")
        .select("id,merchant_id,original_filename,storage_path,parse_status")
        .eq("parse_status", "proceed")
        .not_.is_("storage_path", "null")
        .order("id")
        .execute()
    )
    docs = cast(list[dict[str, Any]], rows_resp.data or [])
    if not docs:
        return [
            SeedOutcome(
                bank_name="(no proceed docs found)",
                hint_excerpt="",
                action="set",
                detail="DRY-RUN — no candidates to process",
            )
        ]

    # Bank-name lookup is via the analyses table since documents has no
    # bank_name column (confirmed on prod 2026-06-27).
    analysis_resp = get_supabase().table("analyses").select("document_id,bank_name").execute()
    doc_to_bank: dict[str, str] = {}
    for a in cast(list[dict[str, Any]], analysis_resp.data or []):
        bank = a.get("bank_name")
        doc_id = a.get("document_id")
        if isinstance(bank, str) and bank.strip() and isinstance(doc_id, str):
            doc_to_bank[doc_id] = bank.strip()

    # Per-bank accumulator: hint text built by merging every successive
    # observation. Audit one row per bank when we actually write.
    bank_pending_hint: dict[str, str] = {}
    bank_doc_count: dict[str, int] = {}

    for batch_idx in range(0, len(docs), batch_size):
        batch = docs[batch_idx : batch_idx + batch_size]
        print(
            f"# generate-from-existing batch {batch_idx // batch_size + 1} ({len(batch)} docs)",
            file=sys.stderr,
        )
        for doc in batch:
            doc_id = str(doc.get("id") or "")
            bank_name = doc_to_bank.get(doc_id)
            if not bank_name:
                continue  # no analysis / no bank name → skip
            try:
                plaintext = asyncio.run(asyncio.to_thread(pdf_store.fetch_plaintext, UUID(doc_id)))
            except Exception as exc:
                print(
                    f"  doc={doc_id[:8]} pdf_store fetch failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                continue
            try:
                with pymupdf.open(  # type: ignore[no-untyped-call]
                    stream=plaintext, filetype="pdf"
                ) as pdf:
                    first_page_text = pdf.load_page(0).get_text("text") or ""
            except Exception as exc:
                print(
                    f"  doc={doc_id[:8]} page-1 extraction failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                continue
            # Minimal parse_result shim: no classified transactions
            # available retroactively (we don't re-run the pipeline),
            # so the running-balance signal is dropped — the generator
            # gracefully omits that sentence when no transactions are
            # found in the shim.
            from types import SimpleNamespace

            shim = SimpleNamespace(
                classified=SimpleNamespace(transactions=[]),
                extraction=None,
            )
            new_hint = generate_hints_from_parse_result(
                bank_name=bank_name,
                first_page_text=first_page_text,
                parse_result=shim,
            )
            if not new_hint:
                continue
            current = bank_pending_hint.get(bank_name)
            bank_pending_hint[bank_name] = merge_hints(current, new_hint)
            bank_doc_count[bank_name] = bank_doc_count.get(bank_name, 0) + 1
        # Inter-batch pause unless we're on the last batch.
        if batch_idx + batch_size < len(docs):
            import time

            time.sleep(sleep_between_batches_s)

    # Convert per-bank accumulator into SeedOutcomes — one per bank.
    outcomes: list[SeedOutcome] = []
    for bank_name, hint_text in sorted(bank_pending_hint.items()):
        excerpt = _excerpt(hint_text)
        doc_count = bank_doc_count.get(bank_name, 0)
        if not apply_writes:
            outcomes.append(
                SeedOutcome(
                    bank_name=bank_name,
                    hint_excerpt=excerpt,
                    action="would_set",
                    detail=(
                        f"DRY-RUN — would set source='auto' hints from {doc_count} doc(s), merged"
                    ),
                )
            )
            continue
        try:
            row = repo.set_hints(bank_name=bank_name, hints=hint_text, source="auto")
        except BankLayoutWriteError as exc:
            outcomes.append(
                SeedOutcome(
                    bank_name=bank_name,
                    hint_excerpt=excerpt,
                    action="error",
                    detail=f"set_hints failed: {type(exc).__name__}: {exc}",
                )
            )
            continue
        audit.record(
            actor=_ACTOR,
            action="bank_layouts.auto_hints_generated",
            subject_type="bank_layout",
            subject_id=row.id,
            details={
                "bank_name": bank_name,
                "source_doc_count": doc_count,
                "hint_length": len(hint_text),
                "hint_excerpt": excerpt,
                "post_write_source": row.hints_source,
            },
        )
        outcomes.append(
            SeedOutcome(
                bank_name=bank_name,
                hint_excerpt=excerpt,
                action="set",
                detail=(
                    f"auto-hints written from {doc_count} doc(s); "
                    f"row id={row.id} source={row.hints_source}"
                ),
            )
        )
    return outcomes


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
    p.add_argument(
        "--bump-parse-count",
        action="store_true",
        help=(
            "Operator-authorized backfill: UPDATE bank_layouts SET "
            "successful_parses = GREATEST(current, --target-count) for the "
            "bank passed via --bank-name. Used to lift a single sparse-"
            "history bank across HINTS_AVAILABLE_THRESHOLD so its authored "
            "extraction_hints start feeding the prompt on the next parse. "
            "Requires --bank-name. DRY-RUN unless --apply is also passed."
        ),
    )
    p.add_argument(
        "--bank-name",
        type=str,
        default=None,
        help=(
            "Bank name to act on. REQUIRED with --bump-parse-count; ignored "
            "in the default hints-seeding mode (that path iterates the "
            "_BANK_HINTS dict)."
        ),
    )
    p.add_argument(
        "--target-count",
        type=int,
        default=HINTS_AVAILABLE_THRESHOLD,
        help=(
            "Target value for successful_parses when --bump-parse-count is "
            f"set (default {HINTS_AVAILABLE_THRESHOLD}, the current "
            "HINTS_AVAILABLE_THRESHOLD). The UPDATE is GREATEST(current, "
            "target) — never lowers an already-higher count."
        ),
    )
    p.add_argument(
        "--generate-from-existing",
        action="store_true",
        help=(
            "Retroactive auto-hint generation: scan every doc at "
            "parse_status='proceed' with a sealed pdf_store blob, "
            "decrypt page 1, derive auto-hints via "
            "aegis.bank_layouts.auto_hints, and write source='auto' hints "
            "for any bank that lacks them. Batched 5 docs per round with "
            "1s sleep between rounds to spare the pdf_store fetch path. "
            "DRY-RUN unless --apply is also passed. Banks with existing "
            "manual hints have the auto observations MERGED in "
            "(upgrades the row's hints_source to 'mixed' under the "
            "repository's normal promotion rules)."
        ),
    )
    args = p.parse_args()
    if args.bump_parse_count and not args.bank_name:
        p.error("--bump-parse-count requires --bank-name")
    if args.bump_parse_count and args.target_count < 1:
        p.error("--target-count must be a positive integer")
    if args.bump_parse_count and args.generate_from_existing:
        p.error("--bump-parse-count and --generate-from-existing are mutually exclusive")
    return args


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
    if args.bump_parse_count:
        outcomes.append(
            _bump_parse_count_one(
                bank_name=args.bank_name,
                target_count=args.target_count,
                repo=repo,
                audit=audit,
                apply_writes=args.apply,
            )
        )
    elif args.generate_from_existing:
        outcomes.extend(
            _generate_from_existing(
                repo=repo,
                audit=audit,
                apply_writes=args.apply,
            )
        )
    else:
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
