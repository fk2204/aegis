"""Portfolio analytics (M11 / U11) — read-only aggregations over existing tables.

The operator dashboard at ``/ui/portfolio`` surfaces outcomes: approval
rate by funder, decisions by tier, decisions by state, recent activity,
and a best-effort fraud-catch rate. None of this is persisted — the
metrics are computed on demand from rows already living in
``merchants``, ``documents``, ``audit_log``, and ``funder_replies``.

Why a separate module
---------------------

``DealRepository`` is the read projection over (merchants x documents).
Portfolio metrics are a fan-out one level above that — they join across
funders, audit history, and reply outcomes that no single repo owns.
Putting the aggregations behind one pure ``compute_portfolio_metrics``
function lets the route handler stay thin and lets tests exercise the
math without spinning up FastAPI.

Constraints (M11 spec):

  * Read-only — no INSERTs / UPDATEs / DELETEs.
  * No new migrations / no new tables.
  * Stays within ``src/aegis/deals/portfolio_analytics.py``,
    ``src/aegis/web/router.py`` (route + Depends wiring only),
    ``src/aegis/web/templates/portfolio.html.j2``, and the route's
    tests.

Design notes:

  * Pipeline state is derived from ``MerchantRow.status`` (provisional
    / needs_manual_naming / finalized) joined to each merchant's most-
    recent ``DocumentRow.parse_status``. There is no ``deals`` table to
    read a per-deal lifecycle from (Phase 7 audit F1 locked that
    decision); the derived state is a faithful read of what AEGIS
    actually persists today.

  * Approval rate by funder reads ``funder_replies`` directly. The
    table's statuses are ``approved`` / ``declined`` / ``countered``
    (migration 021 CHECK constraint); "no-response" is computed as
    ``submissions_count - replies_count`` per funder. Submissions are
    surfaced via ``audit_log`` rows whose ``action`` =
    ``deal.submit_to_funders`` — the ``submissions`` table from
    migration 013 is DESIGN-ONLY (no Supabase backend yet — see
    ``src/aegis/submissions/repository.py``'s module docstring), so the
    audit row is the durable record.

  * Decisions by tier reads ``audit_log`` rows whose ``action`` =
    ``deal.score`` and extracts ``details.tier``. AEGIS does not
    persist a per-deal tier column — the tier is computed by
    ``score_deal`` each time scoring runs and surfaced into the audit
    row. The tier column on the ``decisions`` table tracks state CFDL
    tier (1/2/3), not AEGIS tier (A/B/C/D/F).

  * Decisions by state reads the same ``deal.score`` audit rows and
    joins on the merchant to read ``state``.

  * Fraud catch rate is computed off ``documents.fraud_score >= 70`` —
    matching the parser's hard-decline band (pipeline.py
    ``HARD_DECLINE_THRESHOLD = 65``, with a 70 cushion to surface only
    the unambiguous catches the operator is asking about).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aegis.funders.models import FunderRow
from aegis.merchants.models import MerchantRow
from aegis.storage import DocumentRow

# Maximum date-range span the route accepts. Bounds query cost — every
# downstream query (audit_log scan, funder_replies scan) is filtered to
# this window, so an unbounded ``from=2020-01-01`` cannot wedge the page.
MAX_WINDOW_DAYS: Final[int] = 365
DEFAULT_WINDOW_DAYS: Final[int] = 30

# Fraud score threshold for the "catch" rate. Picked deliberately above
# the parser's HARD_DECLINE_THRESHOLD (65) so the count surfaces only
# unambiguous hard-decline catches. Operator-facing copy ("fraud catch
# rate") would otherwise be confusing if review-band hits (35-64) were
# folded in. Keep in sync with the docstring on
# ``PortfolioMetrics.fraud_catch_rate_pct``.
FRAUD_CATCH_THRESHOLD: Final[int] = 70

# Derived pipeline states surfaced in the KPI strip. These map from
# (merchant.status, latest_doc.parse_status) onto a vocabulary the
# operator recognizes — "approved" / "declined" / "funded" don't exist
# as raw fields today, they're derived projections. See
# ``_derive_pipeline_state``.
PipelineState = Literal[
    "new",
    "in_review",
    "approved",
    "declined",
    "funded",
    "abandoned",
]

# Display order for the KPI strip. The strip iterates this tuple so
# template + accessor agree on ordering without hand-syncing.
PIPELINE_STATE_ORDER: Final[tuple[PipelineState, ...]] = (
    "new",
    "in_review",
    "approved",
    "declined",
    "funded",
    "abandoned",
)

# Tier display order — runs A (best) → F (worst). Same template-side
# discipline as PIPELINE_STATE_ORDER above.
TIER_ORDER: Final[tuple[str, ...]] = ("A", "B", "C", "D", "F")

# Cap for the "top states" panel. Avoids a 50-row table when the
# operator funds 8 states; the rest stack under an "all others" bucket
# if needed (currently truncated cleanly, not bucketed).
TOP_STATES_LIMIT: Final[int] = 10

# Cap for the recent-activity panel — last N scored deals.
RECENT_ACTIVITY_LIMIT: Final[int] = 20


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        frozen=True,
    )


class PipelineCounts(_StrictModel):
    """Count of merchants by derived pipeline state.

    Counts are non-overlapping — each merchant falls into exactly one
    state. The derivation lives in ``_derive_pipeline_state``.
    """

    new: int = Field(ge=0)
    in_review: int = Field(ge=0)
    approved: int = Field(ge=0)
    declined: int = Field(ge=0)
    funded: int = Field(ge=0)
    abandoned: int = Field(ge=0)

    @property
    def total(self) -> int:
        return (
            self.new
            + self.in_review
            + self.approved
            + self.declined
            + self.funded
            + self.abandoned
        )


class FunderApprovalRow(_StrictModel):
    """One row in the "approval rate by funder" panel.

    ``no_response`` is computed as ``max(submitted - approved - declined
    - countered, 0)`` — funders that received a submission but never
    replied. The clamp guards against a reply rate higher than the
    submission count, which can happen if a funder responded to a
    submission older than the date window (mismatched join).
    """

    funder_id: UUID
    funder_name: str
    submitted: int = Field(ge=0)
    approved: int = Field(ge=0)
    declined: int = Field(ge=0)
    countered: int = Field(ge=0)
    no_response: int = Field(ge=0)

    @property
    def approval_rate_pct(self) -> int | None:
        """Approved as a percentage of decided (approved + declined +
        countered) replies. ``None`` when zero decided replies — the
        template renders an em-dash rather than ``0%`` so the operator
        doesn't read "0% approval rate" on a funder that simply hasn't
        replied yet.
        """
        decided = self.approved + self.declined + self.countered
        if decided == 0:
            return None
        return round((self.approved / decided) * 100)


class TierCounts(_StrictModel):
    """Count of scored deals by AEGIS tier (A / B / C / D / F).

    Sourced from ``audit_log`` rows whose action is ``deal.score``. The
    same merchant may be scored multiple times — each scoring event
    counts once. This matches how the operator reads the metric
    ("how many A-tier scoring runs hit the desk this month").
    """

    # Tier letters are the public taxonomy (A is best, F is worst) —
    # uppercase single-letter attributes match the operator vocabulary.
    A: int = Field(ge=0)
    B: int = Field(ge=0)
    C: int = Field(ge=0)
    D: int = Field(ge=0)
    F: int = Field(ge=0)

    @property
    def total(self) -> int:
        return self.A + self.B + self.C + self.D + self.F

    def as_ordered(self) -> list[tuple[str, int]]:
        """Iterate (tier, count) in display order — used by the template."""
        mapping = {"A": self.A, "B": self.B, "C": self.C, "D": self.D, "F": self.F}
        return [(t, mapping[t]) for t in TIER_ORDER]


class StateCount(_StrictModel):
    """One row in the top-states panel."""

    state: str = Field(min_length=2, max_length=2)
    count: int = Field(gt=0)


class RecentDeal(_StrictModel):
    """One row in the recent-activity panel.

    ``merchant_id`` may be ``None`` if the audit row's subject_id can't
    be resolved to a merchant we still have on file (deletion / orphan
    row). The template renders these as plain text rather than a link.
    """

    merchant_id: UUID | None
    business_name: str
    tier: str | None
    recommendation: str | None
    scored_at: datetime | None


class PortfolioMetrics(_StrictModel):
    """Top-level shape returned by ``compute_portfolio_metrics``."""

    from_date: date
    to_date: date
    total_deals: int = Field(ge=0)

    # Top KPI strip.
    pipeline: PipelineCounts
    approval_rate_pct: int | None
    """Overall approved / decided ratio across all funders. ``None``
    when zero decided replies in the window — rendered as em-dash."""

    decline_rate_pct: int | None
    """Decline / decided ratio across all funders. Symmetric with
    ``approval_rate_pct``."""

    avg_tier: str | None
    """Median tier of scored deals in the window — pragmatic "average"
    on an ordinal scale (A=4..F=0). ``None`` when nothing was scored."""

    # Panels.
    funder_table: list[FunderApprovalRow]
    """One row per funder, sorted by submitted-count descending. Funders
    with zero submissions in the window are omitted — the operator
    doesn't need a wall of inactive funders."""

    tier_counts: TierCounts
    state_counts: list[StateCount]
    """Top-N states by deal count. ``N = TOP_STATES_LIMIT`` (10)."""

    recent_activity: list[RecentDeal]
    """Last N scored deals — most recent first. ``N =
    RECENT_ACTIVITY_LIMIT`` (20)."""

    # Best-effort fraud catch rate.
    fraud_catch_count: int = Field(ge=0)
    """Documents with fraud_score >= ``FRAUD_CATCH_THRESHOLD`` (70) in
    the window."""

    fraud_total_scored: int = Field(ge=0)
    """Documents with any fraud_score in the window — denominator."""

    fraud_catch_rate_pct: int | None
    """``fraud_catch_count / fraud_total_scored`` as a percentage,
    rounded. ``None`` when zero documents were scored in the window."""


