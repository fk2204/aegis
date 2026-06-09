"""Tier 1 disclosure-context builder for CA / NY / FL / GA.

Each Tier 1 Jinja template at ``compliance/templates/`` expects ~20
computed variables (funding_provided, finance_charge, apr,
payment_terms_text, estimated_term, prepayment_*_text, etc.). This
module derives every required variable from a ``ScoreInput`` + a
``ScoreResult`` using ``Decimal`` math throughout and the
``compliance/apr.py`` actuarial APR engine — never simple interest,
never ``float`` for money or rate fields.

Why this lives outside disclosure.py
------------------------------------
``disclosure.py`` owns routing (state → tier → template). Context
derivation is its own concern with its own state-specific quirks
(NY uses "finance charges you pay"; FL omits APR; GA is content-based
with APR; CA adds the monthly-cost insert). Pulling it into a dedicated
module keeps the router thin and the context computation testable in
isolation.

Working around the missing Deal entity
--------------------------------------
A canonical ``Deal`` Pydantic model does not exist in this codebase yet
(see ``src/aegis/deals/models.py`` — that file scopes
``(merchant_id, document_id)`` deal IDs only, not a financing-terms
container). For now we derive the disclosure context from
``(ScoreInput, ScoreResult)`` plus three optional kwargs:

* ``funder_name`` — defaults to ``"Commera Capital"``. Replace with a
  per-deal funder once Phase 7B's funder-submission flow records the
  selected funder on the deal.
* ``disbursement_date`` — defaults to ``rendered_at``. Replace with the
  contractually scheduled disbursement date once intake captures it.
* ``payment_terms`` — currently synthesized as a daily payment stream
  from ``requested_term_days`` and the recommended amount/factor.
  Replace with the contract's actual payment schedule once a Deal /
  contract entity exists.

When ``Deal`` arrives, swap these kwargs for ``deal.funder``,
``deal.disbursement_date``, ``deal.payment_terms`` — the call sites in
``render_disclosure()`` are the only ones to update.

State-specific quirks
---------------------
* **CA SB 1235 / 10 CCR § 914** — 9-row table; APR row + monthly-cost
  insert; finance charge per § 943; estimated term per § 901.
* **NY 23 NYCRR § 600.6** — 10-row table (adds Collateral Requirements
  Row 10); identical to CA except APR wording uses "finance charges you
  pay" rather than "fees you pay", and Row 1 carries the
  § 600.6(b)(3)(v) anti-double-dipping paragraph on renewals (handled
  separately by ``renewal.py``).
* **FL Fla. Stat. § 559.9613** — content-based (definition list, not
  table); SIX required items; **no APR row** (the dossier explicitly
  enumerates "No APR row" as a CA/NY difference; ``apr_required=False``
  on the regulation row). FL requires a contract-section reference for
  prepayment regardless of whether costs/discounts exist.
* **GA O.C.G.A. § 10-1-393.18** — content-based like FL but **with APR**
  (item 5 in the seven-item list). Uses ``total_amount_to_be_paid``
  rather than FL's ``total_amount_business_must_pay``.

PII guard
---------
This module receives ``business_name`` / ``owner_name`` through the
``ScoreInput`` but only renders them into HTML, never into logs. No
``logger.info("...", business_name=...)`` calls anywhere.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from aegis.compliance.apr import APRCalculationError, calculate_apr
from aegis.compliance.states import Tier1Regulation
from aegis.logger import get_logger
from aegis.scoring.models import ScoreInput, ScoreResult

_log = get_logger(__name__)


class APRDisclosureError(RuntimeError):
    """Raised when APR cannot be computed for a disclosure.

    Wraps ``compliance.apr.APRCalculationError`` and adds the deal-context
    fields a compliance auditor will want (state, principal, factor,
    term_days, disbursement_date) so the audit-log entry written by the
    catch site is self-describing.

    The disclosure router catches this and refuses to issue the
    disclosure — silently substituting a 0.00% APR (the prior behavior)
    is a CA DFPI §§ 940/942 material defect. The deal is routed to
    ``needs_review`` per R0.4 of the 2026-06-08 audit remediation plan.
    """

    def __init__(
        self,
        message: str,
        *,
        state: str | None = None,
        principal: Decimal | None = None,
        factor: Decimal | None = None,
        term_days: int | None = None,
        disbursement_date: date | None = None,
        deal_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.principal = principal
        self.factor = factor
        self.term_days = term_days
        self.disbursement_date = disbursement_date
        self.deal_id = deal_id

# Default funder identification used when the caller does not supply
# one. Replace with the per-deal funder once Phase 7B records it.
DEFAULT_FUNDER_NAME: Final[str] = "Commera Capital"

# CA SB 1235 § 22802(b)(7) — DFPI requires disclosure of estimated savings
# vs alternative financing. AEGIS currently makes no comparable
# alternative-financing offer (broker / single-channel MCA), so the
# defensible default is the regulator-accepted "Not Applicable" stance
# with rationale text. DFPI accepts N/A when there's genuinely no
# comparable offer made, but the row MUST be rendered — it cannot be
# omitted. When an alternative financing offer system lands, callers
# may override the default by passing ``savings_amount`` +
# ``savings_comparison_text`` to ``build_tier1_disclosure_context``.
DEFAULT_SAVINGS_NA_RATIONALE: Final[str] = (
    "Recipient was not offered an alternative financing product for "
    "comparison; this section is not applicable. "
    "(10 CCR § 914, Cal. Fin. Code § 22802(b)(7))"
)

# Daily-payment business-day denominator used to derive the daily
# payment when only the term in days + total repayment are known.
# § 942 / § 600.7 frame the periodic payment as the average of the
# payments under the contract; for a fixed daily MCA on M-F that
# average is total_repayment / term_business_days.
_BUSINESS_DAYS_PER_WEEK: Final[int] = 5
_CALENDAR_DAYS_PER_WEEK: Final[int] = 7

# Months-per-year denominator for the monthly-cost insert per § 942.
# § 901 says "expressed in months, or a year/month combination", and
# § 942 derives the monthly cost by annualizing the periodic payment
# stream and dividing by 12.
_MONTHS_PER_YEAR: Final[int] = 12
_DAYS_PER_MONTH: Final[Decimal] = Decimal("30.4167")  # 365 / 12

# Quantization helper for dollar formatting.
_CENT = Decimal("0.01")


def _fmt_dollars(amount: Decimal) -> str:
    """Format a Decimal as ``"$X,XXX.XX"``. Always 2dp, comma-grouped."""
    quantized = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    return f"${quantized:,.2f}"


def _fmt_percent(rate_fraction: Decimal) -> str:
    """Format an APR Decimal (0.365) as ``"36.50%"``. Always 2dp."""
    percent = (rate_fraction * Decimal("100")).quantize(_CENT, rounding=ROUND_HALF_UP)
    return f"{percent:.2f}%"


def _fmt_term(days: int) -> str:
    """Format a term in days per § 901 / § 600.7.

    Per regulator guidance: terms of one year or less may be expressed
    in days; longer terms in years/months. AEGIS MCAs are virtually
    always < 1 year so the days form is the common case.
    """
    if days <= 365:
        return f"{days} days"
    years, remainder_days = divmod(days, 365)
    months = int((Decimal(remainder_days) / _DAYS_PER_MONTH).quantize(Decimal("1")))
    if months == 0:
        return f"{years} years"
    return f"{years} years {months} months"


def _derive_payment_schedule(
    principal: Decimal,
    factor: Decimal,
    term_days: int,
    disbursement_date: date,
) -> tuple[list[tuple[date, Decimal]], Decimal, int]:
    """Synthesize a daily M-F payment stream from term + factor.

    Returns ``(payments, daily_payment, business_days)`` where:
      * ``payments`` is a list of (payment_date, payment_amount) pairs,
        one per business day in the term window, suitable for
        ``calculate_apr()``.
      * ``daily_payment`` is the average payment per § 942 / § 600.7
        (total repayment / business-day count, 2dp).
      * ``business_days`` is the integer business-day count over the
        term (M-F days strictly after disbursement).

    This is the placeholder schedule used while ``Deal`` does not exist.
    Once the contract's actual payment schedule lives on a ``Deal``
    entity, replace this with ``deal.payment_schedule``.
    """
    total_repayment = (principal * factor).quantize(_CENT, rounding=ROUND_HALF_UP)

    # Walk forward day-by-day from the day after disbursement, collecting
    # M-F dates only. ``weekday()`` returns 0=Mon..4=Fri,5=Sat,6=Sun.
    business_dates: list[date] = []
    cursor = disbursement_date + timedelta(days=1)
    end = disbursement_date + timedelta(days=term_days)
    while cursor <= end:
        if cursor.weekday() < _BUSINESS_DAYS_PER_WEEK:
            business_dates.append(cursor)
        cursor += timedelta(days=1)

    if not business_dates:
        # Degenerate term (term_days < 1 business day). Bail with a
        # single payment on the next day so APR can still compute.
        single_date = disbursement_date + timedelta(days=1)
        return [(single_date, total_repayment)], total_repayment, 1

    # Average payment per § 942: total / count, 2dp. The last payment
    # absorbs any rounding residual so the sum reconciles exactly.
    daily_payment = (total_repayment / Decimal(len(business_dates))).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )
    payments: list[tuple[date, Decimal]] = [
        (d, daily_payment) for d in business_dates[:-1]
    ]
    paid_so_far = daily_payment * Decimal(len(business_dates) - 1)
    last_payment = (total_repayment - paid_so_far).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )
    payments.append((business_dates[-1], last_payment))

    return payments, daily_payment, len(business_dates)


def _compute_apr_for_disclosure(
    principal: Decimal,
    factor: Decimal,
    term_days: int,
    disbursement_date: date,
) -> Decimal:
    """Compute APR via scipy actuarial method. Returns Decimal fraction.

    Wraps ``compliance/apr.py:calculate_apr`` — never simple interest.
    On degenerate input (where brentq can't bracket) re-raises
    ``APRCalculationError`` so the caller can convert the failure into a
    structured ``APRDisclosureError`` carrying the deal context (state,
    principal, factor, term, disbursement date). The disclosure router
    then halts for review rather than rendering "0.00%" — silently
    substituting a zero APR is a CA DFPI §§ 940/942 material defect and
    must NEVER ship in a delivered disclosure (R0.4 audit remediation).
    """
    payments, _, _ = _derive_payment_schedule(
        principal, factor, term_days, disbursement_date
    )
    return calculate_apr(principal, payments, disbursement_date)


def _payment_terms_text(
    daily_payment: Decimal,
    holdback_pct: Decimal,
) -> str:
    """Per § 914 Row 6 / § 600.6 Row 6: payment terms description.

    AEGIS defaults to the split-rate variant (% of receipts) because the
    dossier explicitly enumerates this as the typical MCA shape. When
    the contract is preset-with-true-up or minimum-payment, the caller
    can override by passing a ``payment_terms_text`` kwarg — TODO when
    Deal entity carries a payment-terms enum.
    """
    if holdback_pct > 0:
        holdback_str = f"{(holdback_pct * Decimal('100')).quantize(_CENT)}%"
        return (
            f"Each business day, your payment processor will remit "
            f"{holdback_str} of your gross receipts to the provider, "
            f"and send any remaining amounts to you. This financing "
            f"does not have a fixed payment schedule and there is no "
            f"minimum payment amount."
        )
    return (
        f"Each business day, the provider will debit "
        f"{_fmt_dollars(daily_payment)} from the recipient's designated "
        f"account until the total payment amount has been remitted."
    )


def _build_common_tier1_fields(
    deal: ScoreInput,
    score: ScoreResult,
    rendered_at: date,
    funder_name: str,
    disbursement_date: date | None,
    *,
    savings_amount: Decimal | None = None,
    savings_comparison_text: str | None = None,
) -> dict[str, object]:
    """Computed fields shared across all four Tier 1 templates.

    Derives funding_provided, finance_charge, apr (when required by the
    state), estimated_total_payment, estimated_term, payment_amount,
    and the boolean flags every template's StrictUndefined consumes.

    Savings disclosure (CA SB 1235 § 22802(b)(7) — R0.3 audit fix):
      ``savings_amount`` + ``savings_comparison_text`` populate the CA
      template's savings row. When both are None (default — AEGIS has
      no alternative-financing offer system today), the row renders as
      ``Not Applicable`` with the regulator-accepted rationale text in
      ``DEFAULT_SAVINGS_NA_RATIONALE``. DFPI accepts N/A when there is
      genuinely no comparable offer made; the row MUST be rendered.
    """
    # Use scorer-recommended terms when present; fall back to the
    # operator-quoted requested terms otherwise. This matches the
    # existing _build_context() shape in disclosure.py.
    principal: Decimal = (
        score.suggested_max_advance
        if score.suggested_max_advance > 0
        else deal.requested_amount
    )
    factor: Decimal = (
        score.recommended_factor_rate
        if score.recommended_factor_rate > 0
        else deal.requested_factor
    )
    term_days: int = (
        score.estimated_payback_days
        if score.estimated_payback_days is not None
        and score.estimated_payback_days > 0
        else deal.requested_term_days
    )
    disbursement: date = disbursement_date or rendered_at

    total_repayment = (principal * factor).quantize(_CENT, rounding=ROUND_HALF_UP)
    finance_charge = (total_repayment - principal).quantize(
        _CENT, rounding=ROUND_HALF_UP
    )
    try:
        apr_fraction = _compute_apr_for_disclosure(
            principal, factor, term_days, disbursement
        )
    except APRCalculationError as exc:
        # NEVER silently substitute 0.00% — R0.4 audit gate. The router
        # caller catches APRDisclosureError and halts the disclosure for
        # operator review with a structured audit_log entry.
        #
        # We log a non-PII summary at error level so the operator sees
        # the failure in tail. Merchant identifiers are NOT included
        # (they live on `deal` which is in the caller's scope).
        _log.error(
            "compliance.apr.compute_failed state=%s term_days=%s factor=%s principal=%s",
            getattr(deal, "state", "?"),
            term_days,
            factor,
            principal,
        )
        raise APRDisclosureError(
            f"APR computation failed for disclosure: {exc}",
            state=getattr(deal, "state", None),
            principal=principal,
            factor=factor,
            term_days=term_days,
            disbursement_date=disbursement,
            deal_id=(
                str(deal.merchant_id) if getattr(deal, "merchant_id", None) else None
            ),
        ) from exc
    _, daily_payment, _business_days = _derive_payment_schedule(
        principal, factor, term_days, disbursement
    )

    # § 942 monthly cost: total repayment / (term_days / 30.4167).
    # Avoids div-zero by guarding term_days > 0 at term_days source.
    if term_days > 0:
        months_decimal = Decimal(term_days) / _DAYS_PER_MONTH
        monthly_cost = (
            total_repayment / months_decimal
            if months_decimal > 0
            else total_repayment
        ).quantize(_CENT, rounding=ROUND_HALF_UP)
    else:
        monthly_cost = total_repayment

    # avg_monthly_income for the § 940/§ 600.6 APR row. AEGIS does not
    # store the merchant's projected monthly income separately; use the
    # parser-derived monthly_revenue as the best available proxy (it is
    # deposits net of transfers + chargebacks, projected to 30 days).
    avg_monthly_income = deal.monthly_revenue.quantize(_CENT, rounding=ROUND_HALF_UP)

    holdback_pct = score.recommended_holdback_pct or Decimal("0")
    payment_terms_text = _payment_terms_text(daily_payment, holdback_pct)

    return {
        # Identity
        "financer_name": funder_name,
        # Row 1 — funding & deductions. AEGIS does not net out third-
        # party payoffs at disclosure time today; ``recipient_funds`` =
        # funding_provided. When intake captures itemized deductions,
        # flip ``recipient_funds_lt_funding`` to True and populate the
        # itemization attachment.
        "funding_provided": _fmt_dollars(principal),
        "recipient_funds": _fmt_dollars(principal),
        "recipient_funds_lt_funding": False,
        "pays_third_party_payoffs": False,
        "third_party_payoff_note": "",
        # Row 2 — APR (CA/NY/GA only; FL omits via template branch).
        "apr": _fmt_percent(apr_fraction),
        "payment_channel": (
            "daily ACH debit from the recipient's business deposit account"
            if holdback_pct == 0
            else "split-funded credit card processor remittance"
        ),
        "avg_monthly_income": _fmt_dollars(avg_monthly_income),
        "finance_is_fee_based": True,
        # Row 3 — Finance Charge.
        "finance_charge": _fmt_dollars(finance_charge),
        "finance_charge_can_increase": False,
        # Row 4 — Estimated Total Payment.
        "estimated_total_payment": _fmt_dollars(total_repayment),
        # Row 5 — Estimated Payment.
        "estimated_payment_amount": _fmt_dollars(daily_payment),
        "estimated_payment_freq": "business day",
        "irregular_payments_note": "",
        # Row 6 — Payment Terms (pre-rendered).
        "payment_terms_text": payment_terms_text,
        # Row 7 — Estimated Term.
        "estimated_term": _fmt_term(term_days),
        "estimated_term_explanation": (
            "Term assumes the recipient's projected income remains constant "
            "over the term of the contract."
        ),
        # Monthly-cost insert (all MCAs are non-monthly periodic).
        "has_monthly_cost_insert": True,
        "estimated_monthly_cost": _fmt_dollars(monthly_cost),
        "monthly_cost_derivation": (
            "Estimated monthly cost is the total estimated payment "
            "amount divided by the term expressed in months."
        ),
        # Rows 8-9 — Prepayment.
        "prepayment_finance_charge_text": (
            "If you prepay this financing in full or in part, the "
            "finance charge will not be reduced — it is fully earned "
            "at funding."
        ),
        "prepayment_additional_fee_text": (
            "There is no additional fee for prepayment."
        ),
        # CA SB 1235 § 22802(b)(7) Savings disclosure (R0.3).
        # ``has_savings_disclosure=True`` when the caller supplied a
        # concrete savings_amount + comparison text (an alternative
        # financing offer was made). Otherwise the row renders as
        # "Not Applicable" with the DFPI-accepted rationale below.
        "has_savings_disclosure": (
            savings_amount is not None and savings_comparison_text is not None
        ),
        "savings_amount": (
            _fmt_dollars(savings_amount) if savings_amount is not None else "N/A"
        ),
        "savings_comparison_text": (
            savings_comparison_text
            if savings_comparison_text is not None
            else DEFAULT_SAVINGS_NA_RATIONALE
        ),
        # Rendering metadata.
        "rendered_at": rendered_at.isoformat(),
    }


def build_tier1_disclosure_context(
    reg: Tier1Regulation,
    deal: ScoreInput,
    score: ScoreResult,
    rendered_at: date,
    *,
    funder_name: str | None = None,
    disbursement_date: date | None = None,
    savings_amount: Decimal | None = None,
    savings_comparison_text: str | None = None,
) -> dict[str, object]:
    """Build the full template context for a Tier 1 state's disclosure.

    Parameters
    ----------
    reg
        The Tier 1 regulation entry from STATES (CA/NY/FL/GA today).
    deal
        ``ScoreInput`` carrying merchant identity + requested terms.
        Used as a stand-in for a future ``Deal`` entity.
    score
        ``ScoreResult`` carrying recommended factor/holdback/term.
    rendered_at
        Date the disclosure is being rendered (typically today UTC).
    funder_name
        Provider name to interpolate into the template. Defaults to
        ``"Commera Capital"`` until per-deal funder tracking lands.
    disbursement_date
        Date the merchant will receive funds. Defaults to
        ``rendered_at``. Drives the APR day-count basis.

    Returns
    -------
    dict[str, object]
        Every variable each of CA/NY/FL/GA templates expects (the union
        of all four docstrings' required-context lists). Renewal-only
        fields (NY's ``is_renewal_with_double_dip`` /
        ``double_dipping_amount``) are added downstream by
        ``renewal.build_state_renewal_context``.
    """
    funder = funder_name or DEFAULT_FUNDER_NAME

    common = _build_common_tier1_fields(
        deal,
        score,
        rendered_at,
        funder,
        disbursement_date,
        savings_amount=savings_amount,
        savings_comparison_text=savings_comparison_text,
    )

    # State-specific overlay. Each state's template's docstring lists
    # the variables it expects; we layer them on top of common rather
    # than mixing into a giant dict so the per-state delta is obvious.
    state = reg.state

    if state == "CA":
        # CA template's variable set is exactly common. No additions.
        return common

    if state == "NY":
        # NY adds Row 10 — Collateral Requirements. AEGIS does not
        # currently capture per-deal collateral; default to "None"
        # (matches the typical unsecured MCA structure described in
        # the dossier). When intake adds a collateral field, route it
        # here.
        common["collateral_requirements_text"] = (
            "There are no collateral requirements for this financing. "
            "The provider's recourse is limited to the receivables "
            "purchased under the agreement."
        )
        # Renewal double-dipping defaults — overwritten by
        # build_state_renewal_context() in disclosure.py when the deal
        # is a renewal. Both keys MUST be defined for StrictUndefined.
        common.setdefault("is_renewal_with_double_dip", False)
        common.setdefault("double_dipping_amount", "$0.00")
        return common

    if state == "FL":
        # FL's variable set differs from CA: § 559.9613(2) uses six
        # named items, not row labels, and the variable names follow
        # the statute (funds_provided / funds_disbursed_to_business /
        # total_amount_business_must_pay / total_dollar_cost / payment_*
        # / prepayment_*). Drop CA-specific keys and emit FL's set.
        return _adapt_to_fl(common)

    if state == "GA":
        # GA mirrors FL's content-based shape but adds APR (item 5)
        # and uses ``total_amount_to_be_paid`` rather than FL's
        # ``total_amount_business_must_pay``. So FL adapter + APR + key
        # rename.
        return _adapt_to_ga(common)

    # Defensive — should never happen because the four states above
    # exhaust the Tier 1 set today. If a new Tier 1 state is added
    # without a context adapter here, fail loudly rather than render
    # a partial disclosure.
    raise NotImplementedError(
        f"Tier 1 disclosure context not implemented for state {state!r} — "
        f"add an adapter to compliance/disclosure_context.py"
    )


def _adapt_to_fl(common: dict[str, object]) -> dict[str, object]:
    """Translate the CA-style common context to FL § 559.9613 variable names.

    FL is content-based (six items) and explicitly omits APR. Variables
    consumed by ``fl_fcfdl.html.j2``:

      funds_provided, funds_disbursed_to_business,
      funds_disbursed_lt_provided, deductions_explanation,
      total_amount_business_must_pay, total_dollar_cost,
      payment_amounts_may_vary, payment_manner, payment_frequency,
      payment_amount_or_first_estimate, has_prepayment_costs_or_discounts,
      prepayment_terms_text, prepayment_contract_provision_ref,
      rendered_at, financer_name.
    """
    # ACH-vs-split-rate manner derived from the common's payment_channel.
    channel = str(common["payment_channel"])
    payment_manner = (
        "ACH debit" if channel.startswith("daily ACH") else "split-funded card receipts"
    )
    # Variable payments only when split-funded; fixed when daily ACH.
    payment_amounts_may_vary = payment_manner != "ACH debit"

    return {
        "financer_name": common["financer_name"],
        "funds_provided": common["funding_provided"],
        "funds_disbursed_to_business": common["recipient_funds"],
        "funds_disbursed_lt_provided": common["recipient_funds_lt_funding"],
        "deductions_explanation": common["third_party_payoff_note"],
        "total_amount_business_must_pay": common["estimated_total_payment"],
        "total_dollar_cost": common["finance_charge"],
        "payment_amounts_may_vary": payment_amounts_may_vary,
        "payment_manner": payment_manner,
        "payment_frequency": "each business day",
        "payment_amount_or_first_estimate": common["estimated_payment_amount"],
        "has_prepayment_costs_or_discounts": False,
        "prepayment_terms_text": "",
        # § 559.9613(2)(f) requires a reference to the contract section
        # governing prepayment regardless of whether costs/discounts
        # exist. The reference is the Master Financing Agreement
        # section AEGIS standardly numbers as the prepayment clause.
        # When the contract template's section number changes, update
        # this string in lockstep.
        "prepayment_contract_provision_ref": (
            "Section 7 of the Master Financing Agreement"
        ),
        "rendered_at": common["rendered_at"],
    }


def _adapt_to_ga(common: dict[str, object]) -> dict[str, object]:
    """Translate the CA-style common context to GA § 10-1-393.18 variable names.

    GA is content-based like FL but adds APR (item 5) and renames the
    "amount business must pay" field to ``total_amount_to_be_paid``.
    Variables consumed by ``ga_sb90.html.j2``:

      financer_name, funds_provided, funds_disbursed_to_business,
      funds_disbursed_lt_provided, deductions_explanation,
      total_amount_to_be_paid, total_dollar_cost, apr,
      payment_amounts_may_vary, payment_manner, payment_frequency,
      payment_amount_or_first_estimate, has_prepayment_costs_or_discounts,
      prepayment_terms_text, rendered_at.
    """
    fl_shape = _adapt_to_fl(common)
    # Drop FL-only key, rename amount-to-pay, add APR.
    fl_shape.pop("total_amount_business_must_pay", None)
    fl_shape.pop("prepayment_contract_provision_ref", None)
    fl_shape["total_amount_to_be_paid"] = common["estimated_total_payment"]
    fl_shape["apr"] = common["apr"]
    return fl_shape


__all__ = [
    "DEFAULT_FUNDER_NAME",
    "APRDisclosureError",
    "build_tier1_disclosure_context",
]
