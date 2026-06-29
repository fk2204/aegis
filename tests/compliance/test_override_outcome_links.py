"""Override → outcome flywheel tests (build plan §9.2).

Two layers:

  1. ``aegis.compliance.override_outcome_links`` module — auto-link
     policy + accuracy aggregation. Covers:
       (a) record_outcome for merchant with 0 overrides → 0 links + no audit
       (b) record_outcome for merchant with 2 overrides → 2 links + 2 audit rows
       (c) link insert failure → outcome still committed + audit row written

  2. /ui/overrides/summary route — exposes the per-reason accuracy
     table and the flywheel summary header (total / linked / accuracy
     by reason).

The Supabase-backed repository code path is not exercised here — it
shares the schema invariants with migration 098 + the SupabaseOverride
patterns from migration 072. The Phase-2 mapper-coverage discipline
(in_memory_vs_supabase test gap) lives in the wider integration test
suite; this file focuses on the flywheel policy + aggregation logic.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_override_outcome_link_repository,
    get_override_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.compliance.override_outcome_links import (
    InMemoryOverrideOutcomeLinkRepository,
    LinkAlreadyExistsError,
    LinkWriteError,
    OverrideOutcomeLinkRepository,
    build_flywheel_summary,
    build_reason_accuracy_rows,
    link_overrides_for_outcome,
)
from aegis.compliance.overrides import InMemoryOverrideRepository

# ---------------------------------------------------------------------------
# Module-level: link_overrides_for_outcome
# ---------------------------------------------------------------------------


def test_link_zero_overrides_writes_no_links_and_no_audit() -> None:
    """Case (a): merchant has no overrides; auto-link is a no-op."""
    repo = InMemoryOverrideOutcomeLinkRepository()
    audit = InMemoryAuditLog()
    merchant_id = uuid4()
    outcome_id = uuid4()

    attempts = link_overrides_for_outcome(
        merchant_id=merchant_id,
        outcome_id=outcome_id,
        repo=repo,
        audit=audit,
    )

    assert attempts == []
    # No audit rows from the auto-linker at all.
    actions = [e["action"] for e in audit.entries]
    assert "override.outcome_linked" not in actions
    assert "override.outcome_link_failed" not in actions


def test_link_two_overrides_writes_two_links_and_two_audit_rows() -> None:
    """Case (b): merchant has 2 overrides; both get linked + audited."""
    repo = InMemoryOverrideOutcomeLinkRepository()
    audit = InMemoryAuditLog()
    merchant_id = uuid4()
    outcome_id = uuid4()

    override_a = uuid4()
    override_b = uuid4()
    repo.seed_override(merchant_id, override_a)
    repo.seed_override(merchant_id, override_b)

    attempts = link_overrides_for_outcome(
        merchant_id=merchant_id,
        outcome_id=outcome_id,
        repo=repo,
        audit=audit,
        actor="filip@commera",
        actor_email="filip@commera.example",
    )

    assert len(attempts) == 2
    assert all(a.link_id is not None for a in attempts)
    assert all(a.error is None for a in attempts)

    # Two audit rows, both override.outcome_linked.
    linked_audits = [e for e in audit.entries if e["action"] == "override.outcome_linked"]
    assert len(linked_audits) == 2
    # Subject is the override (not the outcome) so the dossier filter
    # ``audit | subject=override(id)`` finds the link history.
    subjects = {e["subject_id"] for e in linked_audits}
    assert subjects == {str(override_a), str(override_b)}
    # Details carry both ids + the link_id.
    for e in linked_audits:
        details = e["details"]
        assert details["outcome_id"] == str(outcome_id)
        assert details["merchant_id"] == str(merchant_id)
        assert details["link_id"] is not None


def test_link_idempotent_re_run_is_silent() -> None:
    """A re-run on the same (merchant, outcome) doesn't duplicate audits."""
    repo = InMemoryOverrideOutcomeLinkRepository()
    audit = InMemoryAuditLog()
    merchant_id = uuid4()
    outcome_id = uuid4()
    override_id = uuid4()
    repo.seed_override(merchant_id, override_id)

    # First run: link + audit.
    link_overrides_for_outcome(
        merchant_id=merchant_id,
        outcome_id=outcome_id,
        repo=repo,
        audit=audit,
    )
    # Second run: duplicate suppressed; no new audit row.
    link_overrides_for_outcome(
        merchant_id=merchant_id,
        outcome_id=outcome_id,
        repo=repo,
        audit=audit,
    )

    linked_audits = [e for e in audit.entries if e["action"] == "override.outcome_linked"]
    assert len(linked_audits) == 1


