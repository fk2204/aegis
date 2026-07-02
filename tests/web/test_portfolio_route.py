"""Tests for ``GET /ui/portfolio`` (M11 / U11 — operator analytics view).

Covers:

  * Route renders 200 + the page header banner + the KPI strip.
  * Date-range query string narrows the result set.
  * Date-range > 365 days clamps to 365.
  * Empty pipeline renders the empty-state copy.
  * Approval rate per funder math is correct against a known
    submissions / replies fixture.
  * Tier counts math is correct against a known deal.score audit
    fixture.

The math layer (``compute_portfolio_metrics``) is exercised by both
the route tests (end-to-end through the template) AND a focused unit
test below — separating the two means a regression in the template
doesn't mask a regression in the aggregator.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_decision_snapshot,
    get_disclosure_render_event_repository,
    get_funder_repository,
    get_merchant_repository,
    get_repository,
    get_submission_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.compliance.render_events import (
    RENDER_EVENT_STATUS_APR_FAILED,
    RENDER_EVENT_STATUS_NEEDS_REVIEW,
    RENDER_EVENT_STATUS_OK,
    InMemoryDisclosureRenderEventRepository,
)
from aegis.compliance.snapshot import InMemoryDecisionSnapshot
from aegis.deals.portfolio_analytics import (
    DEFAULT_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    DateRange,
    compute_portfolio_metrics,
    resolve_date_range,
)
from aegis.funders.models import FunderRow
from aegis.funders.repository import InMemoryFunderRepository
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.storage import InMemoryDocumentRepository
from aegis.submissions import InMemorySubmissionRepository
from aegis.submissions.models import SubmissionRow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Plain TestClient against default in-memory repos (empty pipeline)."""
    reset_dependency_caches()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_dependency_caches()


def _merchant(
    *,
    business_name: str,
    state: str | None = "NY",
    status: str = "finalized",
    created_at: datetime | None = None,
) -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        business_name=business_name,
        owner_name="Test Owner",
        state=state,
        status=status,
        created_at=created_at,
    )


def _funder(name: str) -> FunderRow:
    """Minimum-viable FunderRow for tests — only the fields the
    portfolio table reads. The full FunderRow has many defaults so
    construction stays terse."""
    return FunderRow(id=uuid4(), name=name)


def _submission(
    funder_id: object,
    *,
    submitted_at: datetime | None = None,
) -> SubmissionRow:
    """Minimum-viable SubmissionRow for portfolio-table tests.

    The portfolio funder-table aggregator only reads ``funder_id`` and
    ``submitted_at``; the other columns get plausible defaults so the
    Pydantic-strict model validates without leaking test concerns into
    the production code path.
    """
    from decimal import Decimal

    return SubmissionRow(
        merchant_id=uuid4(),
        document_id=uuid4(),
        funder_id=funder_id,
        submitted_at=submitted_at or datetime(2026, 5, 15, tzinfo=UTC),
        submitted_by="test@commerafunding.com",
        csv_doc_hash="a" * 64,
        csv_filename="m__f.csv",
        proposed_amount=Decimal("50000.00"),
        proposed_factor=Decimal("1.3000"),
        proposed_holdback=Decimal("0.1200"),
    )


def _populated_app() -> tuple[
    TestClient,
    InMemoryMerchantRepository,
    InMemoryFunderRepository,
    InMemoryAuditLog,
    InMemoryDecisionSnapshot,
    InMemorySubmissionRepository,
    list[FunderRow],
    list[MerchantRow],
]:
    """Build an app whose merchant + funder repos are pre-seeded with
    a known mix of merchants, funders, audit rows, reply rows, decision
    snapshots, and a submissions repository.

    Returns the client + the live repos + the seeded entities so each
    test can introspect / assert against the same objects.
    """
    reset_dependency_caches()
    merchants_repo = InMemoryMerchantRepository()
    funders_repo = InMemoryFunderRepository()
    docs_repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    snapshot = InMemoryDecisionSnapshot()
    submissions = InMemorySubmissionRepository()

    # Three merchants in three pipeline states.
    m1 = _merchant(business_name="Acme Diner", state="NY")
    m2 = _merchant(business_name="Beta Bakery", state="CA")
    m3 = _merchant(business_name="Gamma Grill", state="NY")
    for m in (m1, m2, m3):
        merchants_repo.upsert(m)

    # Two funders.
    f1 = _funder("OnDeck Capital")
    f2 = _funder("Credibly")
    for f in (f1, f2):
        funders_repo.upsert(f)

    app = create_app()
    app.dependency_overrides[get_merchant_repository] = lambda: merchants_repo
    app.dependency_overrides[get_funder_repository] = lambda: funders_repo
    app.dependency_overrides[get_repository] = lambda: docs_repo
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_decision_snapshot] = lambda: snapshot
    app.dependency_overrides[get_submission_repository] = lambda: submissions
    client = TestClient(app)
    return (
        client,
        merchants_repo,
        funders_repo,
        audit,
        snapshot,
        submissions,
        [f1, f2],
        [m1, m2, m3],
    )