# ---------------------------------------------------------------------------
# Date-range plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DateRange:
    """Validated, clamped date range used by the route + accessor."""

    from_date: date
    to_date: date

    def __post_init__(self) -> None:
        if self.to_date < self.from_date:
            raise ValueError(
                f"to_date {self.to_date} is earlier than from_date {self.from_date}"
            )


def resolve_date_range(
    from_str: str | None,
    to_str: str | None,
    *,
    today: date | None = None,
) -> DateRange:
    """Parse + clamp the ``?from=&to=`` query params.

    Defaults:
      * ``to`` defaults to today.
      * ``from`` defaults to ``to - 30 days``.
      * ``to - from`` is clamped to ``MAX_WINDOW_DAYS`` (365): if the
        operator passes a larger window we silently narrow ``from`` to
        ``to - MAX_WINDOW_DAYS``. The cap bounds query cost rather than
        rejecting the request; the template surfaces the actual rendered
        window to keep the operator oriented.

    Raises ``ValueError`` on malformed ISO strings or a reversed range
    (``to < from`` after parsing). The route handler catches and turns
    it into a 400.
    """
    as_of = today or datetime.now(UTC).date()
    parsed_to = _parse_iso_date(to_str) if to_str else as_of
    parsed_from = (
        _parse_iso_date(from_str)
        if from_str
        else parsed_to - timedelta(days=DEFAULT_WINDOW_DAYS)
    )

    if parsed_to < parsed_from:
        raise ValueError(
            f"to_date {parsed_to.isoformat()} is earlier than from_date "
            f"{parsed_from.isoformat()}"
        )

    # Clamp window. timedelta.days yields an int.
    span = (parsed_to - parsed_from).days
    if span > MAX_WINDOW_DAYS:
        parsed_from = parsed_to - timedelta(days=MAX_WINDOW_DAYS)
    return DateRange(from_date=parsed_from, to_date=parsed_to)


