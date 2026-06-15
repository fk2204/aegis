"""Bank-layout learning package — operator-curated extraction hints.

The parser pipeline records a layout fingerprint + bumps a success
counter on every successful bank-statement parse (parse_status in
('proceed', 'review')). After ``HINTS_AVAILABLE_THRESHOLD`` successful
parses on the same bank, the operator can author free-form
``extraction_hints`` text that the pipeline injects verbatim into the
Bedrock extraction system prompt on subsequent parses of statements
from that bank.

The table is operator-curated metadata, not merchant-keyed PII:
fingerprint dicts NEVER contain account holder names, transaction
descriptions, or other merchant identifiers (see migration 059 header
for the full contract). The operator UI lives at ``/ui/bank-layouts``.

Threshold gating lives in ``repository.HINTS_AVAILABLE_THRESHOLD`` so a
future tune (e.g. "wait until 5 parses") is a single constant edit, not
a code search.
"""

from aegis.bank_layouts.models import BankLayoutRow
from aegis.bank_layouts.repository import (
    HINTS_AVAILABLE_THRESHOLD,
    BankLayoutRepository,
    BankLayoutWriteError,
    InMemoryBankLayoutRepository,
    SupabaseBankLayoutRepository,
)

__all__ = [
    "HINTS_AVAILABLE_THRESHOLD",
    "BankLayoutRepository",
    "BankLayoutRow",
    "BankLayoutWriteError",
    "InMemoryBankLayoutRepository",
    "SupabaseBankLayoutRepository",
]
