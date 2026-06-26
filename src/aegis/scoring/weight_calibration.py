"""Weight-drift calibration engine — outcome feedback loop foundation.

Reads ``deal_outcomes`` (migration 074) joined to ``decisions`` (migration
015) and produces a shadow weight-calibration report. Output is purely
informational — never mutates ``FRAUD_WEIGHTS`` directly. Same
shadow-first discipline that ships every other scoring change
(CLAUDE.md "Decision-boundary changes — deliberate + shadow-first"):

  1. Engine returns a ``WeightDriftReport`` per flag_code with the
     empirical fired-on-charge-off vs not-fired-on-charge-off rates.
  2. ``/ui/calibration`` surfaces the report; operator reviews each row.
  3. Operator records accepted / rejected / deferred into
     ``weight_calibration_log``.
  4. Operator manually edits ``FRAUD_WEIGHTS`` in code after reviewing
     the full report — never auto-tuning.

For each ``FRAUD_WEIGHTS`` key we treat the bucket as "fired" when the
``decisions.score_factors -> breakdown`` JSON carries a non-zero value
for that key. ``score_factors`` is written by
``aegis.api.routes.deals._record_decision`` with the shape::

    {
      "tier": "A" | "B" | "C" | "D" | "F",
      "breakdown": {"metadata": 18.5, "math": 22.0, "patterns": 7.3, ...},
      "soft_concerns": [...]
    }

Decimal-only money / ratio math (CLAUDE.md "NEVER use float for money").
The two-place rounding on ``current_weight`` matches the migration 074
``numeric(6,2)`` shape for the calibration log; ratios round to four
places to preserve enough precision for the dossier surface without
producing noisy trailing digits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aegis.db import get_supabase
from aegis.logger import get_logger
from aegis.parser.pipeline import FRAUD_WEIGHTS

if TYPE_CHECKING:
    pass

_log = get_logger(__name__)


# Outcomes that the calibration engine treats as the negative end of the
# empirical comparison. Mirrors the migration-074 CHECK enum vocabulary.
NEGATIVE_OUTCOMES: frozenset[str] = frozenset({"charged_off", "defaulted"})

# Outcomes excluded from the calibration sample entirely. ``pending`` has
# no signal yet; ``paying`` is still in-flight (could flip either way).
# Only terminal outcomes contribute to the ratio.
TERMINAL_OUTCOMES: frozenset[str] = frozenset(
    {
        "paid_in_full",
        "charged_off",
        "defaulted",
        "renewed",
    }
)

# Confidence-band thresholds. ``low`` below 30 outcomes — too few to draw
# a calibration signal from. ``high`` at 200+ outcomes (~2 funded deals
# per week for 2 years at this scale, or a denser book). ``medium`` is
# the band in between where the report is suggestive but not yet
# decisive.
_LOW_SAMPLE_FLOOR: int = 30
_HIGH_SAMPLE_FLOOR: int = 200

# Denominator clamp for the charge-off ratio. When zero outcomes
# observed without the flag firing, dividing produces an unbounded
# blow-up — the suggested weight stops being interpretable. We clamp
# to 0.001 (i.e. one charge-off per 1000 outcomes) which keeps the
# ratio finite while still signaling "this flag fires on almost every
# charge-off and almost never on clean deals."
_NOT_FIRED_RATE_FLOOR: Decimal = Decimal("0.001")


WeightConfidence = Literal["low", "medium", "high"]


class WeightDriftEntry(BaseModel):
    """One per ``FRAUD_WEIGHTS`` key with at least one observed outcome.

    Money / ratio fields are Decimal end-to-end. Pydantic serialization
    preserves Decimal under ``model_dump(mode='json')`` because we use
    ``model_config`` defaults — Decimal → str in JSON. The dossier
    template renders them with the ``whole_money`` / plain ``str`` filters.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    flag_code: str = Field(min_length=1)
    current_weight: Decimal
    fired_count: int = Field(ge=0)
    fired_charged_off_rate: Decimal
    not_fired_count: int = Field(ge=0)
    not_fired_charged_off_rate: Decimal
    charge_off_ratio: Decimal
    suggested_weight: Decimal
    sample_size: int = Field(ge=0)
    confidence: WeightConfidence