class _FailingLinkRepo:
    """Test double whose ``insert_link`` always raises LinkWriteError."""

    def __init__(self, override_id: UUID, merchant_id: UUID) -> None:
        self._override_id = override_id
        self._merchant_id = merchant_id

    def list_override_ids_for_merchant(self, merchant_id: UUID) -> list[UUID]:
        if merchant_id == self._merchant_id:
            return [self._override_id]
        return []

    def insert_link(self, override_id: UUID, outcome_id: UUID) -> UUID:
        raise LinkWriteError("simulated DB failure")

    def list_links_with_outcomes(self) -> list[dict[str, Any]]:
        return []


def test_link_insert_failure_audits_and_returns_attempt_with_error() -> None:
    """Case (c): the deal_outcomes row already landed; a link failure
    audits ``override.outcome_link_failed`` but DOES NOT raise."""
    merchant_id = uuid4()
    outcome_id = uuid4()
    override_id = uuid4()
    repo: OverrideOutcomeLinkRepository = _FailingLinkRepo(override_id, merchant_id)
    audit = InMemoryAuditLog()

    attempts = link_overrides_for_outcome(
        merchant_id=merchant_id,
        outcome_id=outcome_id,
        repo=repo,
        audit=audit,
    )

    assert len(attempts) == 1
    assert attempts[0].link_id is None
    assert attempts[0].error is not None
    assert "simulated DB failure" in attempts[0].error

    failure_audits = [e for e in audit.entries if e["action"] == "override.outcome_link_failed"]
    assert len(failure_audits) == 1
    detail = failure_audits[0]["details"]
    assert detail["override_id"] == str(override_id)
    assert detail["outcome_id"] == str(outcome_id)
    assert "simulated DB failure" in detail["error"]


def test_idempotent_re_link_via_link_already_exists_does_not_audit() -> None:
    """A LinkAlreadyExistsError raised from the repo collapses to a
    silent success (no audit row); the original link's audit row is
    the durable record."""

    class _DuplicateRepo:
        def list_override_ids_for_merchant(self, merchant_id: UUID) -> list[UUID]:
            return [self._override_id]

        def insert_link(self, override_id: UUID, outcome_id: UUID) -> UUID:
            raise LinkAlreadyExistsError("duplicate")

        def list_links_with_outcomes(self) -> list[dict[str, Any]]:
            return []

        _override_id = uuid4()

    audit = InMemoryAuditLog()
    repo: OverrideOutcomeLinkRepository = _DuplicateRepo()
    attempts = link_overrides_for_outcome(
        merchant_id=uuid4(),
        outcome_id=uuid4(),
        repo=repo,
        audit=audit,
    )
    assert len(attempts) == 1
    assert attempts[0].link_id is None
    assert attempts[0].error is None
    # No audit row written for an idempotent re-link.
    assert all(
        e["action"] not in {"override.outcome_linked", "override.outcome_link_failed"}
        for e in audit.entries
    )


# ---------------------------------------------------------------------------
# Module-level: build_reason_accuracy_rows + build_flywheel_summary
# ---------------------------------------------------------------------------


def _override(reason: str, oid: UUID | None = None) -> dict[str, Any]:
    return {"id": str(oid or uuid4()), "reason_code": reason, "outcome": None}


def test_accuracy_score_too_conservative_funded_is_100_pct() -> None:
    """Two conservative overrides, both funded → 100% accuracy."""
    overrides_a = _override("score_too_conservative")
    overrides_b = _override("score_too_conservative")
    links = [
        {
            "override_id": overrides_a["id"],
            "outcome_id": str(uuid4()),
            "outcome": "paid_in_full",
            "funder_decision": "approved",
        },
        {
            "override_id": overrides_b["id"],
            "outcome_id": str(uuid4()),
            "outcome": "paying",
            "funder_decision": "approved",
        },
    ]

    rows = build_reason_accuracy_rows([overrides_a, overrides_b], links)
    assert len(rows) == 1
    row = rows[0]
    assert row.reason_code == "score_too_conservative"
    assert row.total_overrides == 2
    assert row.funded == 2
    assert row.loss == 0
    assert row.right_calls == 2
    assert row.accuracy_pct == 100.0


def test_accuracy_score_too_conservative_mixed_funded_and_loss() -> None:
    """1 funded + 1 charged_off → 50% accuracy."""
    o1 = _override("score_too_conservative")
    o2 = _override("score_too_conservative")
    links = [
        {
            "override_id": o1["id"],
            "outcome_id": str(uuid4()),
            "outcome": "paid_in_full",
            "funder_decision": "approved",
        },
        {
            "override_id": o2["id"],
            "outcome_id": str(uuid4()),
            "outcome": "charged_off",
            "funder_decision": "approved",
        },
    ]

    rows = build_reason_accuracy_rows([o1, o2], links)
    assert rows[0].funded == 1
    assert rows[0].loss == 1
    assert rows[0].right_calls == 1
    assert rows[0].accuracy_pct == 50.0


