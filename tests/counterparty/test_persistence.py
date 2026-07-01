"""Tests for ``aegis.counterparty.persistence.persist_classifications``.

Covers the two guarantees that keep operator overrides from being
clobbered by the next scoring pass:

  * Rows flagged ``counterparty_overridden=TRUE`` are skipped even
    when the classifier produced a different result. (2026-06 fix.)
  * Empty classifications map = noop (no Supabase calls).

The Supabase client is stubbed via a hand-rolled fake so we don't
depend on the network or on ``supabase-py``'s exact API version.
Follows the same pattern as ``tests/counterparty/test_classifier_real_vu.py``.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from aegis.counterparty.models import CounterpartyClassification
from aegis.counterparty.persistence import persist_classifications


class _FakeQuery:
    """Minimal chainable stand-in for the Supabase query builder.

    Records every call. ``execute`` returns a namespace with ``data``
    populated by whichever recorded route the fixture set up.
    """

    def __init__(self, parent: _FakeSupabase, table: str) -> None:
        self.parent = parent
        self.table_name = table
        self._filters: list[tuple[str, Any, Any]] = []
        self._selected: str | None = None
        self._upserted: list[dict[str, Any]] | None = None
        self._on_conflict: str | None = None
        self._updated_payload: dict[str, Any] | None = None

    def select(self, cols: str) -> _FakeQuery:
        self._selected = cols
        return self

    def in_(self, col: str, values: list[Any]) -> _FakeQuery:
        self._filters.append(("in_", col, list(values)))
        return self

    def eq(self, col: str, value: Any) -> _FakeQuery:
        self._filters.append(("eq", col, value))
        return self

    def upsert(self, rows: list[dict[str, Any]], on_conflict: str | None = None) -> _FakeQuery:
        self._upserted = list(rows)
        self._on_conflict = on_conflict
        return self

    def update(self, payload: dict[str, Any]) -> _FakeQuery:
        self._updated_payload = dict(payload)
        return self

    def execute(self) -> Any:
        self.parent.executed.append(self)

        class _Result:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data

        if self._upserted is not None or self._updated_payload is not None:
            return _Result([])
        # SELECT path — return whatever overridden ids the parent staged.
        return _Result(self.parent.overridden_data)


class _FakeSupabase:
    def __init__(self, overridden_ids: list[str]) -> None:
        self.overridden_data: list[dict[str, str]] = [{"id": i} for i in overridden_ids]
        self.executed: list[_FakeQuery] = []

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self, name)


def test_persist_empty_map_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _boom() -> Any:
        nonlocal called
        called = True
        raise AssertionError("get_supabase should not be called for empty map")

    monkeypatch.setattr("aegis.counterparty.persistence.get_supabase", _boom)
    n = persist_classifications({})
    assert n == 0
    assert called is False


def test_persist_skips_overridden_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    txn_kept = uuid4()
    txn_overridden = uuid4()

    fake = _FakeSupabase(overridden_ids=[str(txn_overridden)])
    monkeypatch.setattr("aegis.counterparty.persistence.get_supabase", lambda: fake)

    classifications = {
        txn_kept: CounterpartyClassification(
            transaction_id=txn_kept,
            counterparty="processor",
            confidence=90,
            reason="stripe_ach",
        ),
        txn_overridden: CounterpartyClassification(
            transaction_id=txn_overridden,
            counterparty="processor",
            confidence=90,
            reason="stripe_ach",
        ),
    }

    n = persist_classifications(classifications)
    assert n == 1

    update_calls = [q for q in fake.executed if q._updated_payload is not None]
    assert len(update_calls) == 1
    # The .in_("id", …) filter carries the kept id and NOT the overridden one.
    id_filters = [f for q in update_calls for f in q._filters if f[0] == "in_" and f[1] == "id"]
    assert id_filters, "expected .in_('id', ...) filter"
    all_ids = {i for _, _, ids in id_filters for i in ids}
    assert str(txn_kept) in all_ids
    assert str(txn_overridden) not in all_ids


def test_persist_returns_zero_when_supabase_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> Any:
        raise RuntimeError("no supabase in test env")

    monkeypatch.setattr("aegis.counterparty.persistence.get_supabase", _boom)
    txn = uuid4()
    n = persist_classifications(
        {
            txn: CounterpartyClassification(
                transaction_id=txn,
                counterparty="processor",
                confidence=90,
                reason="stripe_ach",
            )
        }
    )
    assert n == 0