def _parse_iso_date(value: str) -> date:
    """Parse YYYY-MM-DD. ``date.fromisoformat`` accepts the same shape."""
    return date.fromisoformat(value.strip())


# ---------------------------------------------------------------------------
# Pure aggregation
# ---------------------------------------------------------------------------


def compute_portfolio_metrics(
    *,
    merchants: Iterable[MerchantRow],
    funders: Iterable[FunderRow],
    documents: Iterable[DocumentRow],
    funder_reply_rows: Iterable[dict[str, Any]],
    audit_rows: Iterable[dict[str, Any]],
    date_range: DateRange,
) -> PortfolioMetrics:
    """Pure aggregation. No I/O.

    ``audit_rows`` shape (per ``audit.AuditLog.list_recent``):
      ``{actor, action, subject_type, subject_id, details, created_at}``

    Only rows whose ``action`` starts with ``deal.`` are inspected:
      * ``deal.score`` — used for tier + state + recent activity.
      * ``deal.submit_to_funders`` — used for the per-funder
        submission counter.
      * ``deal.funded`` — used for the pipeline "funded" bucket.

    ``funder_reply_rows`` shape mirrors the ``funder_replies`` table
    (migration 021):
      ``{funder_id, deal_id, status, received_at, ...}``

    Both inputs are expected to already be filtered to the date window
    by the caller (the route handler narrows the supabase queries with
    ``.gte/.lte``). The function does not re-filter — it trusts the
    range, so a test fixture can feed in a known set without time
    plumbing.
    """
    merchants_list = list(merchants)
    funders_list = list(funders)
    documents_list = list(documents)
    replies_list = list(funder_reply_rows)
    audit_list = list(audit_rows)

    docs_by_merchant = _group_documents_by_merchant(documents_list)
    pipeline = _compute_pipeline_counts(merchants_list, docs_by_merchant, audit_list)

    funder_table = _compute_funder_table(funders_list, audit_list, replies_list)
    decided_total = sum(r.approved + r.declined + r.countered for r in funder_table)
    approved_total = sum(r.approved for r in funder_table)
    declined_total = sum(r.declined for r in funder_table)
    approval_rate_pct = (
        round((approved_total / decided_total) * 100) if decided_total > 0 else None
    )
    decline_rate_pct = (
        round((declined_total / decided_total) * 100) if decided_total > 0 else None
    )

    score_rows = _filter_score_audit_rows(audit_list)
    tier_counts = _compute_tier_counts(score_rows)
    avg_tier = _compute_avg_tier(tier_counts)

    merchants_by_id = {m.id: m for m in merchants_list}
    state_counts = _compute_state_counts(score_rows, merchants_by_id)
    recent_activity = _compute_recent_activity(score_rows, merchants_by_id)

    fraud_catch_count, fraud_total_scored = _compute_fraud_counts(documents_list)
    fraud_catch_rate_pct = (
        round((fraud_catch_count / fraud_total_scored) * 100)
        if fraud_total_scored > 0
        else None
    )

    return PortfolioMetrics(
        from_date=date_range.from_date,
        to_date=date_range.to_date,
        total_deals=tier_counts.total,
        pipeline=pipeline,
        approval_rate_pct=approval_rate_pct,
        decline_rate_pct=decline_rate_pct,
        avg_tier=avg_tier,
        funder_table=funder_table,
        tier_counts=tier_counts,
        state_counts=state_counts,
        recent_activity=recent_activity,
        fraud_catch_count=fraud_catch_count,
        fraud_total_scored=fraud_total_scored,
        fraud_catch_rate_pct=fraud_catch_rate_pct,
    )


