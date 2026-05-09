"""State-routing disclosure renderer.

Routes a deal's state to the right rendering path:
  - Tier 1 → render the regulator's prescribed Jinja template at
    `compliance/templates/{template_path}`.
  - Tier 2 → render the generic acknowledgment in `_generic_templates`
    citing the general law.
  - Tier 3 → raise `StateNotAudited` so the caller surfaces "compliance
    research not yet completed for this state" to the operator.

A state not present in `STATES` (i.e., not one of the 45 served) raises
`StateNotServed` — the API rejects upstream with `state_not_served`.

Jinja autoescape is on for every render path so user inputs (merchant
name, owner name) cannot inject HTML.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from jinja2 import (
    BaseLoader,
    DictLoader,
    Environment,
    FileSystemLoader,
    StrictUndefined,
    select_autoescape,
)
from pydantic import BaseModel, ConfigDict

from aegis.compliance._generic_templates import TIER2_GENERIC_ACKNOWLEDGMENT
from aegis.compliance.states import (
    STATES,
    TEMPLATES_DIR,
    StateNotAudited,
    StateNotServed,
    Tier1Regulation,
    Tier2Regulation,
)
from aegis.scoring.models import ScoreInput, ScoreResult

_GENERIC_TEMPLATE_KEY = "tier2_generic"


class RenderedDisclosure(BaseModel):
    """The output of `render_disclosure`. `html` is the rendered document."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    state: str
    tier: int
    html: str
    citation: str


def _build_environment() -> Environment:
    """Tier 1 templates from disk; Tier 2 generic from the constants module.

    Both loaders share an autoescape policy so all renders escape user input.
    """

    class _ChainedLoader(BaseLoader):
        def __init__(self, loaders: list[BaseLoader]) -> None:
            self._loaders = loaders

        def get_source(
            self, environment: Environment, template: str
        ) -> tuple[str, str | None, Any]:
            from jinja2.exceptions import TemplateNotFound

            for ldr in self._loaders:
                try:
                    return ldr.get_source(environment, template)
                except TemplateNotFound:
                    continue
            raise TemplateNotFound(template)

    chained = _ChainedLoader(
        [
            FileSystemLoader(str(TEMPLATES_DIR)),
            DictLoader({_GENERIC_TEMPLATE_KEY: TIER2_GENERIC_ACKNOWLEDGMENT}),
        ]
    )
    return Environment(
        loader=chained,
        autoescape=select_autoescape(enabled_extensions=("html", "j2", "html.j2")),
        undefined=StrictUndefined,  # rendering with a missing var raises, not silently ""
        keep_trailing_newline=True,
    )


_ENV = _build_environment()


def render_disclosure(
    state: str,
    deal: ScoreInput,
    score: ScoreResult,
    *,
    rendered_at: datetime | None = None,
) -> RenderedDisclosure:
    """Render the state's disclosure for a scored deal.

    `state` is a USPS code (case-insensitive). `deal` carries merchant
    identity + requested terms. `score` carries the tier-derived factor +
    holdback the disclosure cites.

    Raises:
      StateNotServed   — state not in the 45 served states.
      StateNotAudited  — state is in STATES but currently Tier 3.
    """
    abbr = (state or "").upper()
    reg = STATES.get(abbr)
    if reg is None:
        raise StateNotServed(f"state_not_served: {state!r}")

    if reg.tier == 3:
        raise StateNotAudited(abbr)

    rendered_at = rendered_at or datetime.now(UTC)
    context = _build_context(reg, deal, score, rendered_at.date())

    if isinstance(reg, Tier1Regulation):
        template = _ENV.get_template(reg.template_path)
        html = template.render(**context)
        return RenderedDisclosure(
            state=abbr, tier=1, html=html, citation=reg.bill_number
        )

    if not isinstance(reg, Tier2Regulation):  # defensive — Tier 3 handled above
        raise RuntimeError(
            f"unexpected regulation tier for {abbr}: {type(reg).__name__}"
        )
    template = _ENV.get_template(_GENERIC_TEMPLATE_KEY)
    html = template.render(**context)
    return RenderedDisclosure(
        state=abbr, tier=2, html=html, citation=reg.general_law_citation
    )


def _build_context(
    reg: Tier1Regulation | Tier2Regulation,
    deal: ScoreInput,
    score: ScoreResult,
    rendered_at: date,
) -> dict[str, object]:
    principal = score.suggested_max_advance or deal.requested_amount
    factor = score.recommended_factor_rate or deal.requested_factor
    total_repayment = (principal * factor).quantize(Decimal("0.01"))

    common: dict[str, object] = {
        "state": reg.state,
        "state_name": reg.state_name,
        "verified_date": reg.verified_date.isoformat(),
        "rendered_at": rendered_at.isoformat(),
        "business_name": deal.business_name,
        "owner_name": deal.owner_name,
        "principal": str(principal),
        "factor": str(factor),
        "total_repayment": str(total_repayment),
    }
    if isinstance(reg, Tier1Regulation):
        # effective_date_statute and effective_date_regulations are both
        # Optional on the model — NY's dossier glues SB 5470 + S898 into a
        # single bill_number and does not quote the original statute date.
        # Format defensively.
        common.update(
            {
                "bill_number": reg.bill_number,
                "effective_date_statute": (
                    reg.effective_date_statute.isoformat()
                    if reg.effective_date_statute is not None
                    else ""
                ),
                "effective_date_regulations": (
                    reg.effective_date_regulations.isoformat()
                    if reg.effective_date_regulations is not None
                    else ""
                ),
                "mandatory_compliance_date": (
                    reg.mandatory_compliance_date.isoformat()
                    if reg.mandatory_compliance_date is not None
                    else ""
                ),
                "statute_citation": reg.statute_citation,
                "regulation_citation": reg.regulation_citation,
                "citation_url_statute": reg.citation_url_statute,
                "citation_url_regulation": reg.citation_url_regulation,
                "prescribed_form_section": reg.prescribed_form_section,
                "apr_calculation_method": reg.apr_calculation_method,
                "coj_allowed": reg.coj_allowed,
                "coj_citation": reg.coj_citation,
                "notes": reg.notes,
            }
        )
    else:
        common.update(
            {
                "general_law_citation": reg.general_law_citation,
                "citation_url": reg.citation_url,
                "notes": reg.notes,
            }
        )
    return common


__all__ = ["RenderedDisclosure", "render_disclosure"]
