"""OFAC SDN + Consolidated sanctions screening — local-cache, fail-closed.

The screener reads the merged JSON cache built by
``scripts/update_ofac_list.py`` (default
``/var/lib/aegis/ofac_cache/ofac_unified.json``) and matches the
merchant's ``business_name`` + ``owner_name`` against every entry name
and alias using Jaro-Winkler similarity ≥ 0.88 (primary) plus a token-
sort ratio fallback. Both the SDN and Consolidated lists are screened
in one pass.

Hard-fail posture
-----------------
- Cache file missing → ``is_clear=False``, ``error="cache_missing"``.
- Cache age > 7 days → ``is_clear=False``, ``error="cache_stale"``.
- Any exception during screening → ``is_clear=False``, ``error=...``.

The dossier route reads ``merchant.ofac_is_clear``; when it's ``False``
the funder-matching grid is suppressed and a red banner is rendered.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

import jellyfish

from aegis.audit import AuditLog
from aegis.close.client import CloseClient
from aegis.close.compliance_tasks import (
    ComplianceGateType,
    OFACGateDetails,
    create_compliance_gate_task,
)
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import MerchantNotFoundError, MerchantRepository

_log = get_logger(__name__)

# Fuzzy-match cutoff. Calibrated for "named John Doe variants" vs
# false-positive on generic English names. 0.88 catches "John A. Doe"
# vs "John Doe" (≈ 0.95) and "Jon Doe" (≈ 0.91); rejects "Jonathan
# Smith" vs "John Doe" (≈ 0.65). Source: jellyfish docs + manual
# spot-check against SDN entries.
JARO_WINKLER_THRESHOLD: Final[float] = 0.88

# Token-sort secondary check uses simple-ratio cutoff. Catches cases
# where word order differs ("Doe, John A." vs "John A. Doe") that
# Jaro-Winkler under-counts.
TOKEN_SORT_THRESHOLD: Final[float] = 0.88

CACHE_STALE_THRESHOLD: Final[timedelta] = timedelta(days=7)

# Override via env in tests + box-side smoke runs.
DEFAULT_CACHE_PATH: Final[Path] = Path(
    os.environ.get(
        "AEGIS_OFAC_CACHE_PATH",
        "/var/lib/aegis/ofac_cache/ofac_unified.json",
    )
)


@dataclass(frozen=True)
class OFACResult:
    is_clear: bool
    match_detail: tuple[str, ...] = field(default_factory=tuple)
    screened_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    lists_checked: tuple[str, ...] = field(default_factory=tuple)
    cache_age_hours: float = 0.0
    error: str | None = None


class _SupabaseLike(Protocol):
    def table(self, name: str) -> object: ...


def _normalize_name(s: str) -> str:
    """Match the normalization used by the cache builder."""
    out: list[str] = []
    prev_space = True
    for ch in s.upper():
        if ch.isalnum():
            out.append(ch)
            prev_space = False
        else:
            if not prev_space:
                out.append(" ")
                prev_space = True
    return "".join(out).strip()


def _token_sort_ratio(a: str, b: str) -> float:
    """Token-sort Jaro-Winkler: sort tokens then compare. Useful when
    the names are the same words in a different order."""
    sa = " ".join(sorted(a.split()))
    sb = " ".join(sorted(b.split()))
    if not sa or not sb:
        return 0.0
    return float(jellyfish.jaro_winkler_similarity(sa, sb))


def _load_cache(cache_path: Path) -> dict[str, object] | None:
    if not cache_path.exists():
        return None
    try:
        with cache_path.open("r", encoding="utf-8") as fh:
            loaded = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("ofac.cache_read_failed path=%s error=%s", cache_path, exc)
        return None
    if not isinstance(loaded, dict):
        return None
    # json.load returns ``Any``-typed payloads; we know the shape is a
    # dict (just asserted) but mypy can't refine across the boundary.
    return dict(loaded)


def _cache_age_hours(cache: dict[str, object]) -> float:
    fetched_at_raw = cache.get("fetched_at")
    if not isinstance(fetched_at_raw, str):
        return float("inf")
    try:
        fetched_at = datetime.fromisoformat(fetched_at_raw)
    except ValueError:
        return float("inf")
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - fetched_at).total_seconds() / 3600.0


def _screen_one_name(
    *,
    candidate: str,
    entries: list[dict[str, object]],
) -> list[str]:
    """Compare ``candidate`` to every entry name + alias. Return a list
    of human-readable match descriptions; empty list means no match."""
    if not candidate:
        return []
    candidate_norm = _normalize_name(candidate)
    if not candidate_norm:
        return []
    matches: list[str] = []
    for entry in entries:
        names_to_check: list[str] = []
        name = entry.get("name")
        if isinstance(name, str) and name:
            names_to_check.append(name)
        aliases = entry.get("aliases")
        if isinstance(aliases, list):
            for alias in aliases:
                if isinstance(alias, str) and alias:
                    names_to_check.append(alias)
        for sanc_name in names_to_check:
            sanc_norm = _normalize_name(sanc_name)
            if not sanc_norm:
                continue
            jw = float(jellyfish.jaro_winkler_similarity(candidate_norm, sanc_norm))
            ts = (
                _token_sort_ratio(candidate_norm, sanc_norm) if jw < JARO_WINKLER_THRESHOLD else 0.0
            )
            if jw >= JARO_WINKLER_THRESHOLD or ts >= TOKEN_SORT_THRESHOLD:
                uid = entry.get("uid", "?")
                list_name = entry.get("list", "?")
                # Report the SANC name (it's already public on
                # ofac.treasury.gov so this isn't a PII leak); the
                # CANDIDATE name is the merchant's business / owner and
                # MUST NOT appear in the match_detail (logged via
                # audit_log, which is operator-readable but the rule
                # is strict).
                matches.append(f"{list_name}:{uid} :: {sanc_name} (jw={jw:.2f} ts={ts:.2f})")
                break  # one match per entry is enough; stop checking aliases
    return matches


def screen_merchant(
    business_name: str | None,
    owner_name: str | None,
    *,
    cache_path: Path = DEFAULT_CACHE_PATH,
    now: datetime | None = None,
) -> OFACResult:
    """Screen one merchant against the merged OFAC cache.

    Fail-closed on every failure mode — the caller is expected to
    treat ``is_clear=False`` as a hard block.
    """
    screened_at = now or datetime.now(UTC)
    cache = _load_cache(cache_path)
    if cache is None:
        return OFACResult(
            is_clear=False,
            screened_at=screened_at,
            cache_age_hours=0.0,
            error="cache_missing",
        )

    age = _cache_age_hours(cache)
    lists_raw = cache.get("lists_checked", [])
    lists_checked: tuple[str, ...]
    if isinstance(lists_raw, list):
        lists_checked = tuple(s for s in lists_raw if isinstance(s, str))
    else:
        lists_checked = ()

    if age > CACHE_STALE_THRESHOLD.total_seconds() / 3600.0:
        return OFACResult(
            is_clear=False,
            screened_at=screened_at,
            lists_checked=lists_checked,
            cache_age_hours=age,
            error="cache_stale",
        )

    entries_raw = cache.get("entries")
    if not isinstance(entries_raw, list):
        return OFACResult(
            is_clear=False,
            screened_at=screened_at,
            lists_checked=lists_checked,
            cache_age_hours=age,
            error="cache_malformed",
        )
    entries: list[dict[str, object]] = [e for e in entries_raw if isinstance(e, dict)]

    try:
        matches: list[str] = []
        if business_name:
            matches.extend(_screen_one_name(candidate=business_name, entries=entries))
        if owner_name:
            matches.extend(_screen_one_name(candidate=owner_name, entries=entries))
    except Exception as exc:
        return OFACResult(
            is_clear=False,
            screened_at=screened_at,
            lists_checked=lists_checked,
            cache_age_hours=age,
            error=str(exc)[:200],
        )

    return OFACResult(
        is_clear=not matches,
        match_detail=tuple(matches),
        screened_at=screened_at,
        lists_checked=lists_checked,
        cache_age_hours=age,
    )


def refresh_ofac_for_merchant(
    merchant_id: UUID,
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    cache_path: Path = DEFAULT_CACHE_PATH,
    close_client: CloseClient | None = None,
) -> OFACResult:
    """Always run a fresh screening, persist the four ofac_* columns,
    write one ``compliance.ofac_screened`` audit row, and (if the result
    is a block) one ``compliance.ofac_block`` row.

    Mirrors ``refresh_ucc_for_merchant`` for the operator-triggered +
    scorer-triggered paths.
    """
    merchant = merchants_repo.get(merchant_id)
    result = screen_merchant(
        merchant.business_name,
        merchant.owner_name,
        cache_path=cache_path,
    )
    cache = _load_cache(cache_path)
    cache_date: datetime | None = None
    if cache is not None:
        fetched_at_raw = cache.get("fetched_at")
        if isinstance(fetched_at_raw, str):
            try:
                cache_date = datetime.fromisoformat(fetched_at_raw)
                if cache_date.tzinfo is None:
                    cache_date = cache_date.replace(tzinfo=UTC)
            except ValueError:
                cache_date = None

    updated = merchant.model_copy(
        update={
            "ofac_checked_at": result.screened_at,
            "ofac_is_clear": result.is_clear,
            "ofac_match_detail": list(result.match_detail),
            "ofac_cache_date": cache_date,
        }
    )
    merchants_repo.upsert(updated)

    audit.record(
        actor="operator",
        action="compliance.ofac_screened",
        subject_type="merchant",
        subject_id=merchant.id,
        details={
            "is_clear": result.is_clear,
            "match_count": len(result.match_detail),
            "lists_checked": list(result.lists_checked),
            "cache_age_hours": round(result.cache_age_hours, 2),
            "error": result.error,
        },
    )
    if not result.is_clear:
        audit.record(
            actor="operator",
            action="compliance.ofac_block",
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "match_count": len(result.match_detail),
                "match_detail": list(result.match_detail),
                "error": result.error,
            },
        )
        # Build-plan 7.3: auto-file a Close task for the operator.
        # Wrapped — a Close outage MUST NOT block the gate decision.
        # ``match_detail`` is a tuple of human-readable lines like
        # ``"sdn:9999 :: BLOCKED ENTITY HOLDINGS LLC (jw=0.97 ts=0.94)"``;
        # the SDN name after the " :: " is the public-record entry we
        # surface in the task text.
        if close_client is not None and result.match_detail:
            sdn_name = _first_match_sdn_name(result.match_detail[0])
            try:
                create_compliance_gate_task(
                    merchant=updated,
                    gate_type=ComplianceGateType.OFAC_BLOCK,
                    details=OFACGateDetails(sdn_name=sdn_name),
                    client=close_client,
                    audit=audit,
                )
            except Exception:  # defense-in-depth around the gate
                _log.warning(
                    "ofac.compliance_task_unexpected_error merchant_id=%s",
                    merchant.id,
                    exc_info=True,
                )
    return result


def _first_match_sdn_name(match_line: str) -> str:
    """Pull the SDN entry name from a match_detail line.

    ``match_detail`` lines look like
    ``"sdn:9999 :: BLOCKED ENTITY HOLDINGS LLC (jw=0.97 ts=0.94)"``;
    the name is everything between ``" :: "`` and the trailing
    ``"(jw=...)"``. Defensive on shape — a malformed line falls back
    to the full string so the task text always carries something.
    """
    after_sep = match_line.split(" :: ", 1)
    if len(after_sep) != 2:
        return match_line.strip() or "(unknown)"
    tail = after_sep[1]
    # Drop the trailing " (jw=X.XX ts=X.XX)" scoring annotation.
    paren_idx = tail.rfind(" (jw=")
    if paren_idx > 0:
        tail = tail[:paren_idx]
    return tail.strip() or "(unknown)"


def ensure_ofac_check(
    merchant: MerchantRow,
    *,
    merchants_repo: MerchantRepository,
    audit: AuditLog,
    cache_path: Path = DEFAULT_CACHE_PATH,
    close_client: CloseClient | None = None,
) -> MerchantRow:
    """Lazy version: screen only when ``ofac_checked_at`` is None.

    Mirrors ``ensure_ucc_check`` semantics. Returns the (possibly
    refreshed) merchant row so the caller can read ``ofac_is_clear``
    in the same request without a second DB round-trip.
    """
    if merchant.ofac_checked_at is not None:
        return merchant
    try:
        refresh_ofac_for_merchant(
            merchant.id,
            merchants_repo=merchants_repo,
            audit=audit,
            cache_path=cache_path,
            close_client=close_client,
        )
    except MerchantNotFoundError:
        _log.warning(
            "ofac.ensure_skipped_unknown_merchant merchant_id=%s",
            merchant.id,
        )
        return merchant
    return merchants_repo.get(merchant.id)


__all__ = [
    "CACHE_STALE_THRESHOLD",
    "DEFAULT_CACHE_PATH",
    "JARO_WINKLER_THRESHOLD",
    "TOKEN_SORT_THRESHOLD",
    "OFACResult",
    "ensure_ofac_check",
    "refresh_ofac_for_merchant",
    "screen_merchant",
]