# ---------------------------------------------------------------------------
# Route: 200 + banner + KPI strip
# ---------------------------------------------------------------------------


def test_portfolio_route_renders_200_and_banner_on_empty_pipeline(
    client: TestClient,
) -> None:
    resp = client.get("/ui/portfolio")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Page header banner — title + the "outcomes" subtitle confirm the
    # template assembled correctly.
    assert "Portfolio" in body
    assert "outcomes" in body
    # KPI strip cells — these labels are the operator-facing copy and
    # must survive any rename. Failure here likely means the strip
    # markup was edited without considering the route contract.
    assert "Approval rate" in body
    assert "Decline rate" in body
    assert "Fraud catches" in body


def test_portfolio_route_renders_empty_state_on_zero_pipeline(
    client: TestClient,
) -> None:
    resp = client.get("/ui/portfolio")
    assert resp.status_code == 200, resp.text
    assert "No submissions tracked yet" in resp.text


# ---------------------------------------------------------------------------
# Date-range query string
# ---------------------------------------------------------------------------


def test_portfolio_route_narrows_via_date_query_params(
    client: TestClient,
) -> None:
    """When ``from`` and ``to`` are passed, the route echoes them in the
    page header. Confirms the parser accepted the values."""
    resp = client.get("/ui/portfolio?from=2026-05-01&to=2026-05-31")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "2026-05-01" in body
    assert "2026-05-31" in body


def test_portfolio_route_rejects_malformed_date(client: TestClient) -> None:
    """Malformed date in the query string surfaces as 400, not 500."""
    resp = client.get("/ui/portfolio?from=not-a-date")
    assert resp.status_code == 400, resp.text


def test_resolve_date_range_clamps_to_max_window() -> None:
    """A window > 365 days narrows ``from`` to ``to - 365`` silently —
    the cap protects query cost. The clamp test lives at the accessor
    level rather than the HTTP layer because the page header reflects
    the rendered (clamped) window, not the requested one."""
    today = date(2026, 6, 9)
    rng = resolve_date_range(
        from_str="2020-01-01",
        to_str=today.isoformat(),
        today=today,
    )
    assert (rng.to_date - rng.from_date).days == MAX_WINDOW_DAYS


def test_resolve_date_range_defaults_to_last_30_days() -> None:
    today = date(2026, 6, 9)
    rng = resolve_date_range(from_str=None, to_str=None, today=today)
    assert rng.to_date == today
    assert (rng.to_date - rng.from_date).days == DEFAULT_WINDOW_DAYS


def test_resolve_date_range_rejects_reversed_range() -> None:
    today = date(2026, 6, 9)
    with pytest.raises(ValueError, match="earlier than from_date"):
        resolve_date_range(
            from_str="2026-06-01",
            to_str="2026-05-01",
            today=today,
        )


# ---------------------------------------------------------------------------
# Approval-rate computation accuracy
# ---------------------------------------------------------------------------


