"""BankLayoutRepository — Protocol + in-memory + Supabase impls.

Mirrors the two-impl pattern of ``aegis.funder_note_submissions.repository``.
The bank-layout learning surface has two writers:

  * ``upsert_success`` — called from the parser pipeline on every
    successful parse. Atomic INSERT-or-UPDATE; bumps
    ``successful_parses``, stamps ``last_seen = NOW()``, merges the
    fingerprint dict (new keys win).
  * ``set_hints`` — called from the operator UI router. Edits only
    ``extraction_hints``; never touches ``successful_parses`` or
    ``last_seen``. Creates a primed row (parses=0) when the operator
    pre-seeds a bank before any parse runs.

``get_hints`` is the ONE read the pipeline performs at extraction time.
Threshold gating lives here (not in the pipeline) so a future tune is a
single ``HINTS_AVAILABLE_THRESHOLD`` edit. Lookups are case-insensitive
at the boundary (Chase / CHASE / chase all match the same row) because
the bank name string the LLM emits varies in case across exports while
the operator stores the canonical-cased display string.

Write failures raise ``BankLayoutWriteError`` so the calling pipeline
can refuse to record the learning (mirrors ``AuditWriteError`` /
``FunderNoteSubmissionWriteError`` semantics from CLAUDE.md
Auditability). The pipeline injection point swallows the error rather
than failing the parse: learning is best-effort, not a parse gate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, Protocol, cast
from uuid import UUID

from aegis.bank_layouts.models import BankLayoutRow
from aegis.db import get_supabase
from aegis.logger import get_logger

_log = get_logger(__name__)


# Threshold for ``get_hints``: a bank needs at least this many successful
# parses before its operator-authored hints feed the extraction prompt.
# Defined as a module constant so a future tune (e.g. operator wants
# hints to take effect after 5 parses) is one edit, not a code search.
HINTS_AVAILABLE_THRESHOLD: Final[int] = 3


class BankLayoutWriteError(RuntimeError):
    """Raised when a bank_layouts row could not be persisted."""


class BankLayoutRepository(Protocol):
    def find_by_bank_name(self, bank_name: str) -> BankLayoutRow | None: ...

    def upsert_success(
        self,
        *,
        bank_name: str,
        fingerprint: dict[str, Any],
    ) -> BankLayoutRow: ...

    def get_hints(self, bank_name: str) -> str | None: ...

    def set_hints(self, *, bank_name: str, hints: str) -> BankLayoutRow: ...

    def list_all(self) -> list[BankLayoutRow]: ...


def _hints_available(row: BankLayoutRow) -> bool:
    """Return True when ``row.extraction_hints`` should feed the prompt.

    Single source of truth for the threshold gate. Both backends call
    through here so the in-memory + Supabase reads can never drift.
    Empty / whitespace-only hints are treated as absent regardless of
    parse count — a primed row with parses=0 trivially fails the gate;
    a parsed row with empty hints would otherwise inject a useless
    header into the prompt.
    """
    if row.successful_parses < HINTS_AVAILABLE_THRESHOLD:
        return False
    if row.extraction_hints is None:
        return False
    return bool(row.extraction_hints.strip())


class InMemoryBankLayoutRepository:
    """Dict-backed bank-layout store. Tests + offline."""

    def __init__(self) -> None:
        # Keyed by lowercased bank_name so the case-insensitive lookup
        # is O(1) and matches the Supabase backend's LOWER(bank_name)
        # query semantics. The stored row retains the operator's
        # original casing on ``bank_name``.
        self._by_lower: dict[str, BankLayoutRow] = {}

    def find_by_bank_name(self, bank_name: str) -> BankLayoutRow | None:
        return self._by_lower.get(bank_name.strip().lower())

    def upsert_success(
        self,
        *,
        bank_name: str,
        fingerprint: dict[str, Any],
    ) -> BankLayoutRow:
        now = datetime.now(UTC)
        key = bank_name.strip().lower()
        current = self._by_lower.get(key)
        if current is None:
            row = BankLayoutRow(
                bank_name=bank_name.strip(),
                layout_fingerprint=dict(fingerprint),
                successful_parses=1,
                last_seen=now,
                created_at=now,
            )
            self._by_lower[key] = row
            return row
        # Merge: new keys win (dict union with `fingerprint` on the right).
        merged = {**current.layout_fingerprint, **fingerprint}
        updated = current.model_copy(
            update={
                "successful_parses": current.successful_parses + 1,
                "last_seen": now,
                "layout_fingerprint": merged,
            }
        )
        self._by_lower[key] = updated
        return updated

    def get_hints(self, bank_name: str) -> str | None:
        row = self.find_by_bank_name(bank_name)
        if row is None:
            return None
        if not _hints_available(row):
            return None
        # ``_hints_available`` already verified non-None + non-empty.
        # ``row.extraction_hints or None`` is a no-op idiom that keeps
        # mypy from rejecting the ``str | None`` return type without
        # branching on a state the gate has already eliminated.
        return row.extraction_hints or None

    def set_hints(self, *, bank_name: str, hints: str) -> BankLayoutRow:
        key = bank_name.strip().lower()
        # Empty string clears the hints; mirror the Supabase backend.
        normalized = hints.strip() or None
        current = self._by_lower.get(key)
        if current is None:
            now = datetime.now(UTC)
            row = BankLayoutRow(
                bank_name=bank_name.strip(),
                extraction_hints=normalized,
                successful_parses=0,
                created_at=now,
            )
            self._by_lower[key] = row
            return row
        updated = current.model_copy(update={"extraction_hints": normalized})
        self._by_lower[key] = updated
        return updated

    def list_all(self) -> list[BankLayoutRow]:
        rows = list(self._by_lower.values())
        # Newest-seen first; rows with last_seen=None sort to the
        # bottom (primed but never parsed). datetime.min as the sort
        # key keeps the comparator total without branching.
        rows.sort(
            key=lambda r: r.last_seen or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return rows


class SupabaseBankLayoutRepository:
    """Persistence backed by Postgres ``bank_layouts`` (mig 059)."""

    def find_by_bank_name(self, bank_name: str) -> BankLayoutRow | None:
        # ``ilike`` with the bank_name as-is is the case-insensitive
        # equivalent of ``LOWER(bank_name) = LOWER(?)`` and works with
        # the supabase-py client (which doesn't expose raw SQL on the
        # ``.table()`` builder). Special characters in the operator-
        # typed bank name need no escaping because we anchor with the
        # full string — there's no wildcard expansion to abuse.
        result = (
            get_supabase()
            .table("bank_layouts")
            .select("*")
            .ilike("bank_name", bank_name.strip())
            .limit(1)
            .execute()
        )
        rows = cast(list[dict[str, Any]], result.data or [])
        if not rows:
            return None
        return _row_to_bank_layout(rows[0])

    def upsert_success(
        self,
        *,
        bank_name: str,
        fingerprint: dict[str, Any],
    ) -> BankLayoutRow:
        existing = self.find_by_bank_name(bank_name)
        now = datetime.now(UTC)
        if existing is None:
            payload: dict[str, Any] = {
                "bank_name": bank_name.strip(),
                "layout_fingerprint": dict(fingerprint),
                "successful_parses": 1,
                "last_seen": now.isoformat(),
            }
            try:
                result = get_supabase().table("bank_layouts").insert(payload).execute()
            except Exception as exc:
                _log.error(
                    "bank_layouts.insert_failed bank_name=%s",
                    bank_name,
                )
                raise BankLayoutWriteError(
                    f"failed to insert bank_layouts row for {bank_name!r}"
                ) from exc
            inserted = cast(list[dict[str, Any]], result.data or [])
            if not inserted:
                raise BankLayoutWriteError("supabase insert returned no row for bank_layouts")
            return _row_to_bank_layout(inserted[0])

        # UPDATE branch — merge fingerprint dict client-side so the
        # write is a single round-trip. Supabase-py doesn't expose
        # ``jsonb || jsonb`` directly through the table builder; the
        # client-side merge has the same "new keys win" semantics as
        # the planned ``layout_fingerprint || fingerprint::jsonb``
        # server-side expression.
        merged = {**existing.layout_fingerprint, **fingerprint}
        update: dict[str, Any] = {
            "successful_parses": existing.successful_parses + 1,
            "last_seen": now.isoformat(),
            "layout_fingerprint": merged,
        }
        try:
            result = (
                get_supabase()
                .table("bank_layouts")
                .update(update)
                .eq("id", str(existing.id))
                .execute()
            )
        except Exception as exc:
            _log.error(
                "bank_layouts.update_failed bank_name=%s",
                bank_name,
            )
            raise BankLayoutWriteError(
                f"failed to update bank_layouts row for {bank_name!r}"
            ) from exc
        updated_rows = cast(list[dict[str, Any]], result.data or [])
        if not updated_rows:
            raise BankLayoutWriteError(
                f"supabase update returned no row for bank_layouts {existing.id}"
            )
        return _row_to_bank_layout(updated_rows[0])

    def get_hints(self, bank_name: str) -> str | None:
        row = self.find_by_bank_name(bank_name)
        if row is None:
            return None
        if not _hints_available(row):
            return None
        # ``_hints_available`` already verified non-None + non-empty.
        return row.extraction_hints or None

    def set_hints(self, *, bank_name: str, hints: str) -> BankLayoutRow:
        normalized: str | None = hints.strip() or None
        existing = self.find_by_bank_name(bank_name)
        if existing is None:
            payload: dict[str, Any] = {
                "bank_name": bank_name.strip(),
                "extraction_hints": normalized,
                "successful_parses": 0,
            }
            try:
                result = get_supabase().table("bank_layouts").insert(payload).execute()
            except Exception as exc:
                _log.error(
                    "bank_layouts.insert_failed bank_name=%s",
                    bank_name,
                )
                raise BankLayoutWriteError(
                    f"failed to insert bank_layouts row for {bank_name!r}"
                ) from exc
            inserted = cast(list[dict[str, Any]], result.data or [])
            if not inserted:
                raise BankLayoutWriteError("supabase insert returned no row for bank_layouts")
            return _row_to_bank_layout(inserted[0])

        try:
            result = (
                get_supabase()
                .table("bank_layouts")
                .update({"extraction_hints": normalized})
                .eq("id", str(existing.id))
                .execute()
            )
        except Exception as exc:
            _log.error(
                "bank_layouts.update_failed bank_name=%s",
                bank_name,
            )
            raise BankLayoutWriteError(
                f"failed to update bank_layouts row for {bank_name!r}"
            ) from exc
        updated_rows = cast(list[dict[str, Any]], result.data or [])
        if not updated_rows:
            raise BankLayoutWriteError(
                f"supabase update returned no row for bank_layouts {existing.id}"
            )
        return _row_to_bank_layout(updated_rows[0])

    def list_all(self) -> list[BankLayoutRow]:
        # NULLS LAST on last_seen DESC: operator-primed rows (never
        # parsed) sort to the bottom so the dashboard's primary
        # scanning order is "most-recently-learned-from first."
        result = (
            get_supabase()
            .table("bank_layouts")
            .select("*")
            .order("last_seen", desc=True, nullsfirst=False)
            .execute()
        )
        return [_row_to_bank_layout(cast(dict[str, Any], r)) for r in (result.data or [])]


# ---------------------------------------------------------------------------
# Row decoders
# ---------------------------------------------------------------------------


def _row_to_bank_layout(row: dict[str, Any]) -> BankLayoutRow:
    """Decode a Postgres row dict to a BankLayoutRow.

    JSONB ``layout_fingerprint`` round-trips as a dict via supabase-py.
    Timestamp columns round-trip as either ``datetime`` (when the
    underlying driver decodes them) or ISO 8601 ``str`` (when the
    Supabase REST client passes the raw JSON through). _parse_dt
    handles both shapes.
    """
    fingerprint_raw = row.get("layout_fingerprint") or {}
    if not isinstance(fingerprint_raw, dict):
        # Defensive: a future schema change that swaps the JSONB to a
        # different shape should fail loud rather than silently corrupt
        # the row's typing.
        raise BankLayoutWriteError(
            f"bank_layouts row has non-dict layout_fingerprint: {type(fingerprint_raw).__name__}"
        )
    return BankLayoutRow(
        id=UUID(row["id"]),
        bank_name=row["bank_name"],
        layout_fingerprint=dict(fingerprint_raw),
        successful_parses=int(row.get("successful_parses") or 0),
        extraction_hints=row.get("extraction_hints"),
        last_seen=_parse_dt(row.get("last_seen")),
        created_at=_parse_dt(row.get("created_at")),
    )


def _parse_dt(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


__all__ = [
    "HINTS_AVAILABLE_THRESHOLD",
    "BankLayoutRepository",
    "BankLayoutWriteError",
    "InMemoryBankLayoutRepository",
    "SupabaseBankLayoutRepository",
]
