"""Tests for the InMemoryProbeReviewRepository + disagreement collector.

Covers the repository contract — add/count/list — and the
``collect_unreviewed_disagreements`` helper that joins documents with
the verdict store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.probe_review import (
    PROBE_TEXT_LAYER_V2,
    InMemoryProbeReviewRepository,
)
from aegis.probe_review.repository import (
    _parse_flag_kv_tail,
    collect_unreviewed_disagreements,
)
from aegis.storage import DocumentRow, InMemoryDocumentRepository


def _seed_document(
    docs: InMemoryDocumentRepository,
    *,
    merchant_id: UUID,
    filename: str,
    flags: list[str],
) -> DocumentRow:
    """Create + finalise an in-memory document with the chosen flags.

    The InMemoryDocumentRepository's ``create_document`` constructor
    returns a pending row; this helper mutates the row in place to
    populate ``all_flags`` + ``parsed_at`` so it surfaces to
    ``list_documents``.
    """
    row = docs.create_document(
        file_hash="hash-" + uuid4().hex,
        byte_size=1024,
        original_filename=filename,
        merchant_id=merchant_id,
    )
    docs._docs[row.id].all_flags = list(flags)
    docs._docs[row.id].parsed_at = datetime.now(UTC)
    docs._docs[row.id].parse_status = "proceed"
    return docs._docs[row.id]


@pytest.fixture
def docs() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def merchants() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def repo() -> InMemoryProbeReviewRepository:
    return InMemoryProbeReviewRepository()


def test_add_verdict_persists_row(repo: InMemoryProbeReviewRepository) -> None:
    doc_id = uuid4()
    row = repo.add_verdict(
        document_id=doc_id,
        probe_name=PROBE_TEXT_LAYER_V2,
        verdict="v2_correct",
        operator_email="filip@commerafunding.com",
    )
    assert row.document_id == doc_id
    assert row.operator_verdict == "v2_correct"
    assert row.operator_email == "filip@commerafunding.com"
    assert row.probe_name == PROBE_TEXT_LAYER_V2


def test_add_verdict_is_idempotent_per_operator(
    repo: InMemoryProbeReviewRepository,
) -> None:
    """A second click from the same operator returns the existing row."""
    doc_id = uuid4()
    first = repo.add_verdict(
        document_id=doc_id,
        probe_name=PROBE_TEXT_LAYER_V2,
        verdict="v2_correct",
        operator_email="filip@commerafunding.com",
    )
    second = repo.add_verdict(
        document_id=doc_id,
        probe_name=PROBE_TEXT_LAYER_V2,
        verdict="v1_correct",  # different verdict — first one wins
        operator_email="filip@commerafunding.com",
    )
    assert first.id == second.id
    assert second.operator_verdict == "v2_correct"


def test_count_verdicts_aggregates_across_operators(
    repo: InMemoryProbeReviewRepository,
) -> None:
    for idx in range(3):
        repo.add_verdict(
            document_id=uuid4(),
            probe_name=PROBE_TEXT_LAYER_V2,
            verdict="v2_correct",
            operator_email=f"op{idx}@aegis.local",
        )
    repo.add_verdict(
        document_id=uuid4(),
        probe_name=PROBE_TEXT_LAYER_V2,
        verdict="v1_correct",
        operator_email="other@aegis.local",
    )
    counts = repo.count_verdicts(PROBE_TEXT_LAYER_V2)
    assert counts["v2_correct"] == 3
    assert counts["v1_correct"] == 1


def test_count_verdicts_returns_both_keys_on_empty(
    repo: InMemoryProbeReviewRepository,
) -> None:
    counts = repo.count_verdicts(PROBE_TEXT_LAYER_V2)
    assert counts == {"v2_correct": 0, "v1_correct": 0}


def test_list_reviewed_document_ids_filters_by_operator(
    repo: InMemoryProbeReviewRepository,
) -> None:
    doc_a = uuid4()
    doc_b = uuid4()
    repo.add_verdict(
        document_id=doc_a,
        probe_name=PROBE_TEXT_LAYER_V2,
        verdict="v2_correct",
        operator_email="me@aegis.local",
    )
    repo.add_verdict(
        document_id=doc_b,
        probe_name=PROBE_TEXT_LAYER_V2,
        verdict="v2_correct",
        operator_email="other@aegis.local",
    )
    reviewed_by_me = repo.list_reviewed_document_ids(
        probe_name=PROBE_TEXT_LAYER_V2, operator_email="me@aegis.local"
    )
    assert reviewed_by_me == {doc_a}


def test_parse_flag_kv_tail_extracts_pairs() -> None:
    flag = (
        "[SHADOW] text_layer_probe_v2_disagrees: "
        "v2_route_vision=True live_route_vision=False "
        "chars_avg=12 numeric_lines=4"
    )
    kv = _parse_flag_kv_tail(flag)
    assert kv["v2_route_vision"] == "True"
    assert kv["live_route_vision"] == "False"
    assert kv["chars_avg"] == "12"
    assert kv["numeric_lines"] == "4"


def _seed_merchant(merchants: InMemoryMerchantRepository, *, business_name: str) -> UUID:
    """Insert a finalised merchant via upsert; return its id."""
    merchant = MerchantRow(
        business_name=business_name,
        state="CA",
    )
    saved = merchants.upsert(merchant)
    return saved.id


def test_collect_unreviewed_disagreements_returns_disagreement_rows(
    docs: InMemoryDocumentRepository,
    merchants: InMemoryMerchantRepository,
    repo: InMemoryProbeReviewRepository,
) -> None:
    """A doc with the disagreement flag surfaces; a doc without one does not."""
    merchant_id = _seed_merchant(merchants, business_name="Acme Inc")

    flagged = _seed_document(
        docs,
        merchant_id=merchant_id,
        filename="flagged.pdf",
        flags=[
            (
                "[SHADOW] text_layer_probe_v2_disagrees: "
                "v2_route_vision=True live_route_vision=False "
                "chars_avg=8 numeric_lines=2"
            )
        ],
    )
    _seed_document(
        docs,
        merchant_id=merchant_id,
        filename="quiet.pdf",
        flags=["[META] some_other_flag"],
    )

    rows = collect_unreviewed_disagreements(
        docs=docs,
        merchants=merchants,
        repo=repo,
        probe_name=PROBE_TEXT_LAYER_V2,
        operator_email="filip@aegis.local",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.document_id == flagged.id
    assert row.original_filename == "flagged.pdf"
    assert row.v1_decision == "use text layer"  # live_route_vision=False
    assert row.v2_decision == "route to vision"  # v2_route_vision=True


def test_collect_unreviewed_disagreements_filters_by_reviewed_set(
    docs: InMemoryDocumentRepository,
    merchants: InMemoryMerchantRepository,
    repo: InMemoryProbeReviewRepository,
) -> None:
    """A doc this operator has already adjudicated is hidden from their listing."""
    merchant_id = _seed_merchant(merchants, business_name="Acme Inc")

    flagged = _seed_document(
        docs,
        merchant_id=merchant_id,
        filename="flagged.pdf",
        flags=[
            "[SHADOW] text_layer_probe_v2_disagrees: v2_route_vision=True live_route_vision=False"
        ],
    )

    repo.add_verdict(
        document_id=flagged.id,
        probe_name=PROBE_TEXT_LAYER_V2,
        verdict="v2_correct",
        operator_email="filip@aegis.local",
    )

    rows = collect_unreviewed_disagreements(
        docs=docs,
        merchants=merchants,
        repo=repo,
        probe_name=PROBE_TEXT_LAYER_V2,
        operator_email="filip@aegis.local",
    )
    assert rows == []

    # ... but another operator still sees it.
    other = collect_unreviewed_disagreements(
        docs=docs,
        merchants=merchants,
        repo=repo,
        probe_name=PROBE_TEXT_LAYER_V2,
        operator_email="other@aegis.local",
    )
    assert len(other) == 1