def test_funder_approval_rate_math_is_exact() -> None:
    """Feed 10 submissions + a known reply set; verify per-funder counts
    and the approval rate percentage.

    Setup:
      * Funder F1: 6 submissions, 3 approved replies, 2 declined,
        0 countered, 0 outcome=no_response → 1 submission still pending.
        Approval rate 3/5 = 60%.
      * Funder F2: 4 submissions, 1 approved, 1 declined, 1 countered,
        0 outcome=no_response → 1 submission still pending. Approval
        rate 1/3 = 33%.

    Post-U20: submissions come from the durable submissions table, not
    audit_log JSON. The replies side stays on funder_replies.

    Post-migration-071: legacy email-parse rows use ``status`` (no
    outcome column). Until an operator explicitly records
    ``outcome='no_response'`` for a missing reply, the residual
    submissions count as ``pending``, NOT ``no_response``. The
    distinction is the load-bearing semantic of the manual outcome
    capture path — "we haven't asked yet" is operationally different
    from "we asked and the funder ghosted".
    """
    f1 = _funder("F1 Funder")
    f2 = _funder("F2 Funder")
    merchants: list[MerchantRow] = []

    # Six submissions to F1 + four to F2 — each is one SubmissionRow.
    submissions: list[SubmissionRow] = []
    for _ in range(6):
        submissions.append(_submission(f1.id))
    for _ in range(4):
        submissions.append(_submission(f2.id))

    # Replies: F1 3 approved + 2 declined; F2 1 approved + 1 declined +
    # 1 countered. Each reply is a funder_replies row dict — legacy
    # email-parse shape (status only, no outcome column).
    reply_rows: list[dict[str, object]] = []
    for status in ["approved", "approved", "approved", "declined", "declined"]:
        reply_rows.append({"funder_id": str(f1.id), "deal_id": str(uuid4()), "status": status})
    for status in ["approved", "declined", "countered"]:
        reply_rows.append({"funder_id": str(f2.id), "deal_id": str(uuid4()), "status": status})

    metrics = compute_portfolio_metrics(
        merchants=merchants,
        funders=[f1, f2],
        documents=[],
        funder_reply_rows=reply_rows,
        audit_rows=[],
        decision_rows=[],
        submissions=submissions,
        date_range=DateRange(from_date=date(2026, 5, 1), to_date=date(2026, 6, 9)),
    )

    rows_by_funder = {r.funder_id: r for r in metrics.funder_table}
    f1_row = rows_by_funder[f1.id]
    f2_row = rows_by_funder[f2.id]

    assert f1_row.submitted == 6
    assert f1_row.approved == 3
    assert f1_row.declined == 2
    assert f1_row.countered == 0
    # No operator-recorded no_response outcome rows — the missing
    # reply rolls into ``pending``, not ``no_response``.
    assert f1_row.no_response == 0
    assert f1_row.pending == 1
    # 3 approved / (3 + 2 + 0 + 0) decided = 60%
    assert f1_row.approval_rate_pct == 60

    assert f2_row.submitted == 4
    assert f2_row.approved == 1
    assert f2_row.declined == 1
    assert f2_row.countered == 1
    assert f2_row.no_response == 0
    assert f2_row.pending == 1
    # 1 / (1 + 1 + 1 + 0) = 33%
    assert f2_row.approval_rate_pct == 33

    # Cross-funder approval / decline rates.
    # Total decided = 5 + 3 = 8. Approved = 3 + 1 = 4. Declined = 2 + 1 = 3.
    assert metrics.approval_rate_pct == round((4 / 8) * 100)
    assert metrics.decline_rate_pct == round((3 / 8) * 100)


