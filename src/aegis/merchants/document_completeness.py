"""Legacy document-completeness checker — thin adapter over stips evaluator.

As of Sprint 6 Track A this module is a backward-compatible adapter
over ``aegis.scoring_v2.stips.evaluate_stips``. The structured stips
evaluator is the new canonical surface; this module preserves the
``check_completeness`` / ``DocumentCompletenessWarning`` shape so
existing callers and tests continue to pass.

Legacy behaviour (unchanged):
  * Returns a list of ``DocumentCompletenessWarning`` — one per
    REQUIRED-and-MISSING document the funder published.
  * Recognises only the three structured kinds: ``voided_check``,
    ``drivers_license``, ``bank_statements_months``. "Unknown"
    requirements (free-text bullets that don't match those keywords)
    are intentionally filtered out — they're the new ``unknown``
    bucket in ``StipsResult`` and the dossier surfaces them as yellow
    chips instead of as legacy red warnings. This filtering preserves
    the pre-Sprint-6 contract: the legacy gate only blocked on
    structured kinds, and so does this adapter.
  * Whitespace-only bullets skipped silently.
  * Empty ``conditional_requirements`` returns ``[]``.

New callers should use ``evaluate_stips`` directly to get the full
bucketed view (required / on_file / missing / unknown).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from aegis.funders.models import FunderRow
from aegis.merchants.models import MerchantRow
from aegis.scoring_v2.stips import evaluate_stips


class DocumentCompletenessWarning(BaseModel):
    """One missing-document warning surfaced to the operator.

    ``requirement_kind`` is a stable machine-readable string the
    template can use to render a per-warning chip. ``requirement_text``
    is the exact funder requirement string the scan matched against
    (verbatim so the operator can see what the funder said). ``missing_field``
    is the ``MerchantRow`` attribute the operator should toggle on the
    intake / merchant-edit form to clear the warning.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_kind: str = Field(min_length=1)
    requirement_text: str = Field(min_length=1)
    missing_field: str = Field(min_length=1)


# Structured kinds the legacy checker tracks. The new stips evaluator
# also emits ``unknown`` items; we filter those out here so legacy
# callers see the identical pre-Sprint-6 contract.
_STRUCTURED_KINDS: frozenset[str] = frozenset(
    {"voided_check", "drivers_license", "bank_statements_months"}
)


def check_completeness(
    *,
    merchant: MerchantRow,
    funder: FunderRow,
) -> list[DocumentCompletenessWarning]:
    """Compare ``funder.conditional_requirements`` against the merchant's
    document-on-file flags. Return one warning per unmet STRUCTURED
    requirement (voided check / driver's license / N-months statements).

    Adapter: delegates to ``aegis.scoring_v2.stips.evaluate_stips`` and
    filters ``StipsResult.missing`` to the three structured kinds. The
    unknown bucket is intentionally excluded — the legacy contract only
    blocked on structured kinds and this adapter preserves that.

    Empty list is the "all clear" signal — the submit-to-funder gate
    that uses this adapter still opens only when this returns ``[]``.
    An empty ``conditional_requirements`` tuple trivially returns ``[]``.
    """
    result = evaluate_stips(funder, merchant)

    warnings: list[DocumentCompletenessWarning] = []
    for item in result.missing:
        if item.kind not in _STRUCTURED_KINDS:
            continue
        # ``StipItem.missing_field`` is non-None for every structured
        # kind by construction in ``stips._classify_bullet``. The
        # ``or ""`` is a defensive fallback for mypy --strict; the
        # ``min_length=1`` constraint on the field would catch a real
        # None at validation time.
        warnings.append(
            DocumentCompletenessWarning(
                requirement_kind=item.kind,
                requirement_text=item.requirement_text,
                missing_field=item.missing_field or "",
            )
        )
    return warnings


__all__ = [
    "DocumentCompletenessWarning",
    "check_completeness",
]
