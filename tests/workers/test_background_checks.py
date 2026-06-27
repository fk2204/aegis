"""Unit tests for ``aegis.workers.run_background_checks``.

The arq job orchestrates two Bedrock-backed sweeps (UCC / previous-default
+ web-presence reputation) on a fresh merchant. The enqueue path is
already covered via the Close-webhook + intake tests; this file covers
the **worker function itself** — idempotency short-circuit, missing /
invalid merchant early-exits, partial-failure aggregation, and the audit
contract.

Wiring strategy:

* ``InMemoryMerchantRepository`` + ``InMemoryAuditLog`` are real (no
  Supabase). The worker reads ``ctx["merchants"]`` / ``ctx["audit"]`` so
  injection is just the test ctx dict.
* The refresh helpers (``aegis.business_intel.refresh.refresh_ucc_for_merchant``
  and ``aegis.web_presence.refresh.refresh_web_presence_for_merchant``) are
  imported lazily inside ``run_background_checks`` — monkeypatch on the
  source modules takes effect on the next call. No real Bedrock traffic
  reaches the wire.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

import aegis.business_intel.refresh as _ucc_refresh_mod
import aegis.web_presence.refresh as _wp_refresh_mod
from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.workers import run_background_checks

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def ctx(audit: InMemoryAuditLog, merchants: InMemoryMerchantRepository) -> dict[str, object]:
    return {"audit": audit, "merchants": merchants}


def _seed_merchant(
    repo: InMemoryMerchantRepository,
    *,
    ucc_checked_at: datetime | None = None,
    web_presence_scanned_at: datetime | None = None,
) -> MerchantRow:
    row = MerchantRow(
        id=uuid4(),
        business_name="Acme Industries LLC",
        state="CA",
        owner_name="A. Operator",
        ucc_checked_at=ucc_checked_at,
        web_presence_scanned_at=web_presence_scanned_at,
    )
    repo.upsert(row)
    return row


def _patch_refreshers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ucc_fn: Callable[..., object] | None = None,
    wp_fn: Callable[..., object] | None = None,
) -> tuple[list[UUID], list[UUID]]:
    """Replace both refresh helpers and record the merchant_id of each call.

    Callers pass ``ucc_fn`` / ``wp_fn`` to override the no-op behavior
    (e.g. raise to exercise the failure branch). Lists are returned so
    assertions can prove the refresher was (or wasn't) invoked without
    poking at internal state.
    """

    ucc_calls: list[UUID] = []
    wp_calls: list[UUID] = []

    def _noop_ucc(merchant_id: UUID, *, merchants_repo: object, audit: object) -> None:
        ucc_calls.append(merchant_id)

    def _noop_wp(merchant_id: UUID, *, merchants_repo: object, audit: object) -> None:
        wp_calls.append(merchant_id)

    monkeypatch.setattr(_ucc_refresh_mod, "refresh_ucc_for_merchant", ucc_fn or _noop_ucc)
    monkeypatch.setattr(_wp_refresh_mod, "refresh_web_presence_for_merchant", wp_fn or _noop_wp)
    return ucc_calls, wp_calls


# ---------------------------------------------------------------------------
# 1. Idempotency skip — both checks already populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_skip_when_both_checks_already_populated(
    ctx: dict[str, object],
    merchants: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merchant already has UCC + web-presence timestamps → worker writes
    a single ``merchant.background_checks_complete`` row with
    ``skipped=True`` + ``reason=already_populated`` and does NOT invoke
    either refresher."""
    now = datetime.now(UTC)
    merchant = _seed_merchant(merchants, ucc_checked_at=now, web_presence_scanned_at=now)
    ucc_calls, wp_calls = _patch_refreshers(monkeypatch)

    result = await run_background_checks(ctx, str(merchant.id), "close_webhook")

    assert result == {
        "merchant_id": str(merchant.id),
        "trigger": "close_webhook",
        "skipped": True,
    }
    assert ucc_calls == []
    assert wp_calls == []

    rows = [e for e in audit.entries if e["action"].startswith("merchant.background_checks")]
    assert len(rows) == 1
    row = rows[0]
    assert row["action"] == "merchant.background_checks_complete"
    assert row["details"]["skipped"] is True
    assert row["details"]["reason"] == "already_populated"
    assert row["details"]["failed_checks"] == []
    assert row["details"]["trigger"] == "close_webhook"


# ---------------------------------------------------------------------------
# 2. Partial failure aggregation — UCC succeeds, web-presence raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_failure_aggregates_failed_checks_without_crashing(
    ctx: dict[str, object],
    merchants: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UCC sweep succeeds, web-presence raises → worker continues, returns
    ``skipped=False`` + ``failed_checks=['web_presence']``, writes
    started + complete audit rows, NO exception escapes."""
    merchant = _seed_merchant(merchants)

    def _wp_boom(merchant_id: UUID, *, merchants_repo: object, audit: object) -> None:
        raise RuntimeError("bedrock 500")

    ucc_calls, wp_calls = _patch_refreshers(monkeypatch, wp_fn=_wp_boom)

    result = await run_background_checks(ctx, str(merchant.id), "intake")

    assert result["skipped"] is False
    assert result["failed_checks"] == ["web_presence"]
    assert ucc_calls == [merchant.id]  # UCC ran
    assert wp_calls == []  # WP raised before list-append

    actions = [e["action"] for e in audit.entries]
    assert actions.count("merchant.background_checks_started") == 1
    assert actions.count("merchant.background_checks_complete") == 1
    complete = next(
        e for e in audit.entries if e["action"] == "merchant.background_checks_complete"
    )
    assert complete["details"]["skipped"] is False
    assert complete["details"]["failed_checks"] == ["web_presence"]
    assert complete["details"]["trigger"] == "intake"


# ---------------------------------------------------------------------------
# 3. Missing merchant — early exit, no scanner calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_merchant_short_circuits_without_scanner_calls(
    ctx: dict[str, object],
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``merchant_id`` not in the repo → worker returns ``skipped=True``,
    writes NO ``merchant.background_checks_*`` audit row, never calls
    either refresher. (The warning lands in the application log; tests
    can't readily assert on stdlib logging without extra plumbing.)"""
    ucc_calls, wp_calls = _patch_refreshers(monkeypatch)
    unknown_id = str(uuid4())

    result = await run_background_checks(ctx, unknown_id, "manual")

    assert result == {
        "merchant_id": unknown_id,
        "trigger": "manual",
        "skipped": True,
    }
    assert ucc_calls == []
    assert wp_calls == []
    bg_rows = [e for e in audit.entries if e["action"].startswith("merchant.background_checks")]
    assert bg_rows == []


# ---------------------------------------------------------------------------
# 4. Invalid UUID string — early exit, no audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_merchant_id_string_short_circuits(
    ctx: dict[str, object],
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``merchant_id_str`` isn't a UUID → worker logs + returns
    ``skipped=True`` with NO audit row + NO scanner calls. arq jobs
    re-running on a poisoned payload must not crash-loop."""
    ucc_calls, wp_calls = _patch_refreshers(monkeypatch)

    result = await run_background_checks(ctx, "not-a-uuid", "close_webhook")

    assert result == {
        "merchant_id": "not-a-uuid",
        "trigger": "close_webhook",
        "skipped": True,
    }
    assert ucc_calls == []
    assert wp_calls == []
    assert audit.entries == []


# ---------------------------------------------------------------------------
# 5. Happy path — both refreshers run, complete audit row carries no PII
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_runs_both_refreshers_and_audits_pii_safely(
    ctx: dict[str, object],
    merchants: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh merchant + both refreshers succeed → both invoked exactly
    once with the merchant_id, both audit rows write subject_id without
    leaking business_name / owner_name into ``details``."""
    merchant = _seed_merchant(merchants)
    ucc_calls, wp_calls = _patch_refreshers(monkeypatch)

    result = await run_background_checks(ctx, str(merchant.id), "close_webhook")

    assert result == {
        "merchant_id": str(merchant.id),
        "trigger": "close_webhook",
        "skipped": False,
        "failed_checks": [],
    }
    assert ucc_calls == [merchant.id]
    assert wp_calls == [merchant.id]

    for row in audit.entries:
        if not row["action"].startswith("merchant.background_checks"):
            continue
        # subject_id carries the merchant id; details carry trigger +
        # operational flags only. Per CLAUDE.md PII rule, business_name
        # / owner_name / state / ein must NEVER appear in the audit
        # details map.
        assert str(row["subject_id"]) == str(merchant.id)
        for field in ("business_name", "owner_name", "ein", "state"):
            assert field not in row["details"], (
                f"PII field {field!r} leaked into {row['action']} details"
            )


# ---------------------------------------------------------------------------
# 6. Partial-state merchant (only one timestamp set) — runs both anyway
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_state_merchant_does_not_short_circuit(
    ctx: dict[str, object],
    merchants: InMemoryMerchantRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Idempotency requires BOTH timestamps set. With only UCC populated
    the worker still runs both refreshers (each helper is itself
    idempotent-on-success, per workers.py docstring)."""
    now = datetime.now(UTC)
    merchant = _seed_merchant(merchants, ucc_checked_at=now, web_presence_scanned_at=None)
    ucc_calls, wp_calls = _patch_refreshers(monkeypatch)

    result = await run_background_checks(ctx, str(merchant.id), "intake")

    assert result["skipped"] is False
    assert ucc_calls == [merchant.id]
    assert wp_calls == [merchant.id]


# ---------------------------------------------------------------------------
# 7. Concurrent invocation — second job sees the first's writes as
# idempotency markers if the first ran to completion. Modeling true
# overlap (two workers in flight at the same instant) needs an async
# lock in the worker; today's behavior is "second invocation after the
# first finishes is a no-op." Anything stronger is a follow-up.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="needs async lock plumbing in run_background_checks "
    "(today's idempotency is sequential — second invocation skips only "
    "after the first writes ucc_checked_at + web_presence_scanned_at via "
    "the refresh helpers; mid-flight overlap is unmodeled)."
)
@pytest.mark.asyncio
async def test_concurrent_invocations_only_run_scanners_once() -> None:
    """TODO: drive two ``run_background_checks`` coroutines against the
    same merchant under ``asyncio.gather`` and assert exactly one set of
    refresher calls. Requires either an in-job lock or a repo-level
    advisory lock; current implementation is best-effort sequential."""
    ...


# Need an async event-loop sentinel for ctx-level asyncio.to_thread().
def _ensure_loop() -> Iterator[None]:
    yield
