"""Federal bankruptcy check via the CourtListener v4 REST API.

Mirrors ``aegis.business_intel.ucc_checker`` in posture: Protocol-bound
client, frozen result dataclass, every failure mode collapses to an
empty result. Distinct from the UCC checker in that the upstream is a
deterministic public REST API (no LLM in the loop) so the parse path
is exact — no JSON-from-prose ambiguity.

CourtListener publishes the entire federal bankruptcy docket (FB
courts) plus party + case-metadata. The query path is:

1. ``GET /api/rest/v4/search/?type=r&q="{name}"&court_type=bankruptcy``
   — keyword search across docket metadata; ``score``-ordered.
2. ``GET /api/rest/v4/parties/?name={name}&docket__court__jurisdiction=FB``
   — exact party name match on bankruptcy-court dockets only.
3. ``GET /api/rest/v4/bankruptcy-information/`` (per-docket lookup) —
   resolves the chapter ("7" / "11" / "13") + filing/closing dates that
   ``search`` and ``parties`` don't carry.

Authentication is optional (``COURTLISTENER_API_TOKEN`` env var). The
anonymous tier allows ~100 req/day; with a token CourtListener bumps
the quota to 5,000 req/day. Both work; we read the env var with
``os.environ.get`` and only attach the ``Authorization`` header when
present.

Thursday 21:00-23:59 PT is CourtListener's published maintenance
window. ``_get_with_retry`` adds two 30-second backoff retries on
connection errors / 5xx so a maintenance hit doesn't fail the merchant
pipeline. After two failed retries the result collapses to
``BankruptcyResult(error="courtlistener_unreachable")`` and the
caller continues.

Audit + gating semantics live in
``aegis.business_intel.bankruptcy_refresh`` (persistence) and the
dossier route (gate render). This module is the pure API + parse
boundary; it does not touch the database or write audit rows.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from aegis.logger import get_logger

_log = get_logger(__name__)

COURTLISTENER_BASE = "https://www.courtlistener.com/api/rest/v4"

# CourtListener v4 reads ``Authorization: Token <key>`` — the anonymous
# tier works without the header (~100 req/day), authenticated bumps to
# 5,000 req/day. We never log the token value.
_TOKEN_ENV = "COURTLISTENER_API_TOKEN"  # noqa: S105 — env-var name, not a credential

# Per-request timeout, in seconds. CourtListener's median response is
# ~400ms but the search endpoint can spike on cold caches.
_REQUEST_TIMEOUT_SECONDS: float = 10.0

# Retry budget on connection errors / 5xx — Thursday-maintenance window
# resilience. Two retries at 30s each is up to ~70s wall clock per
# failed query. Inside ``asyncio.gather`` the per-name searches still proceed
# concurrently so the worst case is bounded by the slower branch, not
# their sum.
_RETRY_LIMIT: int = 2
_RETRY_BACKOFF_SECONDS: float = 30.0

# Bankruptcy "recent" window. Industry convention treats discharged
# Chapter 7 as a soft signal for 7 years after discharge (matches the
# FCRA reporting window). Operator surfaces this on the dossier chip
# even when the case is closed.
_RECENT_WINDOW_DAYS: int = 365 * 7

# Number of top search hits to resolve per name. Bankruptcy filings
# under a common business name can return dozens of unrelated dockets
# — capping at the top 5 keeps the chapter lookup bounded without
# missing the active case (which always scores highest under
# ``order_by=score``).
_MAX_SEARCH_HITS_PER_NAME: int = 5

# Chapter values that gate the dossier render. Anything else surfaces
# as informational only — the gate logic lives in
# ``bankruptcy_refresh``, but the parser canonicalises the string so
# downstream comparisons are exact.
_VALID_CHAPTERS: frozenset[str] = frozenset({"7", "11", "12", "13", "15"})


class _CourtListenerUnreachableError(Exception):
    """Raised by ``_get_with_retry`` when every retry was exhausted.

    Caller paths catch this and let it bubble through ``asyncio.gather``
    so the orchestrator can distinguish "no hits found" (real empty
    result) from "infra failure" (collapses to ``error=...``).
    """


@dataclass(frozen=True)
class BankruptcyResult:
    """One bankruptcy check outcome.

    ``error`` is non-None only on infrastructure failures (timeout,
    CourtListener 5xx, parse failure). ``active=False`` with no
    error means "we successfully checked and there's no open case."
    The two states are distinct on purpose — the dossier chip wording
    differs ("no record" vs "couldn't check").
    """

    active: bool
    recent: bool
    chapter: str | None
    cases: list[dict[str, Any]] = field(default_factory=list)
    checked_at: datetime | None = None
    error: str | None = None


class _HttpClientLike(Protocol):
    """Minimal async HTTP shape the checker depends on.

    Tests inject a stub that returns canned JSON; production uses
    ``httpx.AsyncClient``. Keeping the surface tiny means the stub
    has no chance to silently diverge from real ``httpx`` behaviour.
    """

    async def get(
        self, url: str, *, params: dict[str, Any] | None = ..., headers: dict[str, str] | None = ...
    ) -> httpx.Response: ...


async def _get_with_retry(
    client: _HttpClientLike,
    url: str,
    *,
    params: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> httpx.Response:
    """One GET with a 2-retry backoff on connection errors / 5xx.

    Returns the final ``httpx.Response`` on success (status < 500).
    Raises ``_CourtListenerUnreachableError`` after every retry is
    exhausted so the orchestrator can distinguish "no hits found"
    (real empty result) from "infrastructure failure" (collapses to
    ``BankruptcyResult(error=...)``).
    """
    last_exc: Exception | None = None
    for attempt in range(_RETRY_LIMIT + 1):
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code < 500:
                return resp
            # 5xx — backoff and retry. CourtListener returns 503 during
            # its Thursday maintenance window; the retry is the
            # primary resilience mechanism.
            _log.info(
                "bankruptcy.courtlistener_5xx attempt=%d status=%d url=%s",
                attempt + 1,
                resp.status_code,
                url,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_exc = exc
            _log.info(
                "bankruptcy.courtlistener_connect_failed attempt=%d error=%s",
                attempt + 1,
                type(exc).__name__,
            )
        if attempt < _RETRY_LIMIT:
            await asyncio.sleep(_RETRY_BACKOFF_SECONDS)
    _log.warning(
        "bankruptcy.courtlistener_exhausted_retries url=%s last_error=%s",
        url,
        type(last_exc).__name__ if last_exc else "5xx",
    )
    raise _CourtListenerUnreachableError(url)


def _auth_headers() -> dict[str, str] | None:
    """Build the ``Authorization`` header when the token env var is set.

    Never log the token value — header dict is returned by reference
    to httpx and not echoed.
    """
    token = os.environ.get(_TOKEN_ENV, "").strip()
    if not token:
        return None
    return {"Authorization": f"Token {token}"}


def _normalize_chapter(raw: Any) -> str | None:  # noqa: ANN401 — CourtListener returns mixed int/str
    """Coerce a CourtListener chapter value to its canonical string.

    CourtListener returns chapter as an integer in some payloads and a
    string ("Chapter 7" / "7" / "07") in others. Canonical form: the
    bare digit string. Anything not in ``_VALID_CHAPTERS`` is dropped
    to ``None`` so downstream comparisons are exact.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Strip leading "Chapter " prefix and any leading zeros.
    if text.lower().startswith("chapter "):
        text = text[len("chapter ") :].strip()
    text = text.lstrip("0") or text
    return text if text in _VALID_CHAPTERS else None