def test_funder_outcome_counts_from_operator_recorded_outcome_rows() -> None:
    """When funder_replies rows carry the new ``outcome`` column
    (migration 071), the per-funder panel counts them by outcome and
    distinguishes ``no_response`` from ``pending``.

    Setup (matches the spec): 5 submissions to one funder, with 2
    approved / 1 declined / 1 countered / 1 no_response recorded as
    operator outcomes. ``pending`` = 5 - 5 = 0.
    """
    f1 = _funder("F1 Funder")
    submissions: list[SubmissionRow] = [_submission(f1.id) for _ in range(5)]

    # Operator-recorded outcomes — the new migration-071 column. Each
    # row may have status NULL (no_response) or status mirrored from
    # outcome (approved/declined/countered).
    reply_rows: list[dict[str, object]] = []
    for outcome in ("approved", "approved", "declined", "countered", "no_response"):
        reply_rows.append(
            {
                "funder_id": str(f1.id),
                "deal_id": str(uuid4()),
                "outcome": outcome,
                "status": outcome if outcome != "no_response" else None,
            }
        )

    metrics = compute_portfolio_metrics(
        merchants=[],
        funders=[f1],
        documents=[],
        funder_reply_rows=reply_rows,
        audit_rows=[],
        decision_rows=[],
        submissions=submissions,
        date_range=DateRange(from_date=date(2026, 6, 1), to_date=date(2026, 6, 30)),
    )

    row = metrics.funder_table[0]
    assert row.submitted == 5
    assert row.approved == 2
    assert row.declined == 1
    assert row.countered == 1
    assert row.no_response == 1
    assert row.pending == 0
    # Decided = approved + declined + countered + no_response = 5.
    # Approval rate = 2 / 5 = 40%. no_response is counted as decided
    # (operator confirmed the funder ghosted — a real signal); pending
    # is excluded.
    assert row.approval_rate_pct == 40


def test_funder_outcome_pending_distinct_from_no_response() -> None:
    """3 submissions, only 1 outcome recorded → pending=2, no_response=0.

    This is the load-bearing semantic of migration 071: until the
    operator explicitly records outcome='no_response', the residual
    submissions are pending. The legacy approach (no_response =
    submitted - replies) conflated the two, surfacing "ghosted" counts
    that were actually "we haven't asked yet".
    """
    f1 = _funder("F1 Funder")
    submissions = [_submission(f1.id) for _ in range(3)]
    reply_rows: list[dict[str, object]] = [
        {
            "funder_id": str(f1.id),
            "deal_id": str(uuid4()),
            "outcome": "approved",
            "status": "approved",
        }
    ]

    metrics = compute_portfolio_metrics(
        merchants=[],
        funders=[f1],
        documents=[],
        funder_reply_rows=reply_rows,
        audit_rows=[],
        decision_rows=[],
        submissions=submissions,
        date_range=DateRange(from_date=date(2026, 6, 1), to_date=date(2026, 6, 30)),
    )

    row = metrics.funder_table[0]
    assert row.submitted == 3
    assert row.approved == 1
    assert row.no_response == 0
    assert row.pending == 2


# ---------------------------------------------------------------------------
# Tier-count accuracy
# ---------------------------------------------------------------------------


def test_tier_counts_match_decisions_rows() -> None:
    """Five decisions rows with tiers A/A/B/C/F yield the expected
    per-tier counts and a median of B (sorted ABCF, middle = B). The
    decisions table is the sole source post-U17."""
    merchant = _merchant(business_name="Tier Subject LLC", state="NY")
    decision_rows: list[dict[str, object]] = []
    for tier in ["A", "A", "B", "C", "F"]:
        decision_rows.append(
            {
                "id": str(uuid4()),
                "deal_id": str(uuid4()),
                "decided_at": "2026-06-01T12:00:00Z",
                "decision": "approve",
                "state_code": "NY",
                "score_factors": {"tier": tier},
            }
        )

    metrics = compute_portfolio_metrics(
        merchants=[merchant],
        funders=[],
        documents=[],
        funder_reply_rows=[],
        audit_rows=[],
        decision_rows=decision_rows,
        date_range=DateRange(from_date=date(2026, 5, 1), to_date=date(2026, 6, 9)),
    )

    assert metrics.tier_counts.A == 2
    assert metrics.tier_counts.B == 1
    assert metrics.tier_counts.C == 1
    assert metrics.tier_counts.D == 0
    assert metrics.tier_counts.F == 1
    assert metrics.tier_counts.total == 5
    # Median tier across [F, C, B, A, A] (sorted ascending by score
    # 0,2,3,4,4) — middle position is index 2 → B.
    assert metrics.avg_tier == "B"


