"""Fintech bank-account detection — warning-only soft signal.

WHY THIS IS WARNING-ONLY, NEVER A DECLINE:
==========================================
THE MASTER PLAN POSITION ON BANK-OF-RECORD POLICY IS THAT EACH FUNDER
OWNS ITS OWN APPETITE FOR FINTECH BANK ACCOUNTS (MERCURY, BREX, NOVO,
ETC.). SOME FUNDERS DECLINE THEM OUTRIGHT BECAUSE FINTECH BANKS LACK
THE TRADITIONAL ACH DEBIT CONTROLS / DAILY-REMIT GUARANTEES; OTHERS
ACCEPT THEM. AEGIS IS A PRE-SCREENING TOOL FOR COMMERA CAPITAL (A PURE
ISO BROKER) AND DOES NOT UNILATERALLY DECLINE ON A SIGNAL THAT IS
FUNDER-DEPENDENT. THIS DETECTOR EMITS A SURFACE-ONLY ``[WARN]`` FLAG
SO THE OPERATOR + THE PER-FUNDER MATCH GRID SEE THE BANK AS A SOFT
CONCERN; THE PARSE_STATUS BRANCH IS UNTOUCHED, FRAUD_SCORE IS
UNTOUCHED, AND NO TRACK A / TRACK B / TRACK C SEVERITY IS ASSIGNED.

The list below is curated from the operator-visible fintech / neobank
landscape that produces business-checking statements at scale. New
entries land here when the operator confirms an upstream merchant
banks with them; we do NOT add sub-brands automatically (e.g.
"Mercury Treasury", "Brex Cash") because the LLM-extracted
``bank_name`` already collapses those variants through substring
matching below.
"""

from __future__ import annotations

from typing import Final

# Canonical lowercase fintech bank identifiers. Keys are the
# substring matched against the lowercased ``bank_name``; values are
# the operator-facing canonical display string used in the warning
# message. Order is informational — matching scans every entry and
# returns the first containment hit (lists are short and static, so
# the linear walk is fine — no regex compile overhead).
#
# Substring matching is intentional: "MERCURY BUSINESS CHECKING",
# "Mercury Bank, N.A.", and "MERCURY" all land on the same warning
# without the operator having to enumerate every variant. The static
# list bounds the false-positive surface; if a real bank name ever
# collides we add a longer disambiguating substring at that time.
FINTECH_BANK_IDENTIFIERS: Final[dict[str, str]] = {
    "mercury": "Mercury",
    "brex": "Brex",
    "bluevine": "Bluevine",
    "novo": "Novo",
    "relay": "Relay",
    "lili": "Lili",
    "found": "Found",
    "rho": "Rho",
    "arc": "Arc",
    "nearside": "Nearside",
    "oxygen": "Oxygen",
    "northone": "NorthOne",
}


def detect_fintech_bank(bank_name: str | None) -> tuple[str, str] | None:
    """Case-insensitive substring match against the fintech-bank list.

    Returns ``(canonical_name, warning_message)`` on the first hit, or
    ``None`` when ``bank_name`` is missing / empty / not on the list.

    The warning message is the operator-readable string suitable for
    surfacing on the funder-match grid; the canonical name is the
    display form (mixed case) suitable for inclusion in flag text.
    """
    if not bank_name:
        return None
    needle = bank_name.strip().lower()
    if not needle:
        return None
    for key, canonical in FINTECH_BANK_IDENTIFIERS.items():
        if key in needle:
            warning = (
                f"Merchant banks with {canonical}. Verify funder accepts "
                f"fintech bank accounts before submitting."
            )
            return canonical, warning
    return None


__all__ = [
    "FINTECH_BANK_IDENTIFIERS",
    "detect_fintech_bank",
]
