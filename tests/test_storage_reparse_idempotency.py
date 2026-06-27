"""Reparse-idempotency tests for ``persist_parse_result`` + ``upload``.

Background — the 2026-06-27 reparse-sealed-manual-review flood hit the
production worker on EVERY re-enqueued document because:

  1. ``storage.SupabaseDocumentRepository.persist_parse_result`` was
     INSERT-only on ``analyses`` — second parse against the same
     ``document_id`` raised
     ``duplicate key value violates unique constraint
     "analyses_document_id_key"``.
  2. ``storage.SupabaseDocumentRepository.persist_parse_result`` was
     INSERT-only on ``transactions`` — second parse appended duplicate
     rows. No UNIQUE constraint there, so the bug surfaces as data
     corruption rather than a 23505.
  3. ``storage_objects._SupabaseStorageBackend.upload`` did not pass
     ``upsert: "true"`` so Supabase Storage rejected the second blob
     write with a 409 ``Duplicate``. Surfaced as
     ``pdf_store.storage_upload_failed`` in the worker log.

These tests lock in the fix for all three call sites. The InMemory
backend was already idempotent (dict re-assignment), but we add a
regression test so a future "optimise by skipping the re-write"
refactor doesn't quietly reintroduce the divergence.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from aegis import storage as storage_module
from aegis import storage_objects
from aegis.parser.metadata import MetadataAnalysis
from aegis.parser.models import (
    Aggregates,
    ClassifiedTransaction,
    ExtractedStatement,
    StatementSummary,
    ValidationResult,
    _SourcedInt,
    _SourcedMoney,
)
from aegis.parser.pipeline import PipelineResult
from aegis.storage import (
    InMemoryDocumentRepository,
    SupabaseDocumentRepository,
)
from aegis.storage_objects import (
    StorageError,
    _SupabaseStorageBackend,
)


def _sm(value: str, ids: list[Any] | None = None) -> _SourcedMoney:
    return _SourcedMoney(value=Decimal(value), source_ids=ids or [])


def _si(value: int, ids: list[Any] | None = None) -> _SourcedInt:
    return _SourcedInt(value=value, source_ids=ids or [])


def _pipeline_result(*, num_txns: int = 1) -> PipelineResult:
    """Minimal viable PipelineResult shaped like the existing fixture
    in tests/test_storage.py — we duplicate the helper rather than
    cross-import so this test file stays self-contained.
    """
    classified = [
        ClassifiedTransaction(
            id=uuid4(),
            posted_date=date(2026, 1, 5 + i),
            description="DEPOSIT",
            amount=Decimal("1000.00"),
            running_balance=Decimal("4000.00") + Decimal("1000.00") * i,
            source_page=1,
            source_line=10 + i,
            category="deposit",
            classification_confidence=95,
        )
        for i in range(num_txns)
    ]
    tx_ids = [t.id for t in classified]
    total = Decimal("1000.00") * num_txns
    summary = StatementSummary(
        beginning_balance=Decimal("1000.00"),
        ending_balance=Decimal("1000.00") + total,
        deposit_total=total,
        withdrawal_total=Decimal("0.00"),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
    )
    aggregates = Aggregates(
        avg_daily_balance=_sm("1500.00", tx_ids),
        true_revenue=_sm(str(total), tx_ids),
        num_nsf=_si(0),
        days_negative=_si(0),
        debt_to_revenue=Decimal("0.00"),
        mca_daily_total=_sm("0.00"),
    )
    extraction_stub: Any = type(
        "Stub",
        (),
        {"statement": ExtractedStatement(summary=summary, transactions=classified)},
    )()
    return PipelineResult(
        parse_status="proceed",
        metadata=MetadataAnalysis(
            pdf_creation_date=None,
            pdf_modification_date=None,
            pdf_producer=None,
            pdf_creator=None,
            pdf_author=None,
            page_count=2,
            file_size_bytes=10240,
            eof_markers=1,
            page_sizes=["LETTER"],
            flags=[],
            fraud_score=0,
        ),
        extraction=extraction_stub,
        validation=ValidationResult(passed=True),
        classified=classified,
        patterns=None,
        aggregates=aggregates,
        fraud_score=10,
        fraud_score_breakdown={"metadata_score": 0, "math_score": 0, "patterns_score": 0},
        all_flags=[],
    )


# ---------------------------------------------------------------------------
# Test 1 — InMemory backend is idempotent across re-parses (regression).
# ---------------------------------------------------------------------------


def test_inmemory_persist_parse_result_is_idempotent_on_reparse() -> None:
    """The dict-assignment semantics of the in-memory backend make it
    idempotent — lock that in so a refactor that switches to
    ``setdefault`` or ``insert``-only style fails this test instead of
    regressing prod."""
    repo = InMemoryDocumentRepository()
    doc = repo.create_document(file_hash="f" * 64, byte_size=1000, original_filename="x.pdf")

    # First parse — 2 txns.
    repo.persist_parse_result(doc.id, result=_pipeline_result(num_txns=2))
    assert len(repo.list_transactions(doc.id)) == 2
    first_analysis = repo.get_analysis(doc.id)
    assert first_analysis is not None

    # Second parse against the same document_id — must NOT raise. The
    # in-memory backend collapses to "latest write wins" for both
    # transactions and analyses.
    repo.persist_parse_result(doc.id, result=_pipeline_result(num_txns=3))

    # Transactions REPLACED, not appended.
    assert len(repo.list_transactions(doc.id)) == 3

    # Analysis row's source_ids reflect the SECOND parse's transaction
    # IDs — proves the row was updated, not stale.
    second_analysis = repo.get_analysis(doc.id)
    assert second_analysis is not None
    second_tx_ids = {t.id for t in repo.list_transactions(doc.id)}
    assert set(second_analysis.true_revenue_source_ids) == second_tx_ids


# ---------------------------------------------------------------------------
# Test 2 — Supabase backend issues DELETE-then-INSERT on transactions
#          and UPSERT(on_conflict='document_id') on analyses.
#
# We don't hit a real Supabase — we mock ``get_supabase()`` and record
# the calls made against the fake client. The contract being tested is
# *the sequence of API calls*, not the Postgres behavior (which we
# trust Supabase for once we've issued the right call shape).
# ---------------------------------------------------------------------------


class _RecordedCall:
    def __init__(
        self,
        table: str,
        op: str,
        payload: Any = None,
        on_conflict: str | None = None,
    ) -> None:
        self.table = table
        self.op = op  # "insert" | "upsert" | "delete" | "update" | "eq" | "execute"
        self.payload = payload
        self.on_conflict = on_conflict


class _FakeChain:
    """Records every method call so the test can assert ordering."""

    def __init__(
        self,
        table: str,
        calls: list[_RecordedCall],
        canned_select_data: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._table = table
        self._calls = calls
        self._canned = canned_select_data or {}

    def select(self, cols: str) -> _FakeChain:
        self._calls.append(_RecordedCall(self._table, "select", cols))
        return self

    def insert(self, payload: Any) -> _FakeChain:
        self._calls.append(_RecordedCall(self._table, "insert", payload))
        return self

    def upsert(self, payload: Any, *, on_conflict: str | None = None) -> _FakeChain:
        self._calls.append(_RecordedCall(self._table, "upsert", payload, on_conflict=on_conflict))
        return self

    def delete(self) -> _FakeChain:
        self._calls.append(_RecordedCall(self._table, "delete"))
        return self

    def update(self, payload: Any) -> _FakeChain:
        self._calls.append(_RecordedCall(self._table, "update", payload))
        return self

    def eq(self, col: str, value: Any) -> _FakeChain:
        self._calls.append(_RecordedCall(self._table, "eq", {col: value}))
        return self

    def limit(self, n: int) -> _FakeChain:
        self._calls.append(_RecordedCall(self._table, "limit", n))
        return self

    def execute(self) -> Any:
        self._calls.append(_RecordedCall(self._table, "execute"))

        canned = self._canned.get(self._table, [])

        class _Result:
            def __init__(self, data: list[dict[str, Any]]) -> None:
                self.data = data

        return _Result(canned)


class _FakeClient:
    def __init__(
        self,
        canned_select_data: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.calls: list[_RecordedCall] = []
        self._canned = canned_select_data or {}

    def table(self, name: str) -> _FakeChain:
        return _FakeChain(name, self.calls, self._canned)


def test_supabase_persist_parse_result_deletes_txns_then_upserts_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-parse path: prior transactions deleted by document_id before
    new ones inserted; analyses written via .upsert(on_conflict='document_id')
    so the unique-key collision can't fire."""
    fake = _FakeClient()
    monkeypatch.setattr(storage_module, "get_supabase", lambda: fake)

    repo = SupabaseDocumentRepository()
    doc_id = uuid4()
    result = _pipeline_result(num_txns=2)
    repo.persist_parse_result(doc_id, result=result)

    # Pull every call against the 'transactions' table in order.
    tx_calls = [c for c in fake.calls if c.table == "transactions"]
    ops = [c.op for c in tx_calls]
    # Expected: delete → eq(document_id=...) → execute → insert → execute
    assert ops == ["delete", "eq", "execute", "insert", "execute"], (
        f"transactions call sequence wrong: {ops}"
    )
    # The eq filter targets document_id with the stringified UUID.
    assert tx_calls[1].payload == {"document_id": str(doc_id)}

    # analyses must:
    #  1) SELECT existing id by document_id (FK-preservation lookup), then
    #  2) UPSERT on document_id conflict — never bare INSERT.
    an_calls = [c for c in fake.calls if c.table == "analyses"]
    ops = [c.op for c in an_calls]
    assert "insert" not in ops, f"analyses must NOT use insert(): {ops}"
    # First operation is the id-lookup SELECT (preserves analyses.id
    # across reparses so the decisions.analysis_id FK stays valid).
    assert ops[0] == "select", f"analyses first op must be id-lookup select: {ops}"
    assert "upsert" in ops, f"analyses must include upsert: {ops}"
    upsert_call = next(c for c in an_calls if c.op == "upsert")
    assert upsert_call.on_conflict == "document_id", (
        f"analyses upsert on_conflict must be document_id, got: {upsert_call.on_conflict!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — storage_objects._SupabaseStorageBackend.upload passes
#          upsert: "true" so re-uploads of the pdf_store ciphertext
#          don't 409 on the second parse.
# ---------------------------------------------------------------------------


def test_supabase_storage_upload_passes_upsert_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _BucketStub:
        def upload(
            self,
            *,
            path: str,
            file: bytes,
            file_options: dict[str, str],
        ) -> dict[str, Any]:
            captured["path"] = path
            captured["file"] = file
            captured["file_options"] = file_options
            return {}

    class _StorageStub:
        def from_(self, bucket: str) -> _BucketStub:
            captured["bucket"] = bucket
            return _BucketStub()

    class _ApiStub:
        storage = _StorageStub()

    backend = _SupabaseStorageBackend()
    backend._api = lambda: _ApiStub()  # type: ignore[method-assign]
    monkeypatch.setattr(storage_objects, "_bucket", lambda: "documents-test")

    backend.upload("pdf_store/abc.pdf.enc", b"ciphertext-bytes")

    assert captured["bucket"] == "documents-test"
    assert captured["path"] == "pdf_store/abc.pdf.enc"
    assert captured["file"] == b"ciphertext-bytes"
    # The bug fix: the upsert flag MUST be in file_options. Without it
    # Supabase Storage 409s on the second upload of a pdf_store row
    # (every re-parse of a sealed manual_review doc).
    assert captured["file_options"].get("upsert") == "true", (
        f"upload() must pass upsert: 'true' so re-parses overwrite "
        f"the prior ciphertext; got file_options={captured['file_options']!r}"
    )
    # Content-type still set (regression guard so the fix doesn't
    # silently drop the previously-required header).
    assert captured["file_options"].get("content-type") == "application/octet-stream"


def test_supabase_storage_upload_raises_storageerror_on_backend_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: backend exceptions still map to StorageError. The
    upsert flag flip must not change the error contract."""

    class _BucketStub:
        def upload(self, **kwargs: Any) -> None:
            raise RuntimeError("network down")

    class _StorageStub:
        def from_(self, bucket: str) -> _BucketStub:
            del bucket
            return _BucketStub()

    class _ApiStub:
        storage = _StorageStub()

    backend = _SupabaseStorageBackend()
    backend._api = lambda: _ApiStub()  # type: ignore[method-assign]
    monkeypatch.setattr(storage_objects, "_bucket", lambda: "documents-test")

    with pytest.raises(StorageError) as exc:
        backend.upload("p/x.bin", b"data")
    assert "network down" in str(exc.value)


# ---------------------------------------------------------------------------
# Test 5 — analyses.id preservation on reparse.
#
# The 2026-06-27 reparse flood Round 2: even after the upsert fix landed,
# reparses on docs that already had a ``decisions`` row failed with
# ``decisions_analysis_id_fkey`` violations. Root cause: the Pydantic
# ``AnalysisRow`` generates a fresh ``uuid4()`` for its ``id`` on every
# build, the upsert UPDATEs that PK column, and Postgres CASCADE on
# DELETE does NOT cascade on UPDATE-of-PK. Migration 082 (ON DELETE
# CASCADE) did not solve this — the application-side fix is to reuse
# the existing analyses.id when an upsert would otherwise change it.
# ---------------------------------------------------------------------------


def test_supabase_persist_parse_result_preserves_existing_analyses_id_on_reparse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reparse must reuse the existing ``analyses.id``.

    The first parse stamped ``analysis.id = <X>`` and a downstream
    ``decisions`` row was written with ``analysis_id = <X>``. The reparse
    builds a fresh ``AnalysisRow`` with a new ``uuid4()`` for ``id``;
    without the lookup-then-reuse fix, the upsert UPDATEs the PK to
    that new UUID and the FK trips. With the fix, the lookup finds the
    existing row's id and the upsert preserves it.
    """
    doc_id = uuid4()
    existing_analysis_id = str(uuid4())
    fake = _FakeClient(
        canned_select_data={
            "analyses": [{"id": existing_analysis_id}],
        }
    )
    monkeypatch.setattr(storage_module, "get_supabase", lambda: fake)

    repo = SupabaseDocumentRepository()
    result = _pipeline_result(num_txns=1)
    repo.persist_parse_result(doc_id, result=result)

    # Find the upsert payload and assert its id is the existing UUID,
    # not the fresh uuid4 from _build_analysis().
    an_calls = [c for c in fake.calls if c.table == "analyses"]
    upsert_call = next(c for c in an_calls if c.op == "upsert")
    assert upsert_call.payload["id"] == existing_analysis_id, (
        f"reparse must reuse existing analyses.id={existing_analysis_id} "
        f"but upsert wrote id={upsert_call.payload['id']!r}"
    )


def test_supabase_persist_parse_result_fresh_parse_uses_new_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh parse (no existing analyses row) generates a new UUID.

    Counter-positive: ``existing_id`` lookup returns empty list, so the
    fix's branch goes to ``analysis.id`` as-built by ``_build_analysis``
    (a fresh uuid4). The upsert payload's id must NOT be empty, must be
    a valid UUID string, and must differ from previous fresh-parse runs
    (regenerated on every build).
    """
    doc_id = uuid4()
    fake = _FakeClient()  # no canned data — select returns empty list
    monkeypatch.setattr(storage_module, "get_supabase", lambda: fake)

    repo = SupabaseDocumentRepository()
    result = _pipeline_result(num_txns=1)
    repo.persist_parse_result(doc_id, result=result)

    an_calls = [c for c in fake.calls if c.table == "analyses"]
    upsert_call = next(c for c in an_calls if c.op == "upsert")
    new_id = upsert_call.payload["id"]
    assert new_id, "fresh-parse upsert payload must carry an id"
    # Parses as UUID — would raise if it doesn't.
    parsed = UUID(new_id)
    assert parsed.version == 4, f"fresh-parse id should be uuid4, got version {parsed.version}"


def test_supabase_persist_parse_result_lookup_fires_before_upsert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operation ordering: the id-lookup SELECT must precede the UPSERT.

    If the lookup happens after the upsert, the id-preservation logic
    can't apply. Pin the order so a future refactor that "optimises"
    by moving the lookup elsewhere can't quietly reintroduce the FK
    violation."""
    doc_id = uuid4()
    fake = _FakeClient(canned_select_data={"analyses": [{"id": str(uuid4())}]})
    monkeypatch.setattr(storage_module, "get_supabase", lambda: fake)

    repo = SupabaseDocumentRepository()
    repo.persist_parse_result(doc_id, result=_pipeline_result(num_txns=1))

    an_ops = [c.op for c in fake.calls if c.table == "analyses"]
    select_idx = an_ops.index("select")
    upsert_idx = an_ops.index("upsert")
    assert select_idx < upsert_idx, f"id-lookup SELECT must precede UPSERT, got order: {an_ops}"