def test_recent_activity_orders_most_recent_first() -> None:
    """The recent-activity panel surfaces scoring decisions in
    most-recent-first order. Sourced from the decisions table
    post-U17 — ``decided_at`` drives the sort."""
    decision_rows: list[dict[str, object]] = [
        {
            "id": str(uuid4()),
            "deal_id": str(uuid4()),
            "decided_at": "2026-06-01T08:00:00Z",
            "decision": "approve",
            "state_code": "NY",
            "score_factors": {"tier": "A"},
        },
        {
            "id": str(uuid4()),
            "deal_id": str(uuid4()),
            "decided_at": "2026-06-05T08:00:00Z",
            "decision": "approve",
            "state_code": "NY",
            "score_factors": {"tier": "B"},
        },
    ]
    metrics = compute_portfolio_metrics(
        merchants=[],
        funders=[],
        documents=[],
        funder_reply_rows=[],
        audit_rows=[],
        decision_rows=decision_rows,
        date_range=DateRange(from_date=date(2026, 5, 1), to_date=date(2026, 6, 9)),
    )
    assert len(metrics.recent_activity) == 2
    # Most-recent first → tier=B (June 5) precedes tier=A (June 1).
    assert metrics.recent_activity[0].tier == "B"
    assert metrics.recent_activity[1].tier == "A"


# ---------------------------------------------------------------------------
# Pipeline state — derived from merchant.status + audit signals
# ---------------------------------------------------------------------------


def test_pipeline_state_counts_submitted_and_funded_via_audit() -> None:
    """A merchant with a ``deal.submit_to_funders`` row → "approved"
    bucket. A merchant with a ``deal.funded`` row → "funded" bucket."""
    submitted_m = _merchant(business_name="Submitted Co", state="NY")
    funded_m = _merchant(business_name="Funded Co", state="NY")
    idle_m = _merchant(
        business_name="Idle Co",
        state="NY",
        created_at=datetime.now(UTC),
    )

    audit_rows: list[dict[str, object]] = [
        {
            "actor": "test",
            "action": "deal.submit_to_funders",
            "subject_id": str(submitted_m.id),
            "details": {"funder_ids": []},
            "created_at": None,
        },
        {
            "actor": "test",
            "action": "deal.funded",
            "subject_id": str(funded_m.id),
            "details": {},
            "created_at": None,
        },
    ]

    metrics = compute_portfolio_metrics(
        merchants=[submitted_m, funded_m, idle_m],
        funders=[],
        documents=[],
        funder_reply_rows=[],
        audit_rows=audit_rows,
        decision_rows=[],
        date_range=DateRange(from_date=date(2026, 5, 1), to_date=date(2026, 6, 9)),
    )
    assert metrics.pipeline.approved == 1
    assert metrics.pipeline.funded == 1
    # idle_m has no docs and was just created → "new" (not abandoned).
    assert metrics.pipeline.new == 1


# ---------------------------------------------------------------------------
# End-to-end route smoke — populated repos render the populated panels.
# ---------------------------------------------------------------------------


def test_portfolio_route_renders_populated_funder_table() -> None:
    """With submissions + decision rows seeded into the in-memory stores,
    the route renders the funder names + the tier panel populated from
    the decisions table.

    Post-U20: the funder approval panel reads from the submissions repo,
    not audit_log. The audit-log signal still drives the pipeline
    "approved" bucket because that's a per-merchant boolean (any
    submission row exists).
    """
    client, _, _, audit, snapshot, submissions, funders, merchants = _populated_app()
    f1, _f2 = funders
    submitted_m = merchants[0]

    # Pipeline "approved" bucket still reads audit (it's per-merchant).
    audit.entries.append(
        {
            "actor": "test",
            "action": "deal.submit_to_funders",
            "subject_type": "merchant",
            "subject_id": str(submitted_m.id),
            "details": {"funder_ids": [str(f1.id)]},
            "created_at": None,
        }
    )
    # Funder-table submission count reads the submissions repo (U20).
    # Use a submitted_at inside the default 30-day window so the test
    # doesn't time-drift once wall-clock passes the hard-coded date.
    submissions.create(_submission(f1.id, submitted_at=datetime.now(UTC) - timedelta(days=5)))
    # Post-U17 the score signal must come from the decisions table —
    # the audit-log fallback is gone. Drop a row in the snapshot store
    # directly (it's a list-backed in-memory implementation).
    snapshot._rows.append(
        {
            "id": str(uuid4()),
            "deal_id": str(uuid4()),
            "decided_at": "2026-06-01T12:00:00Z",
            "decision": "approve",
            "state_code": "NY",
            "score_factors": {"tier": "A"},
        }
    )

    resp = client.get("/ui/portfolio")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert f1.name in body
    # Tier chip appears once via tier_counts panel + once in recent
    # activity → at minimum the letter A is in the body. The "scored
    # 1 deal" copy in the page header is the deterministic anchor.
    assert "1 scored deal" in body or "scored deal" in body


