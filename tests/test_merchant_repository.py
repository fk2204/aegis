"""Unit tests for the merchant-from-statement repository methods.

Migration 034 added three methods to ``MerchantRepository``:

* ``create_provisional()`` — INSERT a fresh ``status='provisional'``
  row with NULL business_name / owner_name / state. Used by the
  dashboard ``/ui/upload`` auto-create branch (chunk B).
* ``finalize_provisional(merchant_id, business_name)`` — transition
  ``provisional`` or ``needs_manual_naming`` to ``finalized``,
  setting business_name. Used by the worker at parse-completion.
  Returns the rowcount so the worker can gate its audit row on
  observed change.
* ``mark_needs_manual_naming(merchant_id)`` — transition
  ``provisional`` to ``needs_manual_naming``. Used by the worker
  on blank account_holder, parse exception, parse cancellation,
  and processor-branch success. Also rowcount-gated.

These tests cover the in-memory implementation. The Supabase
implementation has the same contract (status filters, rowcount
returns) but isn't exercised here — that's deferred to a live
integration test in chunk B once the worker calls land.

The rowcount-return-value-shape is operator-required: a false
audit row claiming a state change didn't happen is unacceptable.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from aegis.merchants.repository import (
    PROVISIONAL_BUSINESS_NAME_PLACEHOLDER,
    InMemoryMerchantRepository,
)


@pytest.fixture
def repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


# ---------------------------------------------------------------------------
# create_provisional
# ---------------------------------------------------------------------------


def test_create_provisional_returns_row_with_placeholder_name(
    repo: InMemoryMerchantRepository,
) -> None:
    """A fresh provisional row has status='provisional', a placeholder
    business_name (chosen so the slugify/dossier/sort cascade doesn't
    need None-guards), and NULL owner_name + state.

    The placeholder is intentional — see the model docstring + the
    PROVISIONAL_BUSINESS_NAME_PLACEHOLDER constant comment for why
    business_name is non-null at the type level.
    """
    m = repo.create_provisional()

    assert m.status == "provisional"
    assert m.is_provisional is True
    assert m.business_name == PROVISIONAL_BUSINESS_NAME_PLACEHOLDER
    assert m.owner_name is None
    assert m.state is None
    # created_at and updated_at are stamped at create time.
    assert m.created_at is not None
    assert m.updated_at is not None

    fetched = repo.get(m.id)
    assert fetched.id == m.id
    assert fetched.status == "provisional"
    assert fetched.business_name == PROVISIONAL_BUSINESS_NAME_PLACEHOLDER


def test_create_provisional_returns_distinct_ids_each_call(
    repo: InMemoryMerchantRepository,
) -> None:
    """Each call writes a new row — no accidental reuse. Pins this
    because a per-batch model means N batches → N provisionals."""
    a = repo.create_provisional()
    b = repo.create_provisional()
    assert a.id != b.id


# ---------------------------------------------------------------------------
# finalize_provisional — happy paths
# ---------------------------------------------------------------------------


def test_finalize_provisional_fills_name_and_returns_rowcount_one(
    repo: InMemoryMerchantRepository,
) -> None:
    """The happy path: worker calls finalize on a provisional row,
    business_name is set, status transitions to 'finalized', and the
    method returns 1 so the worker knows to write its audit row."""
    m = repo.create_provisional()

    n = repo.finalize_provisional(
        merchant_id=m.id, business_name="Acme LLC"
    )

    assert n == 1
    fetched = repo.get(m.id)
    assert fetched.status == "finalized"
    assert fetched.is_finalized is True
    assert fetched.business_name == "Acme LLC"
    # owner_name and state intentionally untouched — operator edits
    # later via the existing affordance.
    assert fetched.owner_name is None
    assert fetched.state is None


def test_finalize_provisional_also_lifts_needs_manual_naming(
    repo: InMemoryMerchantRepository,
) -> None:
    """A row marked needs_manual_naming (e.g. because the first parse
    couldn't extract a name) can later be lifted to finalized when a
    subsequent re-parse DOES extract one. Same UPDATE path; same
    rowcount=1; same audit shape from the worker's perspective."""
    m = repo.create_provisional()
    repo.mark_needs_manual_naming(merchant_id=m.id)
    assert repo.get(m.id).status == "needs_manual_naming"

    n = repo.finalize_provisional(
        merchant_id=m.id, business_name="Acme LLC"
    )

    assert n == 1
    assert repo.get(m.id).status == "finalized"


# ---------------------------------------------------------------------------
# finalize_provisional — idempotency (the operator-required gate)
# ---------------------------------------------------------------------------


def test_finalize_provisional_already_finalized_returns_zero(
    repo: InMemoryMerchantRepository,
) -> None:
    """If the operator manually finalized the merchant between upload
    and parse-completion, the worker's finalize call MUST NOT
    overwrite the operator's value. Rowcount returns 0 so the worker
    knows to skip its audit row."""
    m = repo.create_provisional()
    # First finalize: by 'operator' with one name.
    repo.finalize_provisional(merchant_id=m.id, business_name="Operator Name")
    # Worker arrives later with a different name from the statement.
    n = repo.finalize_provisional(
        merchant_id=m.id, business_name="Statement Name"
    )

    assert n == 0
    # The operator's name survives.
    assert repo.get(m.id).business_name == "Operator Name"


def test_finalize_provisional_unknown_merchant_returns_zero(
    repo: InMemoryMerchantRepository,
) -> None:
    """A merchant_id that doesn't exist returns 0 without raising.
    Worker uses this to silently skip the finalize step when its
    document's merchant got deleted in between (rare; happens in
    test sweeps + future delete flows). NO audit row, NO crash."""
    n = repo.finalize_provisional(
        merchant_id=uuid4(), business_name="Acme LLC"
    )
    assert n == 0


# ---------------------------------------------------------------------------
# mark_needs_manual_naming — happy + idempotency
# ---------------------------------------------------------------------------


def test_mark_needs_manual_naming_transitions_and_returns_one(
    repo: InMemoryMerchantRepository,
) -> None:
    """Worker call from the blank-name / parse-failure path: provisional
    row transitions to needs_manual_naming, rowcount=1, audit fires."""
    m = repo.create_provisional()

    n = repo.mark_needs_manual_naming(merchant_id=m.id)

    assert n == 1
    fetched = repo.get(m.id)
    assert fetched.status == "needs_manual_naming"
    assert fetched.needs_manual_naming is True
    # The placeholder business_name SURVIVES — mark_needs_manual_naming
    # only changes status. Operator will overwrite it via intake.
    assert fetched.business_name == PROVISIONAL_BUSINESS_NAME_PLACEHOLDER


def test_mark_needs_manual_naming_already_finalized_returns_zero(
    repo: InMemoryMerchantRepository,
) -> None:
    """The fail-after-success race: parse completed and worker
    transitioned to finalized; a late-arriving processor-branch retry
    or sibling failure tries to mark needs_manual_naming. The status
    filter rejects (rowcount 0); finalized merchant stays finalized."""
    m = repo.create_provisional()
    repo.finalize_provisional(merchant_id=m.id, business_name="Acme LLC")

    n = repo.mark_needs_manual_naming(merchant_id=m.id)

    assert n == 0
    assert repo.get(m.id).status == "finalized"
    assert repo.get(m.id).business_name == "Acme LLC"


def test_mark_needs_manual_naming_already_needs_naming_returns_zero(
    repo: InMemoryMerchantRepository,
) -> None:
    """Doubly-failing parse: first failure marked the merchant
    needs_manual_naming; a sibling parse failure for the same merchant
    arrives later and tries to mark again. Status filter rejects;
    no duplicate audit row from the worker."""
    m = repo.create_provisional()
    repo.mark_needs_manual_naming(merchant_id=m.id)

    n = repo.mark_needs_manual_naming(merchant_id=m.id)

    assert n == 0
    assert repo.get(m.id).status == "needs_manual_naming"


def test_mark_needs_manual_naming_unknown_merchant_returns_zero(
    repo: InMemoryMerchantRepository,
) -> None:
    """Same shape as the finalize unknown-id test — the worker's
    failure-path call should not crash if the merchant has been
    deleted. NO audit row, NO crash."""
    n = repo.mark_needs_manual_naming(merchant_id=uuid4())
    assert n == 0


# ---------------------------------------------------------------------------
# Cross-method invariants
# ---------------------------------------------------------------------------


def test_provisional_create_then_finalize_full_round_trip(
    repo: InMemoryMerchantRepository,
) -> None:
    """End-to-end of the happy path: create_provisional → worker
    completes parse with a clean account_holder → finalize_provisional.
    Final state is what the dashboard renders as a normal merchant."""
    m = repo.create_provisional()
    n = repo.finalize_provisional(merchant_id=m.id, business_name="Acme LLC")

    assert n == 1
    final = repo.get(m.id)
    assert final.status == "finalized"
    assert final.business_name == "Acme LLC"
    assert final.is_finalized is True
    assert final.is_provisional is False
    assert final.needs_manual_naming is False


def test_provisional_create_then_mark_needs_manual_naming_round_trip(
    repo: InMemoryMerchantRepository,
) -> None:
    """End-to-end of the blank-name path: create_provisional → worker
    sees blank account_holder → mark_needs_manual_naming. Dashboard
    surfaces with the yellow chip + intake button."""
    m = repo.create_provisional()
    n = repo.mark_needs_manual_naming(merchant_id=m.id)

    assert n == 1
    final = repo.get(m.id)
    assert final.status == "needs_manual_naming"
    assert final.needs_manual_naming is True
    assert final.business_name == PROVISIONAL_BUSINESS_NAME_PLACEHOLDER
