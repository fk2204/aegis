"""Tests for ``aegis.business_intel.bankruptcy_checker`` + refresh.

Synthetic CourtListener responses match the documented v4 shapes —
search hits with ``docket_id`` + ``dateFiled``, bankruptcy-information
rows with ``chapter`` + ``date_closed``. Real captured fixtures are
preferred per CLAUDE.md's external-integration rule but CourtListener's
public docs are stable and we don't have a sample API call landed yet;
the synthetic shapes are flagged for replacement once a real call lands.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest

from aegis.audit import InMemoryAuditLog
from aegis.business_intel.bankruptcy_checker import (
    BankruptcyResult,
    check_bankruptcy,
)
from aegis.business_intel.bankruptcy_refresh import (
    ensure_bankruptcy_check,
    refresh_bankruptcy_for_merchant,
)
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository


def _make_merchant(
    *,
    bankruptcy_checked_at: datetime | None = None,
) -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        business_name="Acme Bakery LLC",
        owner_name="Jane Smith",
        state="NY",
        bankruptcy_checked_at=bankruptcy_checked_at,
    )


def _json_response(payload: dict[str, Any], *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, json=payload)


class _StubAsyncClient:
    """Minimal AsyncClient stub. ``responses`` is a list of
    ``(url_substring, response_factory)`` pairs evaluated in order;
    the first substring match wins. ``factory`` is called with no args
    so each invocation gets a fresh Response instance (httpx Responses
    aren't safe to reuse)."""

    def __init__(
        self,
        responses: list[tuple[str, Any]],
        *,
        raise_after: int | None = None,
    ) -> None:
        self._responses = responses
        self._call_count = 0
        self._raise_after = raise_after
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        self._call_count += 1
        self.calls.append((url, params))
        if self._raise_after is not None and self._call_count > self._raise_after:
            raise httpx.NetworkError("simulated")
        for needle, factory in self._responses:
            if needle in url:
                resp: httpx.Response = factory()
                return resp
        # No match — return empty results, mirroring real CourtListener
        # behaviour on a no-hit query.
        return _json_response({"results": []})


# ---------------------------------------------------------------------------
# check_bankruptcy
# ---------------------------------------------------------------------------


def _search_resp(docket_ids: list[int]) -> httpx.Response:
    return _json_response(
        {
            "results": [
                {"docket_id": d, "dateFiled": "2024-03-15", "caseName": f"Case {d}"}
                for d in docket_ids
            ]
        }
    )


def _info_resp(*, chapter: str, closed: bool = False) -> httpx.Response:
    return _json_response(
        {
            "results": [
                {
                    "chapter": chapter,
                    "date_closed": "2024-09-01" if closed else None,
                    "date_converted": None,
                }
            ]
        }
    )


@pytest.mark.asyncio
async def test_active_chapter_7_blocks() -> None:
    """Open Chapter 7 with no closing date → ``active=True, chapter='7'``."""
    client = _StubAsyncClient(
        [
            ("/search/", lambda: _search_resp([101])),
            ("/bankruptcy-information/", lambda: _info_resp(chapter="7")),
        ]
    )
    result = await check_bankruptcy("Acme Bakery LLC", client=client)
    assert result.active is True
    assert result.chapter == "7"
    assert result.error is None
    assert len(result.cases) == 1
    assert result.cases[0]["docket_id"] == 101


@pytest.mark.asyncio
async def test_active_chapter_11_amber() -> None:
    """Active Ch.11 → ``chapter='11'`` (amber gate handled upstream)."""
    client = _StubAsyncClient(
        [
            ("/search/", lambda: _search_resp([202])),
            ("/bankruptcy-information/", lambda: _info_resp(chapter="11")),
        ]
    )
    result = await check_bankruptcy("Acme Bakery LLC", client=client)
    assert result.active is True
    assert result.chapter == "11"


@pytest.mark.asyncio
async def test_active_chapter_13_yellow() -> None:
    """Active Ch.13 → ``chapter='13'`` (informational chip only)."""
    client = _StubAsyncClient(
        [
            ("/search/", lambda: _search_resp([303])),
            ("/bankruptcy-information/", lambda: _info_resp(chapter="13")),
        ]
    )
    result = await check_bankruptcy("Acme Bakery LLC", client=client)
    assert result.active is True
    assert result.chapter == "13"


@pytest.mark.asyncio
async def test_discharged_chapter_7_within_seven_years() -> None:
    """Closed Ch.7 within the last 7 years → ``recent=True, active=False``."""
    client = _StubAsyncClient(
        [
            ("/search/", lambda: _search_resp([404])),
            ("/bankruptcy-information/", lambda: _info_resp(chapter="7", closed=True)),
        ]
    )
    result = await check_bankruptcy("Acme Bakery LLC", client=client)
    assert result.active is False
    assert result.recent is True
    assert result.chapter == "7"


@pytest.mark.asyncio
async def test_old_chapter_7_not_recent() -> None:
    """Closed Ch.7 older than 7 years → ``recent=False, active=False, chapter=None``."""
    ten_years_ago = (datetime.now(UTC) - timedelta(days=365 * 10)).date().isoformat()

    def _old_search() -> httpx.Response:
        return _json_response(
            {"results": [{"docket_id": 505, "dateFiled": ten_years_ago, "caseName": "Old"}]}
        )

    client = _StubAsyncClient(
        [
            ("/search/", _old_search),
            ("/bankruptcy-information/", lambda: _info_resp(chapter="7", closed=True)),
        ]
    )
    result = await check_bankruptcy("Acme Bakery LLC", client=client)
    assert result.active is False
    assert result.recent is False
    assert result.chapter is None
    assert result.cases[0]["recent"] is False


@pytest.mark.asyncio
async def test_business_name_and_owner_name_search_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both names dispatched in a single ``asyncio.gather`` call."""
    captured: list[int] = []
    real_gather = asyncio.gather

    async def _spy_gather(*tasks: Any, **kwargs: Any) -> Any:
        captured.append(len(tasks))
        return await real_gather(*tasks, **kwargs)

    monkeypatch.setattr("aegis.business_intel.bankruptcy_checker.asyncio.gather", _spy_gather)

    client = _StubAsyncClient(
        [
            ("/search/", lambda: _search_resp([])),
        ]
    )
    await check_bankruptcy("Acme Bakery LLC", owner_name="Jane Smith", client=client)
    assert captured == [2]


@pytest.mark.asyncio
async def test_timeout_collapses_to_empty_with_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``httpx.TimeoutException`` on every retry → empty result + error."""

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("aegis.business_intel.bankruptcy_checker.asyncio.sleep", _no_sleep)

    class _TimeoutClient:
        async def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
            raise httpx.TimeoutException("simulated")

    result = await check_bankruptcy("Acme Bakery LLC", client=_TimeoutClient())
    # The branch-level retries exhaust; ``_get_with_retry`` raises
    # ``_CourtListenerUnreachable`` which ``asyncio.gather`` captures.
    # The orchestrator's ``infra_error`` branch flips → error string set.
    assert result.error == "courtlistener_unreachable"
    assert result.active is False
    assert result.recent is False
    assert result.chapter is None


@pytest.mark.asyncio
async def test_5xx_then_success_after_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Initial 503 succeeds on retry — no error in final result."""

    # Skip the real backoff sleep so the test doesn't take 30s.
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("aegis.business_intel.bankruptcy_checker.asyncio.sleep", _no_sleep)

    call_count = {"n": 0}

    class _FiveOhThreeThenSuccess:
        async def get(self, url: str, *, params: Any = None, headers: Any = None) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _json_response({}, status_code=503)
            if "/search/" in url:
                return _search_resp([606])
            return _info_resp(chapter="7", closed=True)

    result = await check_bankruptcy("Acme Bakery LLC", client=_FiveOhThreeThenSuccess())
    assert result.error is None
    assert result.chapter == "7"


@pytest.mark.asyncio
async def test_all_retries_fail_collapses_no_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both retries return 5xx — empty result, no exception bubbles."""

    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("aegis.business_intel.bankruptcy_checker.asyncio.sleep", _no_sleep)

    class _Always503:
        async def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
            return _json_response({}, status_code=503)

    result = await check_bankruptcy("Acme Bakery LLC", client=_Always503())
    assert result.error == "courtlistener_unreachable"
    assert result.cases == []


@pytest.mark.asyncio
async def test_no_bankruptcy_info_row_drops_case() -> None:
    """Search hit without a bankruptcy-information row is dropped."""
    client = _StubAsyncClient(
        [
            ("/search/", lambda: _search_resp([707])),
            ("/bankruptcy-information/", lambda: _json_response({"results": []})),
        ]
    )
    result = await check_bankruptcy("Acme Bakery LLC", client=client)
    assert result.active is False
    assert result.cases == []


# ---------------------------------------------------------------------------
# refresh_bankruptcy_for_merchant
# ---------------------------------------------------------------------------


async def _stub_checker_factory(result: BankruptcyResult) -> Any:
    async def _call(business_name: str, owner_name: str | None = None) -> BankruptcyResult:
        return result

    return _call


@pytest.mark.asyncio
async def test_refresh_persists_and_audits_screened() -> None:
    """Every check writes a ``compliance.bankruptcy_screened`` audit row."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant()
    repo.upsert(merchant)

    checker = await _stub_checker_factory(
        BankruptcyResult(
            active=False,
            recent=False,
            chapter=None,
            cases=[],
            checked_at=datetime.now(UTC),
        )
    )
    result = await refresh_bankruptcy_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        checker=checker,
    )

    assert result.active is False
    refreshed = repo.get(merchant.id)
    assert refreshed.bankruptcy_checked_at is not None
    actions = [e["action"] for e in audit.entries]
    assert "compliance.bankruptcy_screened" in actions
    # No block row when there's no Chapter 7.
    assert "compliance.bankruptcy_block" not in actions


@pytest.mark.asyncio
async def test_refresh_writes_block_row_on_chapter_7_active() -> None:
    """Active Ch.7 → screened row AND block row both written."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant()
    repo.upsert(merchant)

    checker = await _stub_checker_factory(
        BankruptcyResult(
            active=True,
            recent=True,
            chapter="7",
            cases=[{"docket_id": 1, "chapter": "7", "active": True}],
            checked_at=datetime.now(UTC),
        )
    )
    await refresh_bankruptcy_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        checker=checker,
    )

    actions = [e["action"] for e in audit.entries]
    assert "compliance.bankruptcy_screened" in actions
    assert "compliance.bankruptcy_block" in actions


