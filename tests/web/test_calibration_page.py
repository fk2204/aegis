"""Tests for ``GET /ui/calibration`` + ``POST /ui/calibration/{flag_code}/review``.

The page renders the WeightDriftReport surface; the review endpoint
writes to ``weight_calibration_log`` and MUST NOT mutate
``FRAUD_WEIGHTS``.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

import aegis.db as _db
from aegis.api.app import create_app
from aegis.api.deps import get_audit, reset_dependency_caches
from aegis.audit import InMemoryAuditLog
from aegis.parser.pipeline import FRAUD_WEIGHTS

# ---------------------------------------------------------------------------
# Fake Supabase
# ---------------------------------------------------------------------------


class _FakeExecuteResult:
    __slots__ = ("data",)

    def __init__(self, data: list[dict[str, Any]]) -> None:
        self.data = data


class _RecordingInsert:
    """Captures every insert and returns the latest payload from execute()."""

    def __init__(self) -> None:
        self.last: dict[str, Any] | None = None

    def insert(self, row: dict[str, Any]) -> _RecordingInsert:
        self.last = row
        return self

    def execute(self) -> _FakeExecuteResult:
        return _FakeExecuteResult([] if self.last is None else [self.last])


class _OutcomeQuery:
    """Returns an empty outcome list — the page test only needs the
    render path, not the empirical math (covered by the engine tests)."""

    def select(self, _cols: str) -> _OutcomeQuery:
        return self

    def gte(self, _col: str, _value: str) -> _OutcomeQuery:
        return self

    def in_(self, _col: str, _values: list[str]) -> _OutcomeQuery:
        return self

    def execute(self) -> _FakeExecuteResult:
        return _FakeExecuteResult([])


class _FakeSupabase:
    def __init__(self) -> None:
        self.calibration_log = _RecordingInsert()

    def table(self, name: str) -> Any:
        if name == "weight_calibration_log":
            return self.calibration_log
        if name == "deal_outcomes":
            return _OutcomeQuery()
        # Unknown table — return a no-op query.
        return _OutcomeQuery()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_supabase(monkeypatch: pytest.MonkeyPatch) -> _FakeSupabase:
    fake = _FakeSupabase()
    monkeypatch.setattr(_db, "get_supabase", lambda: fake)
    # The router and engine both did ``from aegis.db import get_supabase``
    # at import time, so the symbol resolves against the importing module's
    # namespace — patching ``aegis.db`` alone misses them. Patch each
    # consumer's local rebinding too.
    import aegis.scoring.weight_calibration as _cal_engine
    import aegis.web.routers.calibration as _cal_router

    monkeypatch.setattr(_cal_router, "get_supabase", lambda: fake)
    monkeypatch.setattr(_cal_engine, "get_supabase", lambda: fake)
    return fake


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    fake_supabase: _FakeSupabase,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    reset_dependency_caches()


# ---------------------------------------------------------------------------
# Page render
# ---------------------------------------------------------------------------


def test_calibration_page_renders(client: TestClient) -> None:
    """The page returns 200 even with no outcomes (empty-state branch)."""
    resp = client.get("/ui/calibration")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert 'data-test-id="calibration-page"' in body
    # Empty-state copy when there are no outcomes in the lookback window.
    assert 'data-test-id="calibration-empty"' in body


def test_calibration_review_writes_to_log_and_does_not_mutate_weights(
    client: TestClient,
    fake_supabase: _FakeSupabase,
    audit: InMemoryAuditLog,
) -> None:
    """POST /ui/calibration/{flag_code}/review writes to
    weight_calibration_log AND does NOT touch FRAUD_WEIGHTS."""
    flag = "patterns"
    weights_before = dict(FRAUD_WEIGHTS)

    resp = client.post(
        f"/ui/calibration/{flag}/review",
        data={
            "decision": "accepted",
            "suggested_weight": "0.30",
            "sample_size": "120",
            "confidence": "medium",
            "notes": "matches dossier review",
        },
    )
    assert resp.status_code == 200, resp.text
    assert 'data-test-id="calibration-recorded"' in resp.text
    assert "FRAUD_WEIGHTS code manually" in resp.text

    # Row landed.
    row = fake_supabase.calibration_log.last
    assert row is not None
    assert row["flag_code"] == flag
    assert row["operator_decision"] == "accepted"
    assert row["sample_size"] == 120
    assert row["confidence"] == "medium"
    assert Decimal(row["suggested_weight"]) == Decimal("0.30")

    # Audit row.
    matching = [e for e in audit.entries if e["action"] == "weight_calibration.reviewed"]
    assert len(matching) == 1
    assert matching[0]["details"]["flag_code"] == flag

    # FRAUD_WEIGHTS — UNCHANGED. The non-mutation contract is load-bearing.
    # Re-import the module to confirm no monkey-patch leaked in either
    # direction.
    pipeline_module = importlib.import_module("aegis.parser.pipeline")
    assert pipeline_module.FRAUD_WEIGHTS == weights_before


def test_calibration_review_invalid_decision_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/ui/calibration/patterns/review",
        data={
            "decision": "approved",  # wrong vocabulary (uses funder words)
            "suggested_weight": "0.30",
            "sample_size": "50",
            "confidence": "medium",
        },
    )
    assert resp.status_code == 400


def test_calibration_review_unknown_flag_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/ui/calibration/nonexistent_flag/review",
        data={
            "decision": "accepted",
            "suggested_weight": "0.30",
            "sample_size": "50",
            "confidence": "medium",
        },
    )
    assert resp.status_code == 404


def test_calibration_review_invalid_confidence_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/ui/calibration/patterns/review",
        data={
            "decision": "accepted",
            "suggested_weight": "0.30",
            "sample_size": "50",
            "confidence": "very_high",
        },
    )
    assert resp.status_code == 400
