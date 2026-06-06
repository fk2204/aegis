"""PII redaction for test fixtures captured from real prod data.

CLAUDE.md "external-integration test discipline" says: tests must
validate against CAPTURED real payloads, never hand-written synthetic
ones. CLAUDE.md PII rule says: transaction descriptions are PII and
must not appear in places they don't need to be.

These two rules combine into a workflow: capture the real bytes, then
**immediately** redact PII before writing the fixture to disk. The
historical mistake (2026-06-05, `ae62df2`) was a redaction pass that
ran AFTER an initial fixture commit with a regex that missed the
"Zelle payment to" rows, putting named individuals' names into
``git`` history on the private repo.

This module exists so future captures can't repeat that mistake.
Callers do two things:

1. Build the captured payload as a ``dict`` exactly as the upstream
   API returned it.
2. Run it through ``sanitize_fixture_payload`` before writing it.
   The returned payload has every known PII pattern replaced with a
   stable placeholder so the structural shapes (regex match points,
   bundle-matcher pairing logic) still work.

The companion test ``test_fixture_pii_canary.py`` scans every
committed fixture under ``tests/**/fixtures/*.json`` for known PII
patterns and fails CI if any leak through. The sanitizer + canary
together make PII fixtures fail loudly before they ship.

What gets redacted
==================

* ``Zelle payment from|to <NAME>`` — the named individual / business
  after the prefix, up to the first ``Conf#`` token or end of string.
* ``INDN:<MERCHANT NAME>`` — the merchant name in ACH transaction
  descriptions (``INDN:`` = beneficiary name in NACHA format).
* ``VU DEVELOPMENT`` / ``VU DEVELOPMENT CO`` — the specific merchant
  whose data the foundation fixture was captured from. Generalize to
  ``REDACTED_MERCHANT_CO`` so the merchant identity isn't tied to
  the fixture.

What is NOT redacted (intentional)
==================================

* Confirmation# / TRN tracking numbers — structural identifiers used
  by the bundle matcher. Not PII.
* Account last-4s (CHK 7722, CRD 0993) — last-4 of an account is not
  PII per industry convention; full account numbers would be.
* Amount, date, posted_date — not PII.
* Brand names that are processors / payment rails — Shopify, Stripe,
  WooPayments, Venmo, etc. Public companies, not PII.
* CHECKCARD vendor lines (AMAZON MKTPL*…, MURPHY EXPRESS …) — these
  are public merchant identifiers, not customer PII. They could
  reveal the captured merchant's spending pattern but the brand
  names themselves are not PII.

Stable redaction mapping
========================

When a name appears multiple times, the same name maps to the same
``REDACTED_PARTY_N`` placeholder. The mapping is per-call (per
fixture-capture run) — there's no cross-fixture identity to leak.
"""

from __future__ import annotations

import re
from typing import Any

# Public companies / payment rails / processors that should never be
# redacted — they're the patterns the classifier matches against.
_NEVER_REDACT_TOKENS: frozenset[str] = frozenset(
    {
        "VENMO", "PAYPAL", "WOOPAYMENTS", "WOOPAY", "SHOPIFY", "STRIPE",
        "SQUARE", "CLOVER", "AMAZON PAY", "AMAZONPAY", "CPS MERCHANT SER",
        "BANK OF AMERICA", "BOOK", "WIRE TYPE", "DES", "INDN", "ID",
        "ACCTVERIFY", "CASHOUT", "ACH", "CRD", "CHK", "SAV", "ET", "TIME",
        "DATE", "TRN", "SNDR", "REF", "RELATED", "CONFIRMATION",
    }
)


def _make_name_redactor() -> tuple[
    re.Pattern[str], re.Pattern[str], dict[str, str]
]:
    """Build the stable name → REDACTED_PARTY_N mapping closure."""
    name_map: dict[str, str] = {}

    def _next_placeholder(real: str) -> str:
        key = real.strip()
        if key not in name_map:
            name_map[key] = f"REDACTED_PARTY_{len(name_map) + 1}"
        return name_map[key]

    # "Zelle payment (from|to) <NAME> [Conf#…]"
    # ``NAME`` captured non-greedy up to ``Conf#`` or end of string.
    zelle_re = re.compile(
        r"(Zelle payment (?:from|to))\s+(?P<name>.+?)(?=\s+Conf#|\s*$)",
        re.IGNORECASE,
    )

    # The merchant-name-in-INDN pattern.
    indn_re = re.compile(r"\bVU DEVELOPMENT(?:\s+CO)?\b", re.IGNORECASE)

    return zelle_re, indn_re, name_map


def sanitize_transaction_description(
    description: str,
    name_map: dict[str, str] | None = None,
) -> str:
    """Redact PII from one transaction description.

    Preserves all structural shapes the classifier matches on; only
    proper-name substrings are replaced. ``name_map`` lets a caller
    accumulate a stable mapping across multiple sanitize calls so the
    same name always maps to the same placeholder.
    """
    if name_map is None:
        name_map = {}

    zelle_re, indn_re, _ = _make_name_redactor()

    def _zelle_sub(m: re.Match[str]) -> str:
        prefix = m.group(1)
        name = m.group("name").strip()
        if name not in name_map:
            name_map[name] = f"REDACTED_PARTY_{len(name_map) + 1}"
        return f"{prefix} {name_map[name]}"

    out = zelle_re.sub(_zelle_sub, description)
    out = indn_re.sub("REDACTED_MERCHANT_CO", out)
    return out


def sanitize_fixture_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Walk a captured-from-prod transactions fixture and redact PII.

    Expected shape: ``{"documents": [{"transactions": [{"description":
    ...}, ...]}, ...]}`` — the shape used by VU's fixture and any
    follow-on transaction captures.

    Returns a NEW dict (does not mutate the input) so the caller can
    keep the raw payload in memory for debugging without ever writing
    it to disk.
    """
    out = dict(payload)
    docs_in = payload.get("documents") or []
    name_map: dict[str, str] = {}
    new_docs = []
    for d in docs_in:
        d2 = dict(d)
        txns = d.get("transactions") or []
        new_txns = []
        for t in txns:
            t2 = dict(t)
            desc = t.get("description")
            if isinstance(desc, str):
                t2["description"] = sanitize_transaction_description(
                    desc, name_map
                )
            new_txns.append(t2)
        d2["transactions"] = new_txns
        new_docs.append(d2)
    out["documents"] = new_docs
    return out


def assert_no_pii_in_descriptions(payload: dict[str, Any]) -> None:
    """Loud fail if any transaction description still carries the
    known PII patterns. Called by the canary test and by capture
    scripts as a belt-and-suspenders check before write."""
    leaks: list[str] = []
    zelle_re, indn_re, _ = _make_name_redactor()
    for d in payload.get("documents") or []:
        for t in d.get("transactions") or []:
            desc = t.get("description")
            if not isinstance(desc, str):
                continue
            for m in zelle_re.finditer(desc):
                name = m.group("name").strip()
                if not name.startswith("REDACTED_PARTY_"):
                    leaks.append(f"zelle-named: {desc[:100]!r}")
                    break
            if indn_re.search(desc):
                leaks.append(f"indn-merchant: {desc[:100]!r}")
    if leaks:
        raise AssertionError(
            "fixture payload still carries un-redacted PII:\n  "
            + "\n  ".join(leaks[:10])
            + (f"\n  ...and {len(leaks)-10} more" if len(leaks) > 10 else "")
        )


__all__ = [
    "assert_no_pii_in_descriptions",
    "sanitize_fixture_payload",
    "sanitize_transaction_description",
]
