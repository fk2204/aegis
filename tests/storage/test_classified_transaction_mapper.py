"""Round-trip tests for the canonical ``transactions`` table mappers
(``aegis.storage._classified_to_db_row`` + ``aegis.storage._db_row_to_classified``).

The audit at ``docs/audit-mapper-divergence.md`` marks this pair as
"covered indirectly via ``_analysis_to_db_row`` test" — but the only
indirect coverage they get is the ``_build_analysis`` flow in
``tests/test_storage.py``, which doesn't exercise the classified mapper
directly. The note "if ``Transaction`` parent grows new fields they
would drop silently" (audit row 11) is exactly the bug class the
``DocumentRow`` chunk-B drop demonstrated — a model gains fields, the
hand-maintained mapper doesn't get extended, the in-memory backend
masks the drop in tests, the Supabase backend silently nulls the
columns in prod.

This file fills the direct round-trip + structural-coverage gap.

Asymmetry note: ``_classified_to_db_row`` takes
``(tx, document_id, merchant_id)`` — the latter two are NOT
``ClassifiedTransaction`` fields, they're injected from the calling
storage layer. The structural guard therefore walks
``ClassifiedTransaction.model_fields`` and asserts each one survives
write -> read; the extra db-row keys (``document_id`` / ``merchant_id``)
get their own per-field assertions below.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from aegis.parser.models import ClassifiedTransaction
from aegis.storage import _classified_to_db_row, _db_row_to_classified


def _full_classified_tx() -> ClassifiedTransaction:
    """A ``ClassifiedTransaction`` with EVERY field populated to a
    distinguishable non-default value. Used as the round-trip source.
    """
    return ClassifiedTransaction(
        id=uuid4(),
        posted_date=date(2026, 3, 15),
        description="MCA WITHDRAWAL OnDeck 12345",
        amount=Decimal("-123.45"),
        running_balance=Decimal("4567.89"),
        source_page=3,
        source_line=17,
        category="mca_debit",
        classification_confidence=92,
    )


def _round_trip(
    tx: ClassifiedTransaction,
    document_id: UUID | None = None,
    merchant_id: UUID | None = None,
) -> ClassifiedTransaction:
    document_id = document_id or uuid4()
    db_row = _classified_to_db_row(tx, document_id, merchant_id)
    return _db_row_to_classified(db_row)


# ----------------------------------------------------------------------
# Structural guard — "new field added to model but mapper forgotten"
# ----------------------------------------------------------------------


def test_classified_to_db_row_writes_every_field_on_classified_transaction() -> None:
    """STRUCTURAL GUARD (write side). Walk
    ``ClassifiedTransaction.model_fields`` (including inherited fields
    from ``Transaction``) and assert each one with a non-None source
    value lands in the db dict.

    This is the explicit defense against the audit's row-11 risk note —
    if a future commit adds a field to ``Transaction`` or
    ``ClassifiedTransaction``, the mapper extension MUST happen in the
    same commit or this test fails.
    """
    tx = _full_classified_tx()
    document_id = uuid4()
    merchant_id = uuid4()
    db_row = _classified_to_db_row(tx, document_id, merchant_id)

    declared_fields = set(ClassifiedTransaction.model_fields.keys())
    missing: list[str] = []
    for field_name in declared_fields:
        if field_name not in db_row:
            missing.append(field_name)
            continue
        if db_row[field_name] is None:
            # Mapper allowed to write None only when the source field is
            # None. Our fixture populates every field; nothing should
            # come out None here.
            missing.append(field_name)

    assert not missing, (
        f"Mapper _classified_to_db_row dropped fields: {missing!r}. "
        "Every populated ClassifiedTransaction field (including those "
        "inherited from Transaction) must land in the db dict — extend "
        "the mapper in src/aegis/storage.py."
    )


def test_db_row_to_classified_hydrates_every_field_on_classified_transaction() -> None:
    """STRUCTURAL GUARD (read side). Round-trip the full transaction
    through write -> read; every ``ClassifiedTransaction`` field whose
    write-side value was non-None must come back non-None on the
    rebuilt model.
    """
    tx = _full_classified_tx()
    document_id = uuid4()
    merchant_id = uuid4()
    db_row = _classified_to_db_row(tx, document_id, merchant_id)
    restored = _db_row_to_classified(db_row)

    declared_fields = set(ClassifiedTransaction.model_fields.keys())
    for field_name in declared_fields:
        write_value = db_row.get(field_name)
        if write_value is None:
            continue
        read_value = getattr(restored, field_name)
        assert read_value is not None, (
            f"Mapper _db_row_to_classified dropped field {field_name!r}: "
            f"db row carried {write_value!r} but rebuilt "
            "ClassifiedTransaction came out None. Same bug class as "
            "the chunk-B DocumentRow drop — update "
            "_db_row_to_classified in src/aegis/storage.py."
        )


# ----------------------------------------------------------------------
# Per-field round-trip — scalars on Transaction (the parent)
# ----------------------------------------------------------------------


def test_id_round_trips_as_uuid() -> None:
    tx = _full_classified_tx()
    restored = _round_trip(tx)
    assert isinstance(restored.id, UUID)
    assert restored.id == tx.id


def test_posted_date_round_trips_through_isoformat() -> None:
    tx = _full_classified_tx()
    db_row = _classified_to_db_row(tx, uuid4(), None)
    assert db_row["posted_date"] == "2026-03-15"
    restored = _db_row_to_classified(db_row)
    assert restored.posted_date == date(2026, 3, 15)


def test_description_round_trips_verbatim() -> None:
    """Transaction descriptions are PII but live in the DB by design
    (CLAUDE.md PII section). The mapper must not mutate them — no
    normalization, no truncation."""
    tx = _full_classified_tx()
    restored = _round_trip(tx)
    assert restored.description == "MCA WITHDRAWAL OnDeck 12345"


def test_amount_round_trips_as_exact_decimal() -> None:
    """Money math is Decimal; the str-serialize / Decimal-parse cycle
    must preserve precision exactly."""
    tx = _full_classified_tx()
    restored = _round_trip(tx)
    assert restored.amount == Decimal("-123.45")


def test_amount_negative_decimal_preserves_sign() -> None:
    """Withdrawals (mca_debit / nsf_fee / wire_out) are negative; the
    minus sign must survive str round-trip."""
    tx = _full_classified_tx()
    tx.amount = Decimal("-9999.99")
    restored = _round_trip(tx)
    assert restored.amount == Decimal("-9999.99")
    assert restored.amount < Decimal("0")


def test_running_balance_round_trips_as_decimal_when_present() -> None:
    tx = _full_classified_tx()
    restored = _round_trip(tx)
    assert restored.running_balance == Decimal("4567.89")


def test_running_balance_none_round_trips_as_none() -> None:
    """``running_balance`` is optional on ``Transaction``; many real
    statements don't carry per-row running balance. None must survive
    both legs of the round-trip without becoming the string
    ``"None"`` (which would happen if the mapper str-wrapped without
    guarding)."""
    tx = _full_classified_tx()
    tx.running_balance = None
    db_row = _classified_to_db_row(tx, uuid4(), None)
    assert db_row["running_balance"] is None
    restored = _db_row_to_classified(db_row)
    assert restored.running_balance is None


def test_source_page_and_source_line_round_trip_as_ints() -> None:
    """Every transaction MUST carry source_page + source_line for the
    audit drill-down — see CLAUDE.md auditability rule."""
    tx = _full_classified_tx()
    restored = _round_trip(tx)
    assert restored.source_page == 3
    assert restored.source_line == 17


# ----------------------------------------------------------------------
# Per-field round-trip — classification fields (the child)
# ----------------------------------------------------------------------


def test_category_round_trips_through_literal_check() -> None:
    tx = _full_classified_tx()
    restored = _round_trip(tx)
    assert restored.category == "mca_debit"


def test_classification_confidence_round_trips_as_int() -> None:
    tx = _full_classified_tx()
    restored = _round_trip(tx)
    assert restored.classification_confidence == 92


# ----------------------------------------------------------------------
# Mapper-injected db-row fields (NOT on ClassifiedTransaction)
# ----------------------------------------------------------------------


def test_document_id_injected_as_str_on_write() -> None:
    """``document_id`` is not on ``ClassifiedTransaction``; it's
    injected by the storage layer from the document context. Confirm
    the mapper stringifies it."""
    tx = _full_classified_tx()
    document_id = uuid4()
    db_row = _classified_to_db_row(tx, document_id, None)
    assert db_row["document_id"] == str(document_id)


def test_merchant_id_injected_as_str_or_none() -> None:
    """``merchant_id`` is nullable on the documents/transactions side
    (orphan upload). Confirm both branches: present -> str; None ->
    None (NOT the literal string ``"None"``)."""
    tx = _full_classified_tx()
    merchant_id = uuid4()

    with_merchant = _classified_to_db_row(tx, uuid4(), merchant_id)
    assert with_merchant["merchant_id"] == str(merchant_id)

    without_merchant = _classified_to_db_row(tx, uuid4(), None)
    assert without_merchant["merchant_id"] is None


# ----------------------------------------------------------------------
# Required-column behavior on read
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "required_key",
    [
        "id",
        "posted_date",
        "description",
        "amount",
        "source_page",
        "source_line",
        "category",
        "classification_confidence",
    ],
)
def test_missing_required_column_raises(required_key: str) -> None:
    """Truly-required columns (no default in mapper body) must raise
    rather than silently fall through to a default — same discipline as
    ``_row_to_document``'s handling of ``parse_status``. A schema bug
    that silently fills a default would mask real data drift between
    code and DB."""
    tx = _full_classified_tx()
    db_row = _classified_to_db_row(tx, uuid4(), None)
    del db_row[required_key]
    with pytest.raises(KeyError):
        _db_row_to_classified(db_row)
