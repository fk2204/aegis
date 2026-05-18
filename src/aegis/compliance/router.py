"""State-aware compliance router — pure function over a loaded matrix.

Master plan §11 task 4: given a state code, deal amount, and product
type, return the tier the deal sits in, the applicable rules, the
disclosure template path, and any hard-decline rules that fire.

Design constraints:
- Pure function. No IO. Idempotent. The matrix is loaded once at boot;
  the router operates on the in-memory ``StateMatrix``.
- Decimal money. Never ``float``.
- Fail closed on unknown state codes — the router returns a defensive
  Tier 3 routing only if the code is in the matrix; otherwise it raises.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from aegis.compliance.state_matrix import (
    HardDeclineRule,
    StateMatrix,
    Tier1Regulation,
    Tier2Regulation,
    Tier3Regulation,
)

# Product types the router accepts. Identical to ``ProductScope`` in the
# matrix module but kept as a separate frozenset so the router can validate
# inputs without exposing the Literal type at the call site.
_PRODUCT_TYPES: Final[frozenset[str]] = frozenset(
    {"sales_based", "closed_end", "open_end", "factoring", "lease", "asset_based"}
)


class UnknownStateError(ValueError):
    """Raised when the state code is not in the loaded matrix.

    50 states + DC = 51 codes are guaranteed present by ``load_matrix()``.
    A miss here means the caller passed something else (territory, typo).
    """


class UnknownProductTypeError(ValueError):
    """Raised when the product type is not in the canonical set."""


class StateMatrixCorruptionError(RuntimeError):
    """Defensive — should be unreachable given the discriminated union.

    Raised only if the loaded matrix is hand-mutated post-load to contain
    a regulation model the router does not recognize. Distinct from
    ``StateMatrixError`` so callers can tell load-time failures from
    in-memory corruption.
    """


class RouterResult(BaseModel):
    """Outcome of a single router call.

    - ``tier``: 1, 2, or 3.
    - ``applicable_rules``: stable identifiers for the regulatory surface
      that applies (e.g. ``"ca_sb1235"``, ``"tier_3_defensive"``).
    - ``template_path``: repo-relative path to the Jinja template for
      this state's disclosure, when one exists. None for Tier 2/3 (no
      state-specific template) and for Tier 1 states whose template has
      not yet been built (Phase 5).
    - ``hard_decline_rules``: list of rules that hard-decline this deal
      under the state's CFDL. Empty for most deals; non-empty when, e.g.,
      a TX deal uses standard ACH auto-debit (HB 700).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: int = Field(ge=1, le=3)
    applicable_rules: list[str]
    template_path: Path | None
    hard_decline_rules: list[HardDeclineRule]


def _normalize_state(state_code: str) -> str:
    """Uppercase + strip. The matrix uses USPS uppercase 2-letter codes."""
    return state_code.strip().upper()


def _matches_product_scope(
    regulation: Tier1Regulation, product_type: str
) -> bool:
    """Return True iff this Tier 1 CFDL covers the given product type."""
    return product_type in regulation.cfdl.product_scope


def _within_threshold(
    regulation: Tier1Regulation, deal_amount: Decimal
) -> bool:
    """Return True iff the deal amount triggers the CFDL disclosure.

    Per master plan §9.1: ``no_threshold=True`` (LA HB 470) means the
    CFDL applies regardless of amount; otherwise the threshold is an
    inclusive USD ceiling.
    """
    if regulation.cfdl.no_threshold:
        return True
    threshold = regulation.cfdl.threshold_usd
    if threshold is None:
        # Tier 1 with neither a threshold nor no_threshold=True is a
        # schema violation; the loader rejects it. Defensively return
        # False here so the router never silently includes a malformed
        # entry.
        return False
    return deal_amount <= threshold


def _rule_code_for_tier1(state_code: str, regulation: Tier1Regulation) -> str:
    """Stable rule identifier surfaced to audit_log.

    The format is ``<state>_tier1_<n>``-indexed by statute count. The
    common case is one statute → ``<state>_tier1``. CA has two (SB 1235
    + SB 362) → ``ca_tier1`` (the rule code stays stable; the statute
    list is on the matrix entry).
    """
    return f"{state_code.lower()}_tier1"


