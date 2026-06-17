"""Tests for the renewal-reminder cron (T5, 2026-06-17).

``run_renewal_reminder_pass`` walks the renewal pipeline (merchants
with maturity_date within the next 14 days or overdue) and posts one
"Renewal approaching" Close task per merchant. Dedupe key is a
``close.task.renewal_reminder`` audit row stamped with the current
calendar month — re-running the cron the same day (or any day in the
same month) must not duplicate the task.

Skip conditions:
  * Merchant has no close_lead_id → skipped silently
    (skipped_no_lead counter bumps).
  * Already prompted this calendar month → skipped (skipped_dup
    counter bumps).
  * Merchant outside the 14-day window → not in the pipeline at all.

Close API errors on the task POST are audited as
``close.task.renewal_reminder_failed`` but do NOT raise — one bad lead
must not abort the whole cron pass.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest

from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseClient
from aegis.config import get_settings
from aegis.merchants.models import MerchantRow
from aegis.merchants.renewal_reminder import (
    run_renewal_reminder_pass,
)
from aegis.merchants.repository import InMemoryMerchantRepository


@pytest.fixture
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def task_log() -> list[httpx.Request]:
    return []


@pytest.fixture
def close_client(
    monkeypatch: pytest.MonkeyPatch,
    task_log: list[httpx.Request],
) -> Iterator[CloseClient]:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)

    def transport(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/task/"):
            task_log.append(request)
            return httpx.Response(201, json={"id": "task_xyz"})
        return httpx.Response(405)

    yield CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


@pytest.fixture
def failing_close_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[CloseClient]:
    monkeypatch.setenv("CLOSE_API_KEY", "api_test")
    monkeypatch.setenv("CLOSE_API_BASE", "https://api.close.example")
    get_settings.cache_clear()
    monkeypatch.setattr("aegis.close.client.time.sleep", lambda _s: None)

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="task POST failed")

    yield CloseClient(http_client=httpx.Client(transport=httpx.MockTransport(transport)))


def _seed_renewing_merchant(
    repo: InMemoryMerchantRepository,
    *,
    business_name: str = "Renewal Co",
    maturity_date: date | None = None,
    close_lead_id: str | None = "lead_abc",
) -> MerchantRow:
    return repo.upsert(
        MerchantRow(
            business_name=business_name,
            owner_name="J Doe",
            state="CA",
            close_lead_id=close_lead_id,
            status="finalized",
            is_renewal=True,
            maturity_date=maturity_date,
            requested_amount=Decimal("50000.00"),
        )
    )


def test_creates_task_for_merchant_maturing_in_7_days(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    close_client: CloseClient,
    task_log: list[httpx.Request],
) -> None:
    today = date(2026, 6, 17)
    merchant = _seed_renewing_merchant(repo, maturity_date=today + timedelta(days=7))

    summary = run_renewal_reminder_pass(
        audit=audit,
        merchants=repo,
        close_client=close_client,
        today=today,
    )

    assert summary["considered"] == 1
    assert summary["created"] == 1
    assert summary["skipped_no_lead"] == 0
    assert summary["skipped_dup"] == 0
    assert len(task_log) == 1

    import json as _json

    payload = _json.loads(task_log[0].read())
    assert payload["lead_id"] == "lead_abc"
    assert "Renewal approaching" in payload["text"]
    assert "Renewal Co" in payload["text"]
    assert (today + timedelta(days=7)).isoformat() in payload["text"]
    assert payload["date"] == today.isoformat()

    audited = [e for e in audit.entries if e["action"] == "close.task.renewal_reminder"]
    assert len(audited) == 1
    assert audited[0]["details"]["month"] == "2026-06"
    assert audited[0]["subject_id"] == str(merchant.id)


def test_idempotent_within_same_calendar_month(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    close_client: CloseClient,
    task_log: list[httpx.Request],
) -> None:
    today = date(2026, 6, 17)
    _seed_renewing_merchant(repo, maturity_date=today + timedelta(days=5))

    run_renewal_reminder_pass(audit=audit, merchants=repo, close_client=close_client, today=today)
    # Second run on day 18 — same month, audit row blocks duplicate post.
    summary2 = run_renewal_reminder_pass(
        audit=audit,
        merchants=repo,
        close_client=close_client,
        today=today + timedelta(days=1),
    )

    assert summary2["created"] == 0
    assert summary2["skipped_dup"] == 1
    # Still only one task posted.
    assert len(task_log) == 1
    audited = [e for e in audit.entries if e["action"] == "close.task.renewal_reminder"]
    assert len(audited) == 1


def test_skips_merchant_without_close_lead_id(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    close_client: CloseClient,
    task_log: list[httpx.Request],
) -> None:
    today = date(2026, 6, 17)
    _seed_renewing_merchant(
        repo,
        maturity_date=today + timedelta(days=3),
        close_lead_id=None,
    )

    summary = run_renewal_reminder_pass(
        audit=audit, merchants=repo, close_client=close_client, today=today
    )

    assert summary["skipped_no_lead"] == 1
    assert summary["created"] == 0
    assert task_log == []
    assert not any(e["action"] == "close.task.renewal_reminder" for e in audit.entries)


def test_skips_merchant_outside_14_day_window(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    close_client: CloseClient,
    task_log: list[httpx.Request],
) -> None:
    today = date(2026, 6, 17)
    # 30 days out — should not appear in the pipeline at all.
    _seed_renewing_merchant(repo, maturity_date=today + timedelta(days=30))

    summary = run_renewal_reminder_pass(
        audit=audit, merchants=repo, close_client=close_client, today=today
    )
    assert summary["considered"] == 0
    assert summary["created"] == 0
    assert task_log == []


def test_overdue_merchant_still_gets_reminder(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    close_client: CloseClient,
    task_log: list[httpx.Request],
) -> None:
    """The renewal pipeline keeps overdue maturities visible — the cron
    must post a task for them so the operator chases the stale renewal."""
    today = date(2026, 6, 17)
    _seed_renewing_merchant(repo, maturity_date=today - timedelta(days=3))

    summary = run_renewal_reminder_pass(
        audit=audit, merchants=repo, close_client=close_client, today=today
    )
    assert summary["created"] == 1
    assert len(task_log) == 1


def test_new_month_unblocks_a_new_task(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    close_client: CloseClient,
    task_log: list[httpx.Request],
) -> None:
    """The dedupe key is the calendar month. A new month re-arms the
    reminder for any merchant still in the renewal pipeline."""
    day1 = date(2026, 6, 30)
    _seed_renewing_merchant(repo, maturity_date=day1 + timedelta(days=10))

    run_renewal_reminder_pass(audit=audit, merchants=repo, close_client=close_client, today=day1)
    assert len(task_log) == 1

    # Next day rolls into July — audit row's month is 2026-06; new month
    # blocks nothing, so the cron posts again.
    summary = run_renewal_reminder_pass(
        audit=audit,
        merchants=repo,
        close_client=close_client,
        today=date(2026, 7, 1),
    )
    assert summary["created"] == 1
    assert len(task_log) == 2


def test_close_task_failure_audits_but_does_not_raise(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    failing_close_client: CloseClient,
) -> None:
    """A Close 5xx on one task POST must not crash the whole cron."""
    today = date(2026, 6, 17)
    _seed_renewing_merchant(repo, maturity_date=today + timedelta(days=5))

    summary = run_renewal_reminder_pass(
        audit=audit,
        merchants=repo,
        close_client=failing_close_client,
        today=today,
    )
    assert summary["failed"] == 1
    assert summary["created"] == 0

    failures = [e for e in audit.entries if e["action"] == "close.task.renewal_reminder_failed"]
    assert len(failures) == 1
    assert failures[0]["details"]["status_code"] == 500
    # The success audit row must NOT have been written.
    assert not any(e["action"] == "close.task.renewal_reminder" for e in audit.entries)


def test_two_renewing_merchants_each_get_their_own_task(
    repo: InMemoryMerchantRepository,
    audit: InMemoryAuditLog,
    close_client: CloseClient,
    task_log: list[httpx.Request],
) -> None:
    today = date(2026, 6, 17)
    a = _seed_renewing_merchant(
        repo,
        business_name="Alpha Co",
        close_lead_id="lead_alpha",
        maturity_date=today + timedelta(days=3),
    )
    b = _seed_renewing_merchant(
        repo,
        business_name="Bravo Co",
        close_lead_id="lead_bravo",
        maturity_date=today + timedelta(days=10),
    )

    summary = run_renewal_reminder_pass(
        audit=audit, merchants=repo, close_client=close_client, today=today
    )
    assert summary["created"] == 2
    assert len(task_log) == 2

    audited = [e for e in audit.entries if e["action"] == "close.task.renewal_reminder"]
    audited_subject_ids = {e["subject_id"] for e in audited}
    assert audited_subject_ids == {str(a.id), str(b.id)}