# ---------------------------------------------------------------------------
# Pipeline state derivation
# ---------------------------------------------------------------------------


def _group_documents_by_merchant(
    documents: list[DocumentRow],
) -> dict[UUID, list[DocumentRow]]:
    """Bucket docs by merchant_id. Documents with ``merchant_id is None``
    are skipped — they're unassigned uploads, not part of any merchant's
    pipeline state.
    """
    grouped: dict[UUID, list[DocumentRow]] = {}
    for d in documents:
        if d.merchant_id is None:
            continue
        grouped.setdefault(d.merchant_id, []).append(d)
    # Sort each merchant's docs most-recent-first so callers can read
    # docs[0] without re-sorting.
    for mid in grouped:
        grouped[mid].sort(key=lambda doc: doc.uploaded_at, reverse=True)
    return grouped


def _funded_merchant_ids(audit_rows: list[dict[str, Any]]) -> set[UUID]:
    """Merchants with a ``deal.funded`` audit row in the window."""
    funded: set[UUID] = set()
    for r in audit_rows:
        if r.get("action") != "deal.funded":
            continue
        sid = _audit_subject_uuid(r)
        if sid is not None:
            funded.add(sid)
    return funded


def _submitted_merchant_ids(audit_rows: list[dict[str, Any]]) -> set[UUID]:
    """Merchants with a ``deal.submit_to_funders`` audit row in the window."""
    submitted: set[UUID] = set()
    for r in audit_rows:
        if r.get("action") != "deal.submit_to_funders":
            continue
        sid = _audit_subject_uuid(r)
        if sid is not None:
            submitted.add(sid)
    return submitted