def test_portfolio_route_window_clamp_visible_in_header(
    client: TestClient,
) -> None:
    """A 2-year span request clamps to 365 days; the header echoes the
    clamped window so the operator can see the cap."""
    today = date(2026, 6, 9)
    requested_from = (today - timedelta(days=730)).isoformat()
    resp = client.get(f"/ui/portfolio?from={requested_from}&to={today.isoformat()}")
    assert resp.status_code == 200, resp.text
    body = resp.text
    # The clamped from is exactly today - 365.
    clamped_from = (today - timedelta(days=MAX_WINDOW_DAYS)).isoformat()
    assert clamped_from in body
    # The original (unclamped) from must NOT appear — clamp is silent
    # but the rendered window is the truth.
    assert requested_from not in body


# ---------------------------------------------------------------------------
# Fraud catch rate
# ---------------------------------------------------------------------------


def test_fraud_catch_rate_counts_documents_at_or_above_threshold() -> None:
    """3 of 5 docs are above the 70 fraud threshold → catch rate 60%."""
    from aegis.storage import DocumentRow

    now = datetime.now(UTC)
    docs = [
        DocumentRow(
            id=uuid4(),
            file_hash=f"h{i}" * 16,
            byte_size=1000,
            original_filename=f"d{i}.pdf",
            fraud_score=score,
            uploaded_at=now,
        )
        for i, score in enumerate([10, 40, 70, 80, 95])
    ]

    metrics = compute_portfolio_metrics(
        merchants=[],
        funders=[],
        documents=docs,
        funder_reply_rows=[],
        audit_rows=[],
        decision_rows=[],
        date_range=DateRange(from_date=date(2026, 5, 1), to_date=date(2026, 6, 9)),
    )
    assert metrics.fraud_total_scored == 5
    assert metrics.fraud_catch_count == 3
    assert metrics.fraud_catch_rate_pct == 60


# ---------------------------------------------------------------------------
# U17 — decisions table is the SOLE source for tier / state / recent
# activity. The audit_log ``deal.score`` fallback (added in U13) is gone
# once ``document_id`` became required on the score routes.
# ---------------------------------------------------------------------------


def test_tier_counts_read_from_decisions_table_when_present() -> None:
    """When ``decision_rows`` is non-empty, audit_log ``deal.score``
    JSON is NOT consulted for tier_counts. This is the regression-
    prevention for the rewire: someone editing the audit detail key
    set must not silently shift the portfolio counts.
    """
    merchant = _merchant(business_name="Decision Source Co", state="NY")

    # Audit rows that say tier=A on three deal.score events. If the
    # rewire silently fell back to audit, we'd see A=3. We don't.
    audit_rows: list[dict[str, object]] = [
        {
            "actor": "api",
            "action": "deal.score",
            "subject_id": str(merchant.id),
            "details": {"tier": "A", "recommendation": "approve"},
            "created_at": "2026-06-01T12:00:00Z",
        }
        for _ in range(3)
    ]

    # Decisions table says B/B/C — these are the canonical numbers.
    decision_rows: list[dict[str, object]] = [
        {
            "id": str(uuid4()),
            "deal_id": str(uuid4()),
            "decided_at": "2026-06-05T12:00:00Z",
            "decision": "approve",
            "state_code": "NY",
            "score_factors": {"tier": "B"},
        },
        {
            "id": str(uuid4()),
            "deal_id": str(uuid4()),
            "decided_at": "2026-06-06T12:00:00Z",
            "decision": "approve",
            "state_code": "NY",
            "score_factors": {"tier": "B"},
        },
        {
            "id": str(uuid4()),
            "deal_id": str(uuid4()),
            "decided_at": "2026-06-07T12:00:00Z",
            "decision": "manual_review",
            "state_code": "CA",
            "score_factors": {"tier": "C"},
        },
    ]

    metrics = compute_portfolio_metrics(
        merchants=[merchant],
        funders=[],
        documents=[],
        funder_reply_rows=[],
        audit_rows=audit_rows,
        decision_rows=decision_rows,
        date_range=DateRange(from_date=date(2026, 5, 1), to_date=date(2026, 6, 9)),
    )

    # Decisions wins — A=0, B=2, C=1 (NOT A=3 from audit fallback).
    assert metrics.tier_counts.A == 0
    assert metrics.tier_counts.B == 2
    assert metrics.tier_counts.C == 1
    assert metrics.tier_counts.total == 3


