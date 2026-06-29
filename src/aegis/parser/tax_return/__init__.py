"""Tax return parser (Bedrock vision).

Detects business tax returns (Forms 1120 / 1120-S / 1065 / Schedule C
1040) by filename and first-page text, then extracts the structured
figures the dossier surfaces on a "Tax Return Summary" section
(year-over-year gross receipts + net income comparison).

Routing entrypoint
------------------
``detect_tax_form_type(filename, first_page_text)`` returns one of
``"1120" | "1120s" | "1065" | "schedule_c"`` or ``None`` when neither
the filename nor the first-page text carries an unambiguous form
marker. PURE string match; no I/O. Callers (worker / upload route)
that get a non-``None`` answer route the document to this parser
INSTEAD of the bank-statement pipeline — tax returns and bank
statements have nothing in common (no reconciliation, no transaction
table, no period tie-out) so misrouting either way produces garbage.

Extraction entrypoint
---------------------
``extract_tax_return(file_path, form_type, llm_client=...)`` runs the
Bedrock-vision forced tool-use call for the supplied form type and
returns a validated ``TaxReturnExtraction`` (or ``None`` when the
extraction is non-recoverable — empty PDF, sanitiser rejection,
Pydantic validation failure).

Each form type has its own schema and prompt — the C-corp 1120
asks for officer compensation + total liabilities, the partnership
1065 asks for partner distributions, etc. See ``prompts.py``
constants for the exact prompt strings.

Per CLAUDE.md's "Extraction & automation assists, never replaces
judgment" rule, the extracted row is staged on the ``tax_returns``
table and surfaced on the dossier; the operator confirms or edits
via the same merchant edit surface. The extractor is PURE — it
takes a PDF path and an LLM client and returns a model, never
touching the merchant repository.
"""

from __future__ import annotations

from aegis.parser.tax_return.extract import (
    detect_tax_form_type,
    encode_pdf_for_bedrock,
    extract_tax_return,
)
from aegis.parser.tax_return.models import TaxFormType, TaxReturnExtraction
from aegis.parser.tax_return.repository import (
    InMemoryTaxReturnRepository,
    SupabaseTaxReturnRepository,
    TaxReturnRepository,
    TaxReturnRow,
    TaxReturnWriteError,
)

__all__ = [
    "InMemoryTaxReturnRepository",
    "SupabaseTaxReturnRepository",
    "TaxFormType",
    "TaxReturnExtraction",
    "TaxReturnRepository",
    "TaxReturnRow",
    "TaxReturnWriteError",
    "detect_tax_form_type",
    "encode_pdf_for_bedrock",
    "extract_tax_return",
]