def _audit_subject_uuid(row: dict[str, Any]) -> UUID | None:
    """Coerce ``subject_id`` to UUID. Rows with bad / missing ids are skipped."""
    sid = row.get("subject_id")
    if sid is None:
        return None
    if isinstance(sid, UUID):
        return sid
    if isinstance(sid, str):
        try:
            return UUID(sid)
        except ValueError:
            return None
    return None


def _derive_pipeline_state(
    merchant: MerchantRow,
    merchant_docs: list[DocumentRow],
    *,
    funded_ids: set[UUID],
    submitted_ids: set[UUID],
) -> PipelineState:
    """Map (merchant.status, latest doc.parse_status, audit-signal) → state.

    Order of precedence:
      1. Funded — audit has a ``deal.funded`` row.
      2. Approved — audit has a ``deal.submit_to_funders`` row (the
         broker shipped a packet; the funder hasn't said no yet).
      3. Declined — latest doc parse_status in {manual_review, error}.
      4. In review — latest doc parse_status in {pending, review}.
      5. New — no documents yet OR merchant is provisional and has
         only pending docs.
      6. Abandoned — non-finalized merchants that haven't picked up
         a document in 30 days (rough heuristic — better than a
         placeholder zero).

    The "abandoned" bucket is heuristic on purpose: there is no
    explicit lifecycle column for it. Surfacing the count gives the
    operator a number to act on; the underlying merchants are easy to
    audit from the existing list.
    """
    if merchant.id in funded_ids:
        return "funded"
    if merchant.id in submitted_ids:
        return "approved"

    if not merchant_docs:
        # No uploaded statement yet. Provisional / needs_manual_naming
        # rows in this branch are intake-only — likely abandoned if the
        # operator created them and never followed up.
        if (
            merchant.status in {"provisional", "needs_manual_naming"}
            and merchant.created_at is not None
            and (datetime.now(UTC) - merchant.created_at) > timedelta(days=30)
        ):
            return "abandoned"
        return "new"

    latest = merchant_docs[0]
    parse = latest.parse_status
    if parse in {"manual_review", "error"}:
        return "declined"
    if parse in {"pending"}:
        return "new"
    # proceed / review — the deal is live but no submission has gone
    # out yet.
    return "in_review"


def _compute_pipeline_counts(
    merchants: list[MerchantRow],
    docs_by_merchant: dict[UUID, list[DocumentRow]],
    audit_rows: list[dict[str, Any]],
) -> PipelineCounts:
    funded_ids = _funded_merchant_ids(audit_rows)
    submitted_ids = _submitted_merchant_ids(audit_rows)
    buckets: dict[PipelineState, int] = {s: 0 for s in PIPELINE_STATE_ORDER}
    for m in merchants:
        state = _derive_pipeline_state(
            m,
            docs_by_merchant.get(m.id, []),
            funded_ids=funded_ids,
            submitted_ids=submitted_ids,
        )
        buckets[state] += 1
    return PipelineCounts(
        new=buckets["new"],
        in_review=buckets["in_review"],
        approved=buckets["approved"],
        declined=buckets["declined"],
        funded=buckets["funded"],
        abandoned=buckets["abandoned"],
    )


