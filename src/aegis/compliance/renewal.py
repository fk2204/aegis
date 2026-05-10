"""Renewal handling for state-specific disclosure rendering.

Some states require a fresh disclosure for renewal financings AND
specific renewal-only content. NY requires anti-double-dipping
disclosure under § 600.6(b)(3)(v); CA / FL / GA require fresh
disclosures for renewals (because a renewal is a new commercial
financing transaction) but do not impose renewal-only content.

This module owns:
  * ``TransactionType``     — the enum the dossier maps to operator-
    flagged intake state.
  * ``RenewalContext``      — Pydantic model carrying prior-position
    facts the operator (or a future bank-statement auto-detector)
    surfaces.
  * ``build_state_renewal_context()`` — helper that translates a
    ``RenewalContext`` into the state-specific dict that the
    disclosure template merges into its render context.

What this module does NOT do
----------------------------
* Auto-detect renewals from bank statements. Per dossier 14 the
  operator-flagged path is the source of truth for now; auto-detect
  is "medium confidence."
* Enforce a "Renewal" header on CA disclosures. Per CORRECTIONS
  Correction 3, no specific provision in 10 CCR §§ 900-956 mandates
  that exact label — the original dossier guidance was secondary
  industry interpretation.
* Anti-double-dipping math. That lives in ``ny_double_dipping.py``;
  this module imports and orchestrates it.
* Pricing-communication APR re-disclosure. That's already enforced
  by ``pricing_guard.py`` (CA SB 362 + NY § 600.1 / § 600.3); it
  applies to every pricing communication regardless of new vs renewal.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.compliance.ny_double_dipping import compute_double_dipping_amount
from aegis.logger import get_logger

_log = get_logger(__name__)


class RenewalContextRequiredError(ValueError):
    """Raised when a state requires renewal context but the caller didn't supply one.

    Currently only NY raises this — the § 600.6(b)(3)(v) double-dipping
    paragraph cannot be computed without the four prior-position facts.
    """


class TransactionType(StrEnum):
    """The operator-flagged transaction shape per dossier 14.

    Per dossier:
      * NEW            — first funding for the merchant
      * RENEWAL        — new agreement, same merchant, prior position
                         being repaid. New disclosure required (CA, NY,
                         FL, GA all treat as new commercial financing).
      * MODIFICATION   — amend existing agreement (term, payment freq,
                         fee waiver). NO new disclosure required in
                         CA/NY/FL/GA per dossier 14.
      * FORBEARANCE    — pause / reduce payments temporarily. NO new
                         disclosure.
      * DEFAULT_WORKOUT — restructuring after default. State-specific;
                          dossier defers to counsel.

    String values are stable identifiers for storage / API contract.
    """

    NEW = "new"
    RENEWAL = "renewal"
    MODIFICATION = "modification"
    FORBEARANCE = "forbearance"
    DEFAULT_WORKOUT = "default_workout"


class RenewalContext(BaseModel):
    """Prior-position facts that drive renewal-disclosure rendering.

    The four ``prior_*`` decimals are required to compute the NY
    anti-double-dipping disclosure. CA / FL / GA renewals don't need
    them; pass them anyway when known so the audit trail is complete
    and the data is available if state law tightens.

    ``prior_deal_id`` links to AEGIS's own record when AEGIS originated
    the prior position; otherwise None when the prior position was
    funded outside AEGIS and the operator only has the four numbers.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )

    prior_deal_id: UUID | None = None

    # Per docs/compliance/02_new_york.md anti-double-dipping math
    # (§ 600.6(b)(3)(v)). Decimal-only — no float at the boundary
    # (CLAUDE.md money rule). All four are required when the renewal is
    # for a NY merchant; ``build_state_renewal_context`` enforces.
    prior_funded_amount: Decimal = Field(gt=Decimal("0"))
    prior_total_payback: Decimal = Field(gt=Decimal("0"))
    prior_amount_repaid: Decimal = Field(ge=Decimal("0"))
    prior_position_payoff_from_renewal: Decimal = Field(ge=Decimal("0"))

    detection_method: str = Field(
        default="operator_flagged",
        pattern=r"^(operator_flagged|auto_detected|funder_disclosed)$",
        description=(
            "How AEGIS knows the deal is a renewal. Operator-flagged is "
            "the source of truth for now; auto_detected and "
            "funder_disclosed reserved for future paths."
        ),
    )
    detection_confidence: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=Decimal("1"),
        description=(
            "0..1 confidence score for auto-detected renewals; None for "
            "operator-flagged (which is presumed certain)."
        ),
    )


