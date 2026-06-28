"""Secretary of State entity check.

Lookup-then-Bedrock pattern: hit a local SQLite cache (populated by
``scripts/build_sos_database.py`` on a weekly systemd timer) first;
fall back to a prompt-only Bedrock invocation when the merchant's
state isn't covered by bulk data or the name has no fuzzy match.

Local hits are instant + $0. Bedrock fallback uses the cost-tracking
client wrapper so per-call cost rolls up in ``llm_costs``.

The original fallback used the ``web_search_20250305`` server tool
(see :meth:`aegis.llm.BedrockClient.invoke_with_web_search`). That
tool was rejected for the AEGIS Bedrock account during the bulk
SOS pass — every fallback collapsed to ``data_source="no_data"`` on
the dossier, making the panel look broken. The current default is
prompt-only (:meth:`aegis.llm.BedrockClient.invoke_prompt_only`):
the model answers from training-time knowledge and is told to
return ``"Not Found"`` when it can't reliably identify the entity.
Set ``AEGIS_SOS_FALLBACK_MODE=web_search`` to flip back to the old
tool-call path while iterating.

Mirrors ``aegis.business_intel.ucc_checker`` posture: frozen dataclass
result, Protocol client, every failure mode collapses to an empty
``SOSResult``. Caller treats the result as a soft signal — the dossier
renders a chip but never auto-decides on it.

Data-source taxonomy on the returned ``SOSResult``:

* ``local_db:{STATE}`` — matched from the local SQLite cache.
* ``bedrock`` — matched via Bedrock (prompt-only or web-search).
* ``bedrock_not_found`` — Bedrock answered cleanly with "Not Found".
* ``bedrock_unparseable`` — Bedrock answered, response not valid JSON.
* ``bedrock_error`` — Bedrock invocation raised (network, throttle,
  permission, etc.). The "we tried and the call failed" signal.
* ``no_data`` — we never tried (empty business name; Bedrock client
  could not be constructed). The "didn't even try" signal.

The dossier template renders each taxonomy entry in plain English so
the operator can distinguish "Bedrock unreachable" from "Bedrock said
no such entity exists".
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Protocol

from aegis.logger import get_logger

_log = get_logger(__name__)

# Default cache location. ``/var/lib/aegis/`` survives deploys; the
# systemd timer writes here as user ``aegis``. Override via
# ``SOSChecker(db_path=...)`` for tests.
DEFAULT_DB_PATH: Final[Path] = Path("/var/lib/aegis/sos_cache/sos_entities.db")

# Jaro-Winkler similarity threshold for fuzzy name match against the
# local index. 0.88 is the same threshold the OFAC checker uses (Phase
# A); high enough to reject "Acme Restaurant Inc" vs "Acme Roofing Inc"
# but low enough to absorb LLC / DBA / punctuation drift.
_FUZZY_THRESHOLD: Final[float] = 0.88

# Entity-suffix normalisation — lossy but consistent. "LLC" / "L.L.C." /
# "LIMITED LIABILITY COMPANY" all collapse to "LLC" before comparison
# so registry exports keyed in different conventions can still match.
_ENTITY_SUFFIX_MAP: Final[dict[str, str]] = {
    "LIMITED LIABILITY COMPANY": "LLC",
    "LIMITED LIABILITY CO": "LLC",
    "LIMITED LIABILITY": "LLC",
    "L.L.C.": "LLC",
    "L L C": "LLC",
    "INCORPORATED": "INC",
    "INC.": "INC",
    "CORPORATION": "CORP",
    "CORP.": "CORP",
    "COMPANY": "CO",
    "CO.": "CO",
    "LIMITED PARTNERSHIP": "LP",
    "L.P.": "LP",
    "PROFESSIONAL CORPORATION": "PC",
    "P.C.": "PC",
}

# Status tokens that mean "this entity is in good standing". Anything
# else (DISSOLVED, WITHDRAWN, INACTIVE, REVOKED, ADMINISTRATIVELY
# DISSOLVED) collapses to ``is_active = False``. ``None`` when the
# registry returns nothing parseable.
_ACTIVE_STATUS_TOKENS: Final[frozenset[str]] = frozenset(
    {"ACTIVE", "GOOD STANDING", "CURRENT", "IN GOOD STANDING"}
)


@dataclass(frozen=True)
class SOSResult:
    """One SOS lookup outcome. Empty = "no data found, move on"."""

    found: bool
    status: str | None
    entity_name: str | None
    formation_date: str | None
    is_active: bool | None
    data_source: str
    checked_at: datetime
    error: str | None = None


class _BedrockClientLike(Protocol):
    """Minimal protocol the checker needs.

    Both methods are listed because the env-var-gated rollback to the
    web-search path needs them coexisting. The prompt-only method is
    the default; the web-search method is only called when
    ``AEGIS_SOS_FALLBACK_MODE=web_search``.
    """

    def invoke_prompt_only(self, prompt: str) -> str: ...
    def invoke_with_web_search(self, prompt: str) -> str: ...


def normalize_business_name(name: str) -> str:
    """Uppercase, strip non-alphanumeric, collapse entity suffixes.

    Lossy by design — the same business indexed under "Acme LLC" vs
    "ACME, L.L.C." vs "Acme Limited Liability Company" produces one
    canonical key. Used both for the index column and at query time.
    """
    s = (name or "").upper().strip()
    # Collapse multi-word suffix variants first (longest match wins).
    for long_form, short in sorted(_ENTITY_SUFFIX_MAP.items(), key=lambda x: -len(x[0])):
        s = s.replace(long_form, short)
    # Strip non-alphanumeric (drops commas, periods, ampersands, spaces).
    s = re.sub(r"[^A-Z0-9]", "", s)
    return s


def _coerce_is_active(status: str | None) -> bool | None:
    if not status:
        return None
    normalized = status.strip().upper()
    if normalized in _ACTIVE_STATUS_TOKENS:
        return True
    return False


class SOSChecker:
    """Local SQLite lookup with Bedrock fallback.

    ``check_entity(business_name, state)`` returns one ``SOSResult``.
    All failures (missing DB, empty state, Bedrock error, JSON parse
    failure) collapse to an empty result so the caller never has to
    branch.
    """

    def __init__(
        self,
        *,
        db_path: Path = DEFAULT_DB_PATH,
        bedrock_client: _BedrockClientLike | None = None,
    ) -> None:
        self._db_path = db_path
        self._bedrock_client = bedrock_client

    def check_entity(self, business_name: str, state: str | None) -> SOSResult:
        now = datetime.now(UTC)
        name = (business_name or "").strip()
        if not name:
            return SOSResult(
                found=False,
                status=None,
                entity_name=None,
                formation_date=None,
                is_active=None,
                data_source="no_data",
                checked_at=now,
                error="business_name_empty",
            )

        state_norm = (state or "").strip().upper() or None
        if state_norm is None:
            # Skip local DB lookup, go straight to Bedrock fallback —
            # state is needed for both paths but Bedrock can search
            # nationally when forced. Keeps the contract simple.
            return self._check_via_bedrock(name, state_norm, now)

        # Try local SQLite first.
        try:
            local = self._check_local_db(name, state_norm, now)
        except sqlite3.Error as exc:
            _log.warning(
                "sos_checker.local_db_failed business_name_hash=%s state=%s err=%s",
                hash(name),
                state_norm,
                exc,
            )
            local = None
        if local is not None and local.found:
            return local
        return self._check_via_bedrock(name, state_norm, now)

    # ------------------------------------------------------------------
    # Local SQLite lookup
    # ------------------------------------------------------------------
    def _check_local_db(self, name: str, state: str, now: datetime) -> SOSResult | None:
        if not self._db_path.exists():
            return None
        normalized = normalize_business_name(name)
        if not normalized:
            return None

        # Two-pass: exact normalized hit first (uses the composite
        # index), then fuzzy walk over the state's rows. Both queries
        # are bounded by the per-state row count, which keeps a single
        # check well under 10ms on the largest state shards.
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT business_name, status, entity_type, formation_date, "
                "data_source FROM sos_entities "
                "WHERE business_name_normalized = ? AND state = ? LIMIT 1",
                (normalized, state),
            ).fetchone()
            if row is not None:
                return _row_to_result(row, state, now, exact_match=True)

            # Fuzzy fallback. Try ``jellyfish.jaro_winkler_similarity``
            # against the state's rows. Lazy import keeps the cold-path
            # import time on first-touch; jellyfish is on the project
            # dependencies (added with migration 085 + Phase A OFAC).
            try:
                import jellyfish  # PLC0415 lazy import — dep landed with this commit
            except ImportError:
                _log.warning("sos_checker.jellyfish_missing — exact-match only")
                return None

            rows = conn.execute(
                "SELECT business_name, business_name_normalized, status, "
                "entity_type, formation_date, data_source "
                "FROM sos_entities WHERE state = ?",
                (state,),
            ).fetchall()
            best: sqlite3.Row | None = None
            best_score = 0.0
            for candidate in rows:
                score = jellyfish.jaro_winkler_similarity(
                    normalized, candidate["business_name_normalized"]
                )
                if score > best_score:
                    best_score = score
                    best = candidate
            if best is not None and best_score >= _FUZZY_THRESHOLD:
                return _row_to_result(best, state, now, exact_match=False)
            return SOSResult(
                found=False,
                status=None,
                entity_name=None,
                formation_date=None,
                is_active=None,
                data_source="no_data",
                checked_at=now,
            )

    # ------------------------------------------------------------------
    # Bedrock fallback
    # ------------------------------------------------------------------
    def _check_via_bedrock(self, name: str, state: str | None, now: datetime) -> SOSResult:
        client = self._bedrock_client
        if client is None:
            try:
                from aegis.ops.cost_tracking import build_cost_tracking_client

                client = build_cost_tracking_client(call_type="business_intel")
            except Exception:
                _log.warning(
                    "sos_checker.bedrock_init_failed business_name_hash=%s",
                    hash(name),
                    exc_info=True,
                )
                return SOSResult(
                    found=False,
                    status=None,
                    entity_name=None,
                    formation_date=None,
                    is_active=None,
                    data_source="no_data",
                    checked_at=now,
                    error="bedrock_init_failed",
                )

        # Default path is prompt-only — the ``web_search_20250305`` server
        # tool was rejected for the AEGIS Bedrock account during the bulk
        # SOS pass, collapsing every fallback to data_source="no_data".
        # Conservative env-var rollback keeps the old code path available
        # while iterating; see module docstring for the taxonomy.
        use_web_search = (
            os.environ.get("AEGIS_SOS_FALLBACK_MODE", "prompt_only").strip().lower() == "web_search"
        )
        if use_web_search:
            prompt = _build_bedrock_prompt_web_search(name, state)
            invoke = client.invoke_with_web_search
        else:
            prompt = _build_bedrock_prompt_prompt_only(name, state)
            invoke = client.invoke_prompt_only

        try:
            raw = invoke(prompt)
        except Exception:
            _log.warning(
                "sos_checker.bedrock_invoke_failed business_name_hash=%s mode=%s",
                hash(name),
                "web_search" if use_web_search else "prompt_only",
                exc_info=True,
            )
            return SOSResult(
                found=False,
                status=None,
                entity_name=None,
                formation_date=None,
                is_active=None,
                data_source="bedrock_error",
                checked_at=now,
                error="bedrock_invoke_failed",
            )

        try:
            parsed = _parse_bedrock_response(raw)
        except (ValueError, json.JSONDecodeError):
            _log.warning(
                "sos_checker.bedrock_parse_failed raw=%r",
                raw[:200],
            )
            return SOSResult(
                found=False,
                status=None,
                entity_name=None,
                formation_date=None,
                is_active=None,
                data_source="bedrock_unparseable",
                checked_at=now,
                error="bedrock_parse_failed",
            )

        if not parsed.get("found", False):
            return SOSResult(
                found=False,
                status=None,
                entity_name=None,
                formation_date=None,
                is_active=None,
                data_source="bedrock_not_found",
                checked_at=now,
            )

        status_raw = _coerce_str(parsed.get("status"))
        return SOSResult(
            found=True,
            status=status_raw,
            entity_name=_coerce_str(parsed.get("entity_name")),
            formation_date=_coerce_str(parsed.get("formation_date")),
            is_active=_coerce_is_active(status_raw),
            data_source="bedrock",
            checked_at=now,
        )


def _row_to_result(row: sqlite3.Row, state: str, now: datetime, *, exact_match: bool) -> SOSResult:
    status = row["status"]
    return SOSResult(
        found=True,
        status=status,
        entity_name=row["business_name"],
        formation_date=row["formation_date"],
        is_active=_coerce_is_active(status),
        data_source=f"local_db:{state}",
        checked_at=now,
    )


_CODE_FENCE_OPEN = re.compile(r"^```(?:json)?\s*", flags=re.IGNORECASE)
_CODE_FENCE_CLOSE = re.compile(r"\s*```\s*$")


def _build_bedrock_prompt_web_search(name: str, state: str | None) -> str:
    target_state = state or "(unknown state — search nationally)"
    return f"""\