# ---------------------------------------------------------------------------
# ensure_bankruptcy_check (TTL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_skips_when_recent_check_exists() -> None:
    """A check inside the 30-day TTL window short-circuits without invoking
    the checker again."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _make_merchant(bankruptcy_checked_at=datetime.now(UTC))
    repo.upsert(merchant)

    invoked = {"called": False}

    async def _spy_checker(*args: Any, **kwargs: Any) -> BankruptcyResult:
        invoked["called"] = True
        raise AssertionError("checker should not run inside TTL window")

    refreshed = await ensure_bankruptcy_check(
        merchant,
        merchants_repo=repo,
        audit=audit,
        checker=_spy_checker,
    )
    assert refreshed.id == merchant.id
    assert invoked["called"] is False
    # No new audit row written.
    assert [e["action"] for e in audit.entries] == []


@pytest.mark.asyncio
async def test_ensure_runs_when_stale() -> None:
    """A check older than 30 days re-fires."""
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    stale = datetime.now(UTC) - timedelta(days=40)
    merchant = _make_merchant(bankruptcy_checked_at=stale)
    repo.upsert(merchant)

    checker = await _stub_checker_factory(
        BankruptcyResult(
            active=False,
            recent=False,
            chapter=None,
            checked_at=datetime.now(UTC),
        )
    )
    refreshed = await ensure_bankruptcy_check(
        merchant,
        merchants_repo=repo,
        audit=audit,
        checker=checker,
    )
    assert refreshed.bankruptcy_checked_at is not None
    assert refreshed.bankruptcy_checked_at > stale
    assert any(e["action"] == "compliance.bankruptcy_screened" for e in audit.entries)
