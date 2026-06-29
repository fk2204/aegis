"""Pydantic models for the tax-return parser.

``TaxFormType`` enumerates the four business-tax forms AEGIS extracts:
1120 (C-corp), 1120-S (S-corp), 1065 (partnership), Schedule C
(sole-prop on a 1040). The 1040 personal return is mentioned in the
build plan §6.1 but its schema overlaps with Schedule C (sole-prop
returns); the extractor handles 1040+Schedule C as a single
"schedule_c" path. The migration's CHECK constraint accepts ``1040``
as a future-compatible value.

``TaxReturnExtraction`` is the shape every per-form extractor returns.
Optional figures (officer_compensation, partner_distributions, etc.)
are ``None`` when the form type does not surface that line item — a
Schedule C never has officer compensation; a 1120 never has partner
distributions. The dossier renderer hides ``None`` rows.

Money columns use the project-wide ``Money`` alias so the
numeric(14,2) Postgres round-trip is exact — see CLAUDE.md "Money
math: never float" rule.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

TaxFormType = Literal["1120", "1120s", "1065", "schedule_c"]

# Money columns are numeric(14,2) on the wire (migration 093). Decimal
# end-to-end so the same Pydantic validation guards both the extractor
# output and the repository upsert.
Money = Annotated[Decimal, Field(max_digits=14, decimal_places=2)]


class TaxReturnExtraction(BaseModel):
    """Validated tax-return extraction result.

    All monetary fields are Optional because no single form populates
    every column — see the per-form prompts in ``extract.py`` for which
    fields each form is expected to fill. ``form_type`` and
    ``tax_year`` are REQUIRED on every form; an extraction missing
    either is non-recoverable and the extractor returns ``None``
    instead of constructing a partial model.

    ``tax_year`` is the year of the return (the year the figures
    describe, NOT the year the return was filed). The IRS calls this
    the "tax period" — printed on the form's top-line as
    "Tax year 2024" or "For tax year beginning ___ ending ___".

    ``net_income`` is the form's natural net-income line:
      * 1120: line 30 ("Taxable income")
      * 1120-S: line 21 ("Ordinary business income (loss)")
      * 1065: line 22 ("Ordinary business income (loss)")
      * Schedule C: line 31 ("Net profit or (loss)")
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    form_type: TaxFormType
    tax_year: int = Field(ge=2000, le=2100)
    gross_receipts: Money | None = None
    net_income: Money | None = None
    total_assets: Money | None = None
    total_liabilities: Money | None = None
    officer_compensation: Money | None = None
    # 1120-S shareholder W-2 compensation. Distinct from
    # officer_compensation on 1120 (C-corps file officer comp on
    # Schedule E; S-corps file shareholder comp on line 7).
    shareholder_compensation: Money | None = None
    # 1065 partner distributions ("Distributions" on Schedule K).
    partner_distributions: Money | None = None
    # Schedule C cost of goods sold (line 42) + total expenses
    # (line 28). Separate so the dossier can show "gross -
    # COGS = gross profit, gross profit - expenses = net".
    cogs: Money | None = None
    total_expenses: Money | None = None


__all__ = ["Money", "TaxFormType", "TaxReturnExtraction"]