def _parse_date(raw: Any) -> datetime | None:  # noqa: ANN401 — opaque API value
    """Best-effort ISO-date parse. CourtListener returns ``YYYY-MM-DD``
    or full ISO timestamps depending on the endpoint."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        # ``fromisoformat`` handles both ``2024-03-15`` and
        # ``2024-03-15T14:32:18.123456+00:00`` in 3.11+.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        return None


def _is_recent(filing_dt: datetime | None) -> bool:
    """Recent = within the last 7 years from now (UTC)."""
    if filing_dt is None:
        return False
    return (datetime.now(UTC) - filing_dt) <= timedelta(days=_RECENT_WINDOW_DAYS)


def _is_active(case: dict[str, Any]) -> bool:
    """A case is active when it has no closing/terminated date.

    CourtListener marks closed cases with ``date_terminated`` (docket
    field) or ``date_closed`` (bankruptcy-information field). Either
    present + non-null → closed. Both absent → still active.
    """
    if case.get("date_terminated"):
        return False
    if case.get("date_closed"):
        return False
    return True


async def _search_name(
    client: _HttpClientLike,
    name: str,
    *,
    headers: dict[str, str] | None,
) -> list[dict[str, Any]]:
    """Run the ``search`` query for one name; return the top N hits."""
    resp = await _get_with_retry(
        client,
        f"{COURTLISTENER_BASE}/search/",
        params={
            "type": "r",  # type=r → recap (federal court dockets, includes bankruptcy)
            "q": f'"{name}"',
            "court_type": "bankruptcy",
            "order_by": "score",
        },
        headers=headers,
    )
    try:
        data = resp.json()
    except ValueError:
        _log.warning("bankruptcy.search_json_decode_failed name_len=%d", len(name))
        return []
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    # Defensive: each entry must be a dict so downstream key access
    # doesn't blow up. CourtListener has been stable but the v4 API
    # has shipped enum-string entries in older releases.
    hits = [r for r in results[:_MAX_SEARCH_HITS_PER_NAME] if isinstance(r, dict)]
    return hits


async def _resolve_bankruptcy_info(
    client: _HttpClientLike,
    docket_id: int | str,
    *,
    headers: dict[str, str] | None,
) -> dict[str, Any] | None:
    """Pull the bankruptcy-information row for one docket.

    The chapter + filing/closing dates live here, not on the parent
    docket row. Returns ``None`` when CourtListener has no
    bankruptcy-information record for the docket (which means it's
    not a real bankruptcy case — we drop it from results).
    """
    resp = await _get_with_retry(
        client,
        f"{COURTLISTENER_BASE}/bankruptcy-information/",
        params={"docket": str(docket_id)},
        headers=headers,
    )
    try:
        data = resp.json()
    except ValueError:
        return None
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    return first if isinstance(first, dict) else None


def _docket_id_from_hit(hit: dict[str, Any]) -> int | str | None:
    """Extract the docket primary key from a search hit.

    CourtListener returns the docket id on different keys depending
    on the endpoint version (``docket_id`` on v4 search, ``docket``
    on the parties endpoint). Accept either.
    """
    for key in ("docket_id", "docket", "id"):
        v = hit.get(key)
        if isinstance(v, int) and v > 0:
            return v
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    return None


async def _check_one_name(
    client: _HttpClientLike,
    name: str,
    *,
    headers: dict[str, str] | None,
) -> list[dict[str, Any]]:
    """Search + chapter-resolve for one party name.

    Returns the per-case dict the orchestrator will fold into the
    final ``BankruptcyResult``. Each entry carries the normalised
    chapter, the filing date, an ``active`` boolean, and the docket id.
    """
    name = (name or "").strip()
    if not name:
        return []
    hits = await _search_name(client, name, headers=headers)
    if not hits:
        return []
    resolved: list[dict[str, Any]] = []
    for hit in hits:
        docket_id = _docket_id_from_hit(hit)
        if docket_id is None:
            continue
        info = await _resolve_bankruptcy_info(client, docket_id, headers=headers)
        if info is None:
            # No bankruptcy-information row → not a real bankruptcy.
            continue
        chapter = _normalize_chapter(info.get("chapter"))
        filing_dt = _parse_date(info.get("date_converted") or info.get("date_last_to_file_claims"))
        if filing_dt is None:
            filing_dt = _parse_date(hit.get("dateFiled") or hit.get("date_filed"))
        # Merge the docket + info dicts so the gating logic can see
        # ``date_terminated`` (docket) AND ``date_closed`` (info).
        merged: dict[str, Any] = {
            "docket_id": docket_id,
            "chapter": chapter,
            "filing_date": filing_dt.isoformat() if filing_dt else None,
            "date_terminated": hit.get("date_terminated"),
            "date_closed": info.get("date_closed"),
            "case_name": hit.get("caseName") or hit.get("case_name"),
            "court_id": hit.get("court_id") or hit.get("court"),
        }
        merged["active"] = _is_active(merged)
        merged["recent"] = _is_recent(filing_dt)
        resolved.append(merged)
    return resolved


def _summarise(cases: list[dict[str, Any]]) -> tuple[bool, bool, str | None]:
    """Reduce per-case rows to the top-level ``(active, recent, chapter)``.

    The chapter reported is the most-severe active chapter (7 > 11 >
    12 > 13 > 15 by underwriting impact). If no case is active, the
    chapter is the most-severe recent chapter. If nothing is recent
    either, ``chapter`` is ``None``.
    """
    if not cases:
        return False, False, None
    active_chapters = [c["chapter"] for c in cases if c.get("active") and c.get("chapter")]
    recent_chapters = [c["chapter"] for c in cases if c.get("recent") and c.get("chapter")]
    is_active = bool(active_chapters)
    is_recent = bool(recent_chapters)
    # Severity order: most aggressive bankruptcy first.
    severity = ["7", "11", "12", "13", "15"]
    pool = active_chapters if is_active else recent_chapters
    chapter: str | None = None
    for c in severity:
        if c in pool:
            chapter = c
            break
    return is_active, is_recent, chapter


async def check_bankruptcy(
    business_name: str,
    owner_name: str | None = None,
    *,
    client: _HttpClientLike | None = None,
) -> BankruptcyResult:
    """Run a federal bankruptcy check; collapse on failure.

    ``business_name`` is the canonical search term — CourtListener
    matches party strings exactly. ``owner_name`` is searched
    concurrently so a personal bankruptcy filed under the principal's
    name still surfaces. Empty / None inputs are skipped per-branch;
    if both are empty the function short-circuits to an empty result.

    On any infrastructure failure (timeout, exhausted retries, JSON
    parse error) the result is ``BankruptcyResult(error=<reason>)``
    with ``active=False`` and ``recent=False`` — the dossier
    distinguishes "checked and found nothing" from "couldn't check"
    on the ``error`` field.
    """
    business_clean = (business_name or "").strip()
    owner_clean = (owner_name or "").strip()
    if not business_clean and not owner_clean:
        return BankruptcyResult(active=False, recent=False, chapter=None)

    headers = _auth_headers()
    owned_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)
        owned_client = True

    try:
        tasks = []
        if business_clean:
            tasks.append(_check_one_name(client, business_clean, headers=headers))
        if owner_clean and owner_clean.lower() != business_clean.lower():
            tasks.append(_check_one_name(client, owner_clean, headers=headers))
        try:
            grouped = await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            return BankruptcyResult(active=False, recent=False, chapter=None, error="timeout")
        cases: list[dict[str, Any]] = []
        infra_error = False
        for branch in grouped:
            if isinstance(branch, _CourtListenerUnreachableError):
                infra_error = True
                _log.warning(
                    "bankruptcy.branch_unreachable error=%s",
                    type(branch).__name__,
                )
                continue
            if isinstance(branch, BaseException):
                # Unexpected — surface the type for debugging but
                # treat as infra failure so the dossier shows "couldn't
                # check" rather than silently returning a false-negative.
                infra_error = True
                _log.warning(
                    "bankruptcy.branch_failed error=%s",
                    type(branch).__name__,
                )
                continue
            cases.extend(branch)
        if infra_error and not cases:
            return BankruptcyResult(
                active=False,
                recent=False,
                chapter=None,
                error="courtlistener_unreachable",
            )
        active, recent, chapter = _summarise(cases)
        return BankruptcyResult(
            active=active,
            recent=recent,
            chapter=chapter,
            cases=cases,
            checked_at=datetime.now(UTC),
        )
    finally:
        if owned_client:
            await client.aclose()  # type: ignore[union-attr]  # owned httpx.AsyncClient


__all__ = [
    "COURTLISTENER_BASE",
    "BankruptcyResult",
    "check_bankruptcy",
]