You have access to a web_search tool. Use AT MOST 3 web searches total.

Search the {target_state} Secretary of State business-entity registry
for: {name!r}.

Return ONE JSON object and nothing else. No prose, no code fence.

{{
  "found": true,
  "status": "<verbatim registry status, e.g. ACTIVE / DISSOLVED / INACTIVE / WITHDRAWN>",
  "entity_name": "<official entity name as recorded>",
  "formation_date": "<YYYY-MM-DD or null>"
}}

When no matching entity is located, return {{"found": false}}.

Search the official state SOS registry first (e.g. for FL try sunbiz.org).
Do not use third-party aggregator sites unless the official registry is
unreachable. Verify the entity matches the searched name closely — do
not return a different business that merely contains similar words."""


def _build_bedrock_prompt_prompt_only(name: str, state: str | None) -> str:
    """Prompt-only fallback used when the ``web_search`` tool is unavailable.

    The model answers from training-time knowledge with a strict JSON
    schema and is told to return ``{"found": false}`` whenever it cannot
    confidently identify the entity. That conservative default is what
    keeps an LLM-only lookup from manufacturing false positives.
    """
    target_state = state or "(unknown state)"
    return f"""\
Look up the Secretary of State business-entity registration for
{name!r} in {target_state}.

Return ONE JSON object and nothing else. No prose, no code fence.

{{
  "found": true,
  "status": "<one of: ACTIVE | INACTIVE | DISSOLVED | WITHDRAWN | NOT FOUND>",
  "entity_name": "<official entity name as recorded, or null>",
  "formation_date": "<YYYY-MM-DD or null>"
}}

When you cannot reliably confirm the entity exists (no training-time
knowledge, ambiguous name, multiple unrelated matches), return
{{"found": false}}.

Do NOT guess. Do NOT fabricate a formation date. Do NOT return a
different business that merely contains similar words. Verify the
entity matches the searched name closely; otherwise return found=false."""


def _parse_bedrock_response(raw: str) -> dict[str, object]:
    cleaned = (raw or "").strip()
    cleaned = _CODE_FENCE_OPEN.sub("", cleaned)
    cleaned = _CODE_FENCE_CLOSE.sub("", cleaned)
    data: object = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    # Pydantic-style narrowing without a model: every consumer reads
    # values with ``.get`` + ``_coerce_*``. The dict shape is locked
    # in by the prompt contract above.
    return data


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


__all__ = [
    "DEFAULT_DB_PATH",
    "SOSChecker",
    "SOSResult",
    "normalize_business_name",
]