# ---------------------------------------------------------------------------
# Funder approval table
# ---------------------------------------------------------------------------


def _compute_funder_table(
    funders: list[FunderRow],
    audit_rows: list[dict[str, Any]],
    reply_rows: list[dict[str, Any]],
) -> list[FunderApprovalRow]:
    """Build the per-funder submission + reply counters.

    Submissions: audit_log ``deal.submit_to_funders`` rows record a
    ``details.funder_ids`` list of UUID strings (per the existing
    submission flow in ``aegis.scoring.submission_package``). Each
    funder_id in the list counts as one submission to that funder.

    Replies: ``funder_replies.funder_id`` + ``status`` aggregated per
    funder. Sourced from migration 021.
    """
    funder_by_id = {f.id: f for f in funders}
    submitted_per_funder: dict[UUID, int] = {}
    for r in audit_rows:
        if r.get("action") != "deal.submit_to_funders":
            continue
        details = r.get("details")
        if not isinstance(details, dict):
            continue
        funder_ids_raw = details.get("funder_ids", [])
        if not isinstance(funder_ids_raw, list):
            continue
        for fid_str in funder_ids_raw:
            fid = _to_uuid(fid_str)
            if fid is None:
                continue
            submitted_per_funder[fid] = submitted_per_funder.get(fid, 0) + 1

    replies_per_funder: dict[UUID, dict[str, int]] = {}
    for r in reply_rows:
        fid = _to_uuid(r.get("funder_id"))
        status = r.get("status")
        if fid is None or status not in {"approved", "declined", "countered"}:
            continue
        bucket = replies_per_funder.setdefault(
            fid, {"approved": 0, "declined": 0, "countered": 0}
        )
        bucket[status] += 1

    rows: list[FunderApprovalRow] = []
    # Include every funder that has either a submission or a reply in
    # the window — funders with zero activity stay off the table to
    # keep it readable on a quiet month.
    active_funder_ids = set(submitted_per_funder) | set(replies_per_funder)
    for fid in active_funder_ids:
        funder = funder_by_id.get(fid)
        name = funder.name if funder is not None else f"funder {str(fid)[:8]}"
        submitted = submitted_per_funder.get(fid, 0)
        bucket = replies_per_funder.get(
            fid, {"approved": 0, "declined": 0, "countered": 0}
        )
        approved = bucket["approved"]
        declined = bucket["declined"]
        countered = bucket["countered"]
        no_response = max(submitted - approved - declined - countered, 0)
        rows.append(
            FunderApprovalRow(
                funder_id=fid,
                funder_name=name,
                submitted=submitted,
                approved=approved,
                declined=declined,
                countered=countered,
                no_response=no_response,
            )
        )
    rows.sort(key=lambda r: (-r.submitted, r.funder_name.lower()))
    return rows


def _to_uuid(value: object) -> UUID | None:
    """Coerce a possibly-string-or-UUID input to ``UUID | None``.

    The ``object`` type is intentional — supabase JSON rows arrive as
    ``Any`` shapes; this function exists so callers stay strongly
    typed downstream while the messy boundary is contained here.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Tier counts + state counts + recent activity (all from deal.score audit)
# ---------------------------------------------------------------------------


def _filter_score_audit_rows(audit_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return rows whose action is ``deal.score`` (the scoring event).

    Ordered most-recent first when ``created_at`` is present; rows
    without a timestamp keep their input order (InMemoryAuditLog
    doesn't stamp ``created_at`` — the caller's order is the order).
    """
    score_rows = [r for r in audit_rows if r.get("action") == "deal.score"]
    score_rows.sort(
        key=lambda r: (_audit_created_at(r) or datetime.min.replace(tzinfo=UTC)),
        reverse=True,
    )
    return score_rows