class WeightDriftReport(BaseModel):
    """The full report produced by ``compute_weight_drift``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    lookback_days: int = Field(ge=1)
    total_outcomes: int = Field(ge=0)
    entries: list[WeightDriftEntry] = Field(default_factory=list)


class SupabaseClient(Protocol):
    """Narrow Supabase-shape Protocol so tests can inject a fake client.

    Only ``table().select().execute()`` shape is consumed; we keep this
    intentionally permissive (returning ``Any``) because the supabase-py
    library does not ship typed responses.
    """

    # ``Any`` is intentional: supabase-py ships no typed response shape
    # for the table-builder chain, so the calibration engine consumes
    # ``table().select().execute()`` results behind a runtime cast and
    # cannot pin a static return type at this Protocol boundary.
    def table(self, name: str) -> Any: ...  # noqa: ANN401


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


async def compute_weight_drift(
    *,
    db: SupabaseClient | None = None,
    lookback_days: int = 180,
) -> WeightDriftReport:
    """Empirical weight-drift report keyed on ``FRAUD_WEIGHTS``.

    For every ``FRAUD_WEIGHTS`` bucket the function compares the
    charge-off rate of outcomes WHERE the bucket fired against outcomes
    WHERE it didn't, then multiplies the current weight by the ratio to
    produce a ``suggested_weight``. The suggestion is shadow-only — the
    caller surfaces it for operator review; ``FRAUD_WEIGHTS`` is never
    mutated from here.

    Args:
        db: Supabase client. ``None`` lazily resolves ``get_supabase()`` —
            production path. Tests inject a fake.
        lookback_days: How far back to scan ``deal_outcomes``. Default
            180 days matches the typical 6-month MCA term — enough deals
            have terminal outcomes inside this window without picking up
            outcomes that predate the live-decision cutover.

    Returns:
        A ``WeightDriftReport`` with one entry per ``FRAUD_WEIGHTS``
        key that has at least one observed outcome.

    Raises:
        ValueError: ``lookback_days`` < 1.
    """
    if lookback_days < 1:
        raise ValueError(f"lookback_days must be >= 1, got {lookback_days}")

    client = db if db is not None else get_supabase()
    rows = _load_outcome_rows(client, lookback_days=lookback_days)
    total_outcomes = len(rows)

    entries: list[WeightDriftEntry] = []
    for flag_code in FRAUD_WEIGHTS:
        entry = _entry_for_flag(flag_code=flag_code, rows=rows)
        if entry is not None:
            entries.append(entry)

    return WeightDriftReport(
        generated_at=datetime.now(UTC),
        lookback_days=lookback_days,
        total_outcomes=total_outcomes,
        entries=entries,
    )


def _entry_for_flag(
    *,
    flag_code: str,
    rows: list[dict[str, Any]],
) -> WeightDriftEntry | None:
    """Build one ``WeightDriftEntry`` for a single ``FRAUD_WEIGHTS`` key.

    Returns ``None`` when the sample is entirely empty (sample_size == 0)
    so the surface only shows flags with at least one observation.
    """
    current_weight = Decimal(str(FRAUD_WEIGHTS[flag_code])).quantize(Decimal("0.01"))

    fired_count = 0
    fired_charged_off = 0
    not_fired_count = 0
    not_fired_charged_off = 0

    for row in rows:
        outcome = row.get("outcome")
        if outcome not in TERMINAL_OUTCOMES:
            # Defensive — should already be filtered by the loader, but
            # the calibration math assumes only terminal outcomes.
            continue
        is_negative = outcome in NEGATIVE_OUTCOMES
        fired = _flag_fired(row, flag_code=flag_code)
        if fired:
            fired_count += 1
            if is_negative:
                fired_charged_off += 1
        else:
            not_fired_count += 1
            if is_negative:
                not_fired_charged_off += 1

    sample_size = fired_count + not_fired_count
    if sample_size == 0:
        return None

    fired_rate = _safe_rate(numerator=fired_charged_off, denominator=fired_count)
    not_fired_rate = _safe_rate(numerator=not_fired_charged_off, denominator=not_fired_count)

    # Clamp the denominator so a zero baseline (no charge-offs without
    # the flag firing) does not blow up the ratio to infinity. The clamp
    # value (0.001 ≈ one charge-off per 1000 outcomes) keeps the ratio
    # finite while still signaling "this flag fires on almost every
    # charge-off and almost never on clean deals."
    clamped_not_fired_rate = max(_NOT_FIRED_RATE_FLOOR, not_fired_rate)
    ratio = (fired_rate / clamped_not_fired_rate).quantize(Decimal("0.0001"))

    suggested = (current_weight * ratio).quantize(Decimal("0.0001"))

    return WeightDriftEntry(
        flag_code=flag_code,
        current_weight=current_weight,
        fired_count=fired_count,
        fired_charged_off_rate=fired_rate.quantize(Decimal("0.0001")),
        not_fired_count=not_fired_count,
        not_fired_charged_off_rate=not_fired_rate.quantize(Decimal("0.0001")),
        charge_off_ratio=ratio,
        suggested_weight=suggested,
        sample_size=sample_size,
        confidence=_confidence_band(sample_size),
    )


def _flag_fired(row: dict[str, Any], *, flag_code: str) -> bool:
    """Whether ``flag_code`` contributed to the decision on this outcome.

    Reads ``decisions.score_factors -> breakdown -> {flag_code}``.
    "Fired" means the breakdown entry exists AND is non-zero (truthy).
    Shadow keys (weight 0.0) are eligible — their breakdown entry, when
    populated by future scorer wiring, will be a non-zero contribution
    even though the weight is 0.

    A missing breakdown / missing key is treated as "did NOT fire". This
    is the conservative default — an unknown flag does not count as
    firing against the empirical comparison.
    """
    score_factors = row.get("score_factors")
    if not isinstance(score_factors, dict):
        return False
    breakdown = score_factors.get("breakdown")
    if not isinstance(breakdown, dict):
        return False
    value = breakdown.get(flag_code)
    if value is None:
        return False
    try:
        as_decimal = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return False
    return as_decimal != Decimal("0")


def _safe_rate(*, numerator: int, denominator: int) -> Decimal:
    """Charge-off rate for one cohort. Zero denominator → zero rate.

    Decimal end-to-end. Denominator clamping happens at the ratio
    computation site, not here.
    """
    if denominator == 0:
        return Decimal("0")
    return Decimal(numerator) / Decimal(denominator)


def _confidence_band(sample_size: int) -> WeightConfidence:
    """Map sample_size to a confidence band. Branchy by design — the
    thresholds need to be readable inline."""
    if sample_size < _LOW_SAMPLE_FLOOR:
        return "low"
    if sample_size >= _HIGH_SAMPLE_FLOOR:
        return "high"
    return "medium"


def _load_outcome_rows(
    client: SupabaseClient,
    *,
    lookback_days: int,
) -> list[dict[str, Any]]:
    """Load every ``deal_outcomes`` row in the lookback window with the
    decision's ``score_factors`` joined in.

    Filters out ``pending`` and ``paying`` rows in the query so the
    Python side only iterates terminal-outcome rows. The Supabase REST
    JOIN syntax (``decision_id(score_factors)``) follows the same
    pattern other repositories in this codebase use (see
    ``aegis.submissions.repository``).

    Parameterized via supabase-py builder methods — never string
    interpolation (CLAUDE.md "No string-interpolated SQL").
    """
    since_iso = _since_iso(lookback_days=lookback_days)
    try:
        result = (
            client.table("deal_outcomes")
            .select(
                "id,merchant_id,decision_id,submitted_at,funder_id,"
                "funder_decision,funded_amount,factor_rate,term_days,"
                "first_payment_date,outcome,outcome_recorded_at,"
                "charge_off_amount,created_at,"
                # Embedded FK fetch — supabase-py returns the related
                # row dict under ``decisions`` (table name, not column).
                "decisions(score_factors,decision_reason_codes,score)"
            )
            .gte("submitted_at", since_iso)
            .in_("outcome", list(TERMINAL_OUTCOMES))
            .execute()
        )
    except Exception as exc:  # pragma: no cover — defensive
        _log.warning(
            "weight_calibration.load_failed lookback_days=%d error=%s",
            lookback_days,
            type(exc).__name__,
        )
        return []
    data = result.data if result is not None else None
    if not data:
        return []
    return [_flatten_row(r) for r in data]


def _flatten_row(row: dict[str, Any]) -> dict[str, Any]:
    """Pull the embedded ``decisions`` join up to the row level.

    The Supabase embed returns ``{"decisions": {"score_factors": {...}, ...}}``.
    Down-stream callers (``_flag_fired``) read ``score_factors`` directly
    on the row, so we lift it.
    """
    embedded = row.get("decisions")
    if isinstance(embedded, dict):
        if "score_factors" in embedded:
            row["score_factors"] = embedded["score_factors"]
        if "decision_reason_codes" in embedded:
            row["decision_reason_codes"] = embedded["decision_reason_codes"]
        if "score" in embedded:
            row["decision_score"] = embedded["score"]
    return row


def _since_iso(*, lookback_days: int) -> str:
    """ISO-8601 timestamp ``lookback_days`` ago in UTC.

    Extracted so tests can patch the cutoff without monkeypatching
    datetime globally.
    """
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(days=lookback_days)
    return cutoff.isoformat()


__all__ = [
    "NEGATIVE_OUTCOMES",
    "TERMINAL_OUTCOMES",
    "SupabaseClient",
    "WeightConfidence",
    "WeightDriftEntry",
    "WeightDriftReport",
    "compute_weight_drift",
]