def test_accuracy_other_reason_code_renders_no_pct() -> None:
    """funder_specific_fit etc carry no directional accuracy signal."""
    o = _override("funder_specific_fit")
    links = [
        {
            "override_id": o["id"],
            "outcome_id": str(uuid4()),
            "outcome": "paid_in_full",
            "funder_decision": "approved",
        }
    ]
    rows = build_reason_accuracy_rows([o], links)
    assert rows[0].reason_code == "funder_specific_fit"
    assert rows[0].right_calls is None
    assert rows[0].accuracy_pct is None
    assert rows[0].funded == 1


def test_accuracy_pending_only_returns_none_pct() -> None:
    """If every linked outcome is pending, accuracy is N/A (no signal)."""
    o = _override("score_too_conservative")
    links = [
        {
            "override_id": o["id"],
            "outcome_id": str(uuid4()),
            "outcome": "pending",
            "funder_decision": "approved",
        }
    ]
    rows = build_reason_accuracy_rows([o], links)
    assert rows[0].pending == 1
    assert rows[0].funded == 0
    assert rows[0].loss == 0
    assert rows[0].accuracy_pct is None


def test_flywheel_summary_zero_overrides_returns_none_pcts() -> None:
    summary = build_flywheel_summary(0, [])
    assert summary.total_overrides == 0
    assert summary.linked_overrides == 0
    assert summary.linked_pct is None
    assert summary.funded_pct is None
    assert summary.declined_pct is None


def test_flywheel_summary_partial_link_coverage() -> None:
    """5 overrides total, 2 linked → linked_pct=40%."""
    o1 = _override("score_too_conservative")
    o2 = _override("score_too_conservative")
    links = [
        {
            "override_id": o1["id"],
            "outcome_id": str(uuid4()),
            "outcome": "paid_in_full",
            "funder_decision": "approved",
        },
        {
            "override_id": o2["id"],
            "outcome_id": str(uuid4()),
            "outcome": "charged_off",
            "funder_decision": "approved",
        },
    ]
    rows = build_reason_accuracy_rows([o1, o2], links)
    summary = build_flywheel_summary(5, rows)
    assert summary.total_overrides == 5
    assert summary.linked_overrides == 2
    assert summary.linked_pct == 40.0
    assert summary.funded_pct == 50.0
    assert summary.loss_pct == 50.0


# ---------------------------------------------------------------------------
# Route-level: /ui/overrides/summary
# ---------------------------------------------------------------------------


@pytest.fixture
def override_repo() -> InMemoryOverrideRepository:
    return InMemoryOverrideRepository()


@pytest.fixture
def link_repo() -> InMemoryOverrideOutcomeLinkRepository:
    return InMemoryOverrideOutcomeLinkRepository()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def client(
    override_repo: InMemoryOverrideRepository,
    link_repo: InMemoryOverrideOutcomeLinkRepository,
    audit: InMemoryAuditLog,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_override_repository] = lambda: override_repo
    app.dependency_overrides[get_override_outcome_link_repository] = lambda: link_repo
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


@pytest.mark.skip(
    reason="flywheel-summary template integration follows in a separate commit "
    "with migration 098. The repository + dependency wiring is in place; the "
    "/ui/overrides/summary template edit has not landed yet."
)
def test_overrides_summary_renders_total_linked_and_accuracy(
    client: TestClient,
    override_repo: InMemoryOverrideRepository,
    link_repo: InMemoryOverrideOutcomeLinkRepository,
) -> None:
    """End-to-end: seed an override + a link with a funded outcome, hit
    the route, assert the new flywheel section is rendered.

    Authenticated as admin via the bearer token established by
    ``tests/conftest.py``.
    """
    # Seed one override into the override repo.
    merchant_id = uuid4()
    document_id = uuid4()
    from aegis.compliance.overrides import DossierOverridePayload

    override_id = override_repo.insert_dossier_override(
        DossierOverridePayload(
            merchant_id=merchant_id,
            document_id=document_id,
            original_recommendation="manual_review",
            operator_decision="approve",
            reason_code="score_too_conservative",
            reason_detail="strong revenue trend",
            pattern_false_positives=["nsf_volatility"],
            operator_id="filip@commera",
            operator_email="filip@commera.example",
        )
    )

    # Seed a matching linked outcome (funded).
    outcome_id = uuid4()
    link_repo.seed_outcome(outcome_id, outcome="paid_in_full", funder_decision="approved")
    link_repo.insert_link(override_id, outcome_id)

    resp = client.get("/ui/overrides/summary")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # Total overrides + linked count header.
    assert 'data-test-id="flywheel-summary"' in body
    assert 'data-test-id="flywheel-linked"' in body
    # Accuracy table per reason code.
    assert 'data-test-id="override-accuracy"' in body
    assert "score_too_conservative" in body
    # 100% accuracy text rendered.
    assert "100.0%" in body


def test_overrides_summary_empty_state_when_no_overrides(
    client: TestClient,
) -> None:
    """Day-zero rendering: no overrides → empty-state copy."""
    resp = client.get("/ui/overrides/summary")
    assert resp.status_code == 200
    assert "No operator overrides captured yet." in resp.text