def _audit_created_at(row: dict[str, Any]) -> datetime | None:
    value = row.get("created_at")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _compute_tier_counts(score_rows: list[dict[str, Any]]) -> TierCounts:
    counts = {tier: 0 for tier in TIER_ORDER}
    for r in score_rows:
        details = r.get("details")
        if not isinstance(details, dict):
            continue
        tier = details.get("tier")
        if isinstance(tier, str) and tier in counts:
            counts[tier] += 1
    return TierCounts(
        A=counts["A"], B=counts["B"], C=counts["C"], D=counts["D"], F=counts["F"]
    )


# Numeric mapping for the median-tier calculation. Higher is better.
_TIER_SCORE: Final[dict[str, int]] = {"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}
_TIER_FROM_SCORE: Final[dict[int, str]] = {v: k for k, v in _TIER_SCORE.items()}


def _compute_avg_tier(tier_counts: TierCounts) -> str | None:
    """Median tier across the scored deals in the window.

    Median (not mean): the operator reads tiers as discrete bands, so
    "average B+" is meaningless. The 50th-percentile bucket maps
    cleanly back to a tier letter. ``None`` when zero deals scored.
    """
    flat: list[int] = []
    for tier in TIER_ORDER:
        flat.extend([_TIER_SCORE[tier]] * getattr(tier_counts, tier))
    if not flat:
        return None
    flat.sort()
    mid = len(flat) // 2
    median_value = flat[mid]
    return _TIER_FROM_SCORE.get(median_value)


def _compute_state_counts(
    score_rows: list[dict[str, Any]],
    merchants_by_id: dict[UUID, MerchantRow],
) -> list[StateCount]:
    counts: dict[str, int] = {}
    for r in score_rows:
        sid = _audit_subject_uuid(r)
        if sid is None:
            continue
        merchant = merchants_by_id.get(sid)
        if merchant is None or merchant.state is None:
            continue
        state = merchant.state.upper()
        counts[state] = counts.get(state, 0) + 1
    rows = [StateCount(state=s, count=c) for s, c in counts.items() if c > 0]
    rows.sort(key=lambda r: (-r.count, r.state))
    return rows[:TOP_STATES_LIMIT]


def _compute_recent_activity(
    score_rows: list[dict[str, Any]],
    merchants_by_id: dict[UUID, MerchantRow],
) -> list[RecentDeal]:
    out: list[RecentDeal] = []
    for r in score_rows[:RECENT_ACTIVITY_LIMIT]:
        sid = _audit_subject_uuid(r)
        merchant = merchants_by_id.get(sid) if sid is not None else None
        details = r.get("details")
        tier: str | None = None
        recommendation: str | None = None
        if isinstance(details, dict):
            tier_value = details.get("tier")
            if isinstance(tier_value, str):
                tier = tier_value
            rec_value = details.get("recommendation")
            if isinstance(rec_value, str):
                recommendation = rec_value
        out.append(
            RecentDeal(
                merchant_id=merchant.id if merchant is not None else None,
                business_name=(
                    merchant.business_name
                    if merchant is not None
                    else (f"merchant {str(sid)[:8]}" if sid is not None else "—")
                ),
                tier=tier,
                recommendation=recommendation,
                scored_at=_audit_created_at(r),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Fraud catch rate
# ---------------------------------------------------------------------------


def _compute_fraud_counts(documents: list[DocumentRow]) -> tuple[int, int]:
    """Return (catch_count, total_scored).

    A document with ``fraud_score is None`` (never scored — pending /
    error) is excluded from the denominator. Only scored docs count.
    """
    total = 0
    catches = 0
    for d in documents:
        if d.fraud_score is None:
            continue
        total += 1
        if d.fraud_score >= FRAUD_CATCH_THRESHOLD:
            catches += 1
    return catches, total


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "FRAUD_CATCH_THRESHOLD",
    "MAX_WINDOW_DAYS",
    "PIPELINE_STATE_ORDER",
    "RECENT_ACTIVITY_LIMIT",
    "TIER_ORDER",
    "TOP_STATES_LIMIT",
    "DateRange",
    "FunderApprovalRow",
    "PipelineCounts",
    "PipelineState",
    "PortfolioMetrics",
    "RecentDeal",
    "StateCount",
    "TierCounts",
    "compute_portfolio_metrics",
    "resolve_date_range",
]