# Money formatting helper: matches the existing template inputs which
# all arrive pre-formatted as ``"$X,XXX.XX"``. Keeping it local rather
# than pulling in ``money.py`` formatters keeps the renewal module
# self-contained.
def _format_dollars(amount: Decimal) -> str:
    return f"${amount:,.2f}"


# State codes that require renewal-specific disclosure content.
# Currently NY only — § 600.6(b)(3)(v). CA / FL / GA generate fresh
# disclosures for renewals but do not have renewal-only content.
_STATES_REQUIRING_RENEWAL_CONTEXT: Final[frozenset[str]] = frozenset({"NY"})


def build_state_renewal_context(
    state: str,
    renewal: RenewalContext | None,
) -> dict[str, object]:
    """Translate a ``RenewalContext`` into state-specific template context.

    Returns a dict that the disclosure router can merge into the base
    template context. Empty dict for states without renewal-only
    content; populated for NY with the double-dipping disclosure
    fields the ``ny_cfdl.html.j2`` template's Row 1 conditional
    consumes.

    Parameters
    ----------
    state
        USPS code (case-insensitive). Drives state-specific routing.
    renewal
        Optional ``RenewalContext``. May be None for non-renewal deals
        OR for renewals in states that don't require the context (CA /
        FL / GA). For NY renewals, MUST be non-None — see ``Raises``.

    Returns
    -------
    dict[str, object]
        State-specific template fields. For NY renewals, returns
        ``{"is_renewal_with_double_dip": True,
        "double_dipping_amount": "$X,XXX.XX"}``. For NY non-renewals,
        returns ``{"is_renewal_with_double_dip": False,
        "double_dipping_amount": "$0.00"}`` (the template still
        consumes both keys; the Boolean gates the conditional). For
        other states, returns ``{}``.

    Raises
    ------
    RenewalContextRequiredError
        When ``state == "NY"`` and the caller signals a renewal (by
        passing a non-None ``renewal``) but the NY-required prior-
        position fields somehow can't yield a computation. Pydantic
        validates field shape at ``RenewalContext`` construction, so
        in practice this fires only on caller errors that bypass the
        model.
    """
    abbr = (state or "").upper()

    if abbr not in _STATES_REQUIRING_RENEWAL_CONTEXT:
        # CA / FL / GA / IL / Tier 3 — no renewal-only content. The
        # disclosure renders normally; the operator flagged it as a
        # renewal for audit purposes only.
        if renewal is not None:
            _log.info(
                "renewal.no_state_specific_content state=%s prior_deal_id=%s",
                abbr,
                renewal.prior_deal_id,
            )
        return {}

    # NY path. Both renewal and non-renewal deals need the two template
    # keys (the template's StrictUndefined would raise otherwise).
    if renewal is None:
        return {
            "is_renewal_with_double_dip": False,
            "double_dipping_amount": "$0.00",
        }

    try:
        double_dipping = compute_double_dipping_amount(
            prior_funded_amount=renewal.prior_funded_amount,
            prior_total_payback=renewal.prior_total_payback,
            prior_amount_repaid=renewal.prior_amount_repaid,
            renewal_amount_used_to_pay_prior=(
                renewal.prior_position_payoff_from_renewal
            ),
        )
    except ValueError as exc:
        # Re-raise as the renewal-specific error so callers can branch
        # on it without inspecting the underlying double-dipping module.
        raise RenewalContextRequiredError(
            f"NY renewal disclosure cannot be computed: {exc}"
        ) from exc

    _log.info(
        "renewal.ny_double_dipping_computed state=NY prior_deal_id=%s "
        "double_dipping_amount=%s",
        renewal.prior_deal_id,
        double_dipping,
    )

    return {
        "is_renewal_with_double_dip": True,
        "double_dipping_amount": _format_dollars(double_dipping),
    }


__all__ = [
    "RenewalContext",
    "RenewalContextRequiredError",
    "TransactionType",
    "build_state_renewal_context",
]