def router(
    state_code: str,
    deal_amount: Decimal,
    product_type: str,
    matrix: StateMatrix,
) -> RouterResult:
    """Route a deal to its compliance surface.

    The matrix must be a fully validated ``StateMatrix`` (returned by
    ``load_matrix()``). The function is pure: no IO, no caching, no
    global state. Calling it repeatedly with the same arguments returns
    equal results.

    Raises:
        UnknownStateError: state code not present in the matrix.
        UnknownProductTypeError: product type not in the canonical set.
        TypeError: ``deal_amount`` is not a ``Decimal`` (we never accept
            ``float`` for money — master plan §2 / CLAUDE.md).
    """
    if not isinstance(deal_amount, Decimal):
        raise TypeError(
            f"deal_amount must be Decimal, got {type(deal_amount).__name__}"
        )
    if product_type not in _PRODUCT_TYPES:
        raise UnknownProductTypeError(
            f"unknown product_type: {product_type!r}; expected one of "
            f"{sorted(_PRODUCT_TYPES)}"
        )

    normalized = _normalize_state(state_code)
    regulation = matrix.states.get(normalized)
    if regulation is None:
        raise UnknownStateError(f"state_code not in matrix: {state_code!r}")

    if isinstance(regulation, Tier1Regulation):
        return _route_tier1(normalized, regulation, deal_amount, product_type)
    if isinstance(regulation, Tier2Regulation):
        return _route_tier2(normalized)
    if isinstance(regulation, Tier3Regulation):
        return _route_tier3(normalized)
    # Should be unreachable because StateRegulation is a closed union.
    raise StateMatrixCorruptionError(  # pragma: no cover
        f"unexpected tier model for {normalized}: {type(regulation).__name__}"
    )


def _route_tier1(
    state_code: str,
    regulation: Tier1Regulation,
    deal_amount: Decimal,
    product_type: str,
) -> RouterResult:
    """Tier 1: CFDL applies if product+amount fit; otherwise tier=3 default.

    A Tier 1 state's CFDL may not apply to every deal:
      - CT covers MCA only — a CT closed-end loan falls through to
        Tier 3 defensive posture.
      - TX has a $1M disclosure threshold — TX deals above $1M still
        fire registration but not the disclosure template.
    The router surfaces this by:
      - in-scope deals: ``tier=1`` + state's template path + applicable
        rules + hard-decline rules.
      - out-of-scope deals: ``tier=3`` + defensive disclosure rules.
        Hard-decline rules from the Tier 1 statute still apply (TX's
        auto-debit prohibition fires regardless of amount).
    """
    in_scope = _matches_product_scope(regulation, product_type) and _within_threshold(
        regulation, deal_amount
    )
    if in_scope:
        template_path: Path | None = (
            Path(regulation.cfdl.template_path) if regulation.cfdl.template_path else None
        )
        return RouterResult(
            tier=1,
            applicable_rules=[_rule_code_for_tier1(state_code, regulation)],
            template_path=template_path,
            hard_decline_rules=list(regulation.hard_decline_rules),
        )
    # Out of CFDL scope, but Tier 1 overlays / hard-decline rules still
    # apply (notably TX auto-debit, which is overlay-level, not amount-
    # or product-scoped).
    return RouterResult(
        tier=3,
        applicable_rules=[
            "tier_3_defensive",
            f"{state_code.lower()}_overlays_apply",
        ],
        template_path=None,
        hard_decline_rules=list(regulation.hard_decline_rules),
    )


def _route_tier2(state_code: str) -> RouterResult:
    """Tier 2: watch list. Routed like Tier 3 until promoted to Tier 1."""
    return RouterResult(
        tier=2,
        applicable_rules=[
            "tier_3_defensive",  # generates the same defensive disclosure
            f"{state_code.lower()}_watchlist",
        ],
        template_path=None,
        hard_decline_rules=[],
    )


def _route_tier3(state_code: str) -> RouterResult:
    """Tier 3: defensive disclosure only."""
    return RouterResult(
        tier=3,
        applicable_rules=[
            "tier_3_defensive",
            f"{state_code.lower()}_general_law",
        ],
        template_path=None,
        hard_decline_rules=[],
    )


__all__ = [
    "RouterResult",
    "StateMatrixCorruptionError",
    "UnknownProductTypeError",
    "UnknownStateError",
    "router",
]