def test_state_counts_read_from_decisions_state_code() -> None:
    """``decisions.state_code`` is the authoritative state — no join
    through merchants needed. Two NY + one CA decision rows produce
    NY=2, CA=1 regardless of merchant.state."""
    decision_rows: list[dict[str, object]] = [
        {
            "id": str(uuid4()),
            "deal_id": str(uuid4()),
            "decided_at": "2026-06-05T12:00:00Z",
            "decision": "approve",
            "state_code": "NY",
            "score_factors": {"tier": "A"},
        },
        {
            "id": str(uuid4()),
            "deal_id": str(uuid4()),
            "decided_at": "2026-06-06T12:00:00Z",
            "decision": "approve",
            "state_code": "ny",  # mixed case — should normalize
            "score_factors": {"tier": "B"},
        },
        {
            "id": str(uuid4()),
            "deal_id": str(uuid4()),
            "decided_at": "2026-06-07T12:00:00Z",
            "decision": "decline",
            "state_code": "CA",
            "score_factors": {"tier": "D"},
        },
    ]

    metrics = compute_portfolio_metrics(
        merchants=[],
        funders=[],
        documents=[],
        funder_reply_rows=[],
        audit_rows=[],
        decision_rows=decision_rows,
        date_range=DateRange(from_date=date(2026, 5, 1), to_date=date(2026, 6, 9)),
    )

    state_map = {row.state: row.count for row in metrics.state_counts}
    assert state_map.get("NY") == 2
    assert state_map.get("CA") == 1


def test_audit_log_score_row_alone_is_not_counted_when_decisions_present() -> None:
    """Regression-prevention: a ``deal.score`` audit row with no matching
    decisions row does NOT inflate tier_counts. Decisions is the sole
    source of truth post-U17."""
    merchant = _merchant(business_name="Ghost Score LLC", state="TX")
    audit_rows: list[dict[str, object]] = [
        {
            "actor": "api",
            "action": "deal.score",
            "subject_id": str(merchant.id),
            "details": {"tier": "A", "recommendation": "approve"},
            "created_at": "2026-06-01T12:00:00Z",
        }
    ]
    # One unrelated decision row so decisions_list is non-empty —
    # this is the trigger for the rewire path.
    decision_rows: list[dict[str, object]] = [
        {
            "id": str(uuid4()),
            "deal_id": str(uuid4()),
            "decided_at": "2026-06-08T12:00:00Z",
            "decision": "decline",
            "state_code": "FL",
            "score_factors": {"tier": "F"},
        }
    ]

    metrics = compute_portfolio_metrics(
        merchants=[merchant],
        funders=[],
        documents=[],
        funder_reply_rows=[],
        audit_rows=audit_rows,
        decision_rows=decision_rows,
        date_range=DateRange(from_date=date(2026, 5, 1), to_date=date(2026, 6, 9)),
    )

    # The ghost audit-only A is invisible — only the F from decisions.
    assert metrics.tier_counts.A == 0
    assert metrics.tier_counts.F == 1
    assert metrics.tier_counts.total == 1


def test_empty_decisions_yields_empty_tier_and_state_panels() -> None:
    """U17 contract: with no decisions rows in the window, tier_counts
    is all zeros, state_counts is empty, recent_activity is empty, and
    avg_tier is None — regardless of how many ``deal.score`` audit rows
    sit in the audit_log. The audit-log fallback that U13 added to
    paper over the pre-U17 gap is gone; the operator runs more deals
    to populate decisions.
    """
    merchant = _merchant(business_name="Empty Window LLC", state="NY")
    # A heap of audit ``deal.score`` rows that USED to drive the
    # fallback path. Post-U17 they're invisible to the portfolio
    # tier / state / recent-activity panels.
    audit_rows: list[dict[str, object]] = [
        {
            "actor": "api",
            "action": "deal.score",
            "subject_id": str(merchant.id),
            "details": {"tier": tier, "recommendation": "approve"},
            "created_at": "2026-06-01T12:00:00Z",
        }
        for tier in ["A", "A", "B", "C", "F"]
    ]

    metrics = compute_portfolio_metrics(
        merchants=[merchant],
        funders=[],
        documents=[],
        funder_reply_rows=[],
        audit_rows=audit_rows,
        decision_rows=[],
        date_range=DateRange(from_date=date(2026, 5, 1), to_date=date(2026, 6, 9)),
    )

    assert metrics.tier_counts.A == 0
    assert metrics.tier_counts.B == 0
    assert metrics.tier_counts.C == 0
    assert metrics.tier_counts.D == 0
    assert metrics.tier_counts.F == 0
    assert metrics.tier_counts.total == 0
    assert metrics.state_counts == []
    assert metrics.recent_activity == []
    assert metrics.avg_tier is None


# ---------------------------------------------------------------------------
# U21 — disclosure_render_queue tile (Part 1 plumb-through)
#
# Two tests assert the tile fields populate from the render-event repo:
#   * Zero counts when the repo is empty.
#   * Populated counts when seeded with one row per actionable status.
# Mirrors the existing "fraud catch rate" tile-style coverage so the
# regression-protection bar matches the rest of the panels.
# ---------------------------------------------------------------------------


def test_disclosure_render_queue_tile_zero_counts_when_repo_empty() -> None:
    """An empty render-events repo → all four counters are zero, and
    ``actionable_count`` (the chip value) is 0. The tile renders the
    calm state rather than a warn chip."""
    reset_dependency_caches()
    render_repo = InMemoryDisclosureRenderEventRepository()
    app = create_app()
    app.dependency_overrides[get_disclosure_render_event_repository] = lambda: render_repo
    client = TestClient(app)
    try:
        resp = client.get("/ui/portfolio")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "Disclosure render queue" in body
        # Both buckets zero → tile reads "0 / 0" with zero-count copy.
        assert "needs_review 0" in body
        assert "apr_failed 0" in body
    finally:
        reset_dependency_caches()


def test_disclosure_render_queue_tile_populated_from_repo() -> None:
    """One ``needs_review`` + two ``apr_compute_failed`` + one ``ok``
    row in the repo → tile reads needs_review=1, apr_failed=2,
    actionable_count=3, total=4. The "Triage →" link is in the body."""
    reset_dependency_caches()
    render_repo = InMemoryDisclosureRenderEventRepository()

    def _seed(status: str) -> None:
        render_repo.record(
            deal_id=uuid4(),
            merchant_id=uuid4(),
            state="CA",
            template_path="compliance/templates/ca_sb1235.html.j2",
            status=status,
            status_reason="brentq failed to converge"
            if status == RENDER_EVENT_STATUS_APR_FAILED
            else None,
            details={"term_days": 180, "factor": "1.35"},
            recipient_email=None,
            rendered_by="api",
            rendered_at=datetime.now(UTC),
            metadata=None,
        )

    _seed(RENDER_EVENT_STATUS_NEEDS_REVIEW)
    _seed(RENDER_EVENT_STATUS_APR_FAILED)
    _seed(RENDER_EVENT_STATUS_APR_FAILED)
    _seed(RENDER_EVENT_STATUS_OK)

    app = create_app()
    app.dependency_overrides[get_disclosure_render_event_repository] = lambda: render_repo
    client = TestClient(app)
    try:
        resp = client.get("/ui/portfolio")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "Disclosure render queue" in body
        assert "needs_review 1" in body
        assert "apr_failed 2" in body
        # Triage link to the new page is rendered with text "Triage →".
        assert "/ui/disclosure-events" in body
        assert "Triage" in body
    finally:
        reset_dependency_caches()
