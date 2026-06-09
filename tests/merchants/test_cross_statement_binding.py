"""M8 — cross-statement / related-account binding detector tests.

Pure-function unit tests over
``aegis.merchants.cross_statement_detector``. The detector has no I/O,
so fixtures are dataclass instances built in-test — no in-memory repo
needed.

Per CLAUDE.md "Decision-boundary changes — shadow-first" + the parser-
side R1.1 shadow-pattern precedent: every flag emitted here MUST have
``severity == 0`` so it never feeds ``patterns.fraud_score``. The
shadow-severity invariant is asserted on every fire-path test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from aegis.merchants.cross_statement_detector import (
    CurrentUploadContext,
    PriorAnalysisIdentity,
    PriorDocumentRef,
    detect_cross_statement_bindings,
    detect_duplicate_pdf_upload,
    detect_related_account_holder,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64

# Use distinct deterministic UUIDs so the test asserts source_id ordering
# without flakiness.
_DOC_CURRENT = UUID("00000000-0000-0000-0000-000000000001")
_DOC_PRIOR_1 = UUID("00000000-0000-0000-0000-000000000002")
_DOC_PRIOR_2 = UUID("00000000-0000-0000-0000-000000000003")
_DOC_PRIOR_3 = UUID("00000000-0000-0000-0000-000000000004")


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Sub-detector A — duplicate PDF upload


def test_duplicate_pdf_upload_fires_on_sha_collision() -> None:
    """Two documents same sha256 for same merchant → flag fires."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="1234",
    )
    priors = [
        PriorDocumentRef(
            document_id=_DOC_PRIOR_1,
            sha256_original=_HASH_A,
            uploaded_at=_utc(2026, 5, 1),
        ),
    ]

    flags = detect_duplicate_pdf_upload(current, priors)

    assert len(flags) == 1
    flag = flags[0]
    assert flag.code == "duplicate_pdf_upload"
    assert flag.severity == 0  # shadow-only invariant
    assert str(_DOC_PRIOR_1) in flag.detail
    assert "2026-05-01" in flag.detail  # uploaded_at lands in detail
    assert _DOC_PRIOR_1 in flag.source_ids


def test_duplicate_pdf_no_match_no_flag() -> None:
    """Different sha256 across priors → no flag."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="1234",
    )
    priors = [
        PriorDocumentRef(
            document_id=_DOC_PRIOR_1,
            sha256_original=_HASH_B,
            uploaded_at=_utc(2026, 5, 1),
        ),
        PriorDocumentRef(
            document_id=_DOC_PRIOR_2,
            sha256_original=_HASH_C,
            uploaded_at=_utc(2026, 4, 1),
        ),
    ]

    flags = detect_duplicate_pdf_upload(current, priors)
    assert flags == []


def test_duplicate_pdf_same_doc_id_not_flagged() -> None:
    """If the 'prior' is in fact the current row (id collision), don't fire.

    Defensive: the caller should always exclude the current document from
    the priors list, but the detector also guards. This catches the case
    where the caller passes ``list_documents(merchant_id=X)`` without
    filtering out the just-inserted row.
    """
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="1234",
    )
    priors = [
        PriorDocumentRef(
            document_id=_DOC_CURRENT,
            sha256_original=_HASH_A,
            uploaded_at=_utc(2026, 5, 1),
        ),
    ]
    flags = detect_duplicate_pdf_upload(current, priors)
    assert flags == []


def test_duplicate_pdf_legacy_priors_without_hash_skipped() -> None:
    """Priors with sha256_original=None (pre-033 legacy) silently skipped."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="1234",
    )
    priors = [
        PriorDocumentRef(
            document_id=_DOC_PRIOR_1,
            sha256_original=None,
            uploaded_at=_utc(2026, 5, 1),
        ),
    ]
    flags = detect_duplicate_pdf_upload(current, priors)
    assert flags == []


def test_duplicate_pdf_current_without_hash_skipped() -> None:
    """Current upload sha256=None (storage step pending) → no flag."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=None,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="1234",
    )
    priors = [
        PriorDocumentRef(
            document_id=_DOC_PRIOR_1,
            sha256_original=_HASH_A,
            uploaded_at=_utc(2026, 5, 1),
        ),
    ]
    flags = detect_duplicate_pdf_upload(current, priors)
    assert flags == []


def test_duplicate_pdf_multi_collision_counts_and_sources() -> None:
    """Three prior copies of the same sha → detail surfaces count, all
    document_ids in source_ids."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="1234",
    )
    priors = [
        PriorDocumentRef(
            document_id=_DOC_PRIOR_1,
            sha256_original=_HASH_A,
            uploaded_at=_utc(2026, 4, 1),
        ),
        PriorDocumentRef(
            document_id=_DOC_PRIOR_2,
            sha256_original=_HASH_A,
            uploaded_at=_utc(2026, 5, 1),
        ),
        PriorDocumentRef(
            document_id=_DOC_PRIOR_3,
            sha256_original=_HASH_B,
            uploaded_at=_utc(2026, 5, 15),
        ),
    ]
    flags = detect_duplicate_pdf_upload(current, priors)
    assert len(flags) == 1
    flag = flags[0]
    assert "total_prior_copies=2" in flag.detail
    # Oldest first — deterministic.
    assert flag.source_ids == [_DOC_PRIOR_1, _DOC_PRIOR_2]
    assert _DOC_PRIOR_3 not in flag.source_ids  # different hash


def test_duplicate_pdf_caller_filters_to_one_merchant() -> None:
    """The detector takes ALREADY-merchant-filtered priors.

    The caller (worker) queries ``list_documents(merchant_id=X)`` so
    cross-merchant hash collisions are never passed in. This test
    documents the contract: the detector evaluates whatever priors it
    receives. With an empty priors list, same hash on a totally
    different merchant's upload does NOT fire.
    """
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="1234",
    )
    flags = detect_duplicate_pdf_upload(current, priors=[])
    assert flags == []


# ---------------------------------------------------------------------------
# Sub-detector B — related account


def test_related_account_same_holder_new_last4_fires() -> None:
    """Same holder + new last4 → flag fires."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="9999",
    )
    priors = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_1,
            bank_name="Chase",
            account_holder="Acme LLC",
            account_last4="1234",
        ),
    ]
    flags = detect_related_account_holder(current, priors)
    assert len(flags) == 1
    flag = flags[0]
    assert flag.code == "related_account_suspected"
    assert flag.severity == 0
    assert "existing_last4=1234" in flag.detail
    assert "new_last4=9999" in flag.detail
    assert "holder=Acme LLC" in flag.detail
    assert _DOC_PRIOR_1 in flag.source_ids


def test_related_account_same_holder_same_last4_no_flag() -> None:
    """Same holder + same last4 → no flag (more months of same account)."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="1234",
    )
    priors = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_1,
            bank_name="Chase",
            account_holder="Acme LLC",
            account_last4="1234",
        ),
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_2,
            bank_name="Chase",
            account_holder="Acme LLC",
            account_last4="1234",
        ),
    ]
    flags = detect_related_account_holder(current, priors)
    assert flags == []


def test_related_account_different_holder_no_flag() -> None:
    """Different holder name → no flag."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="9999",
    )
    priors = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_1,
            bank_name="Chase",
            account_holder="Beta Corp",
            account_last4="1234",
        ),
    ]
    flags = detect_related_account_holder(current, priors)
    assert flags == []


def test_related_account_case_insensitive_holder_match() -> None:
    """Case-insensitive match: 'Acme LLC' vs 'ACME LLC' → flag fires."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="9999",
    )
    priors = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_1,
            bank_name="Chase",
            account_holder="ACME LLC",
            account_last4="1234",
        ),
    ]
    flags = detect_related_account_holder(current, priors)
    assert len(flags) == 1
    # Detail surfaces the RAW current holder, not the normalized form.
    assert "holder=Acme LLC" in flags[0].detail


def test_related_account_normalization_whitespace_punctuation() -> None:
    """Holder normalization handles whitespace + punctuation drift.

    'Acme,  LLC' (extra comma + double space) and 'Acme LLC' should
    match. Common on real statements where the bank's name field
    inserts variable punctuation across periods.
    """
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme,  LLC",
        account_last4="9999",
    )
    priors = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_1,
            bank_name="Chase",
            account_holder="  Acme LLC ",
            account_last4="1234",
        ),
    ]
    flags = detect_related_account_holder(current, priors)
    assert len(flags) == 1, (
        "expected punctuation+whitespace-normalized match to fire"
    )


def test_related_account_entity_suffix_not_stripped() -> None:
    """'Acme LLC' vs 'Acme Inc' → no flag.

    Different entity suffixes are legally distinct entities. The
    detector MUST NOT strip 'LLC' / 'Inc' / 'Corp' — doing so would
    fire on legitimately separate businesses.
    """
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="9999",
    )
    priors = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_1,
            bank_name="Chase",
            account_holder="Acme Inc",
            account_last4="1234",
        ),
    ]
    flags = detect_related_account_holder(current, priors)
    assert flags == []


def test_related_account_current_holder_none_no_flag() -> None:
    """Current holder None → cannot compare → no flag."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder=None,
        account_last4="9999",
    )
    priors = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_1,
            bank_name="Chase",
            account_holder="Acme LLC",
            account_last4="1234",
        ),
    ]
    flags = detect_related_account_holder(current, priors)
    assert flags == []


def test_related_account_current_last4_none_no_flag() -> None:
    """Current last4 None → cannot derive drift → no flag."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4=None,
    )
    priors = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_1,
            bank_name="Chase",
            account_holder="Acme LLC",
            account_last4="1234",
        ),
    ]
    flags = detect_related_account_holder(current, priors)
    assert flags == []


def test_related_account_prior_last4_none_does_not_alone_fire() -> None:
    """If every prior matching the holder has last4=None we cannot honestly
    claim 'drift to a new last4' — no flag. (We'd have nothing to drift FROM.)"""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="9999",
    )
    priors = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_1,
            bank_name="Chase",
            account_holder="Acme LLC",
            account_last4=None,
        ),
    ]
    flags = detect_related_account_holder(current, priors)
    assert flags == []


def test_related_account_multi_prior_last4_set_joined_sorted() -> None:
    """Multiple prior last4 values join in sorted order in detail string."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="9999",
    )
    priors = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_1,
            bank_name="Chase",
            account_holder="Acme LLC",
            account_last4="5555",
        ),
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_2,
            bank_name="Wells Fargo",
            account_holder="Acme LLC",
            account_last4="1234",
        ),
    ]
    flags = detect_related_account_holder(current, priors)
    assert len(flags) == 1
    flag = flags[0]
    # Deterministic sorted comma join.
    assert "existing_last4=1234,5555" in flag.detail
    # Both prior doc_ids surface as source_ids — operator can drill to
    # either to see the OTHER accounts already on file.
    assert _DOC_PRIOR_1 in flag.source_ids
    assert _DOC_PRIOR_2 in flag.source_ids


def test_related_account_dedups_last4_in_set() -> None:
    """If two priors share the same last4, only one copy of the last4
    appears in the joined detail (set-based dedup)."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="9999",
    )
    priors = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_1,
            bank_name="Chase",
            account_holder="Acme LLC",
            account_last4="1234",
        ),
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_2,
            bank_name="Chase",
            account_holder="Acme LLC",
            account_last4="1234",
        ),
    ]
    flags = detect_related_account_holder(current, priors)
    assert len(flags) == 1
    assert "existing_last4=1234" in flags[0].detail
    # Both source documents still credited.
    assert set(flags[0].source_ids) == {_DOC_PRIOR_1, _DOC_PRIOR_2}


# ---------------------------------------------------------------------------
# Top-level entry combines both


def test_detect_cross_statement_bindings_combines_both() -> None:
    """When BOTH sub-detectors fire, the entry returns both flags in
    deterministic order (duplicate first, related second)."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="9999",
    )
    prior_documents = [
        PriorDocumentRef(
            document_id=_DOC_PRIOR_1,
            sha256_original=_HASH_A,
            uploaded_at=_utc(2026, 5, 1),
        ),
    ]
    prior_analyses = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_2,
            bank_name="Chase",
            account_holder="Acme LLC",
            account_last4="1234",
        ),
    ]
    flags = detect_cross_statement_bindings(
        current,
        prior_documents=prior_documents,
        prior_analyses=prior_analyses,
    )
    assert [f.code for f in flags] == [
        "duplicate_pdf_upload",
        "related_account_suspected",
    ]
    # Shadow-only severity invariant on every flag.
    assert all(f.severity == 0 for f in flags)


def test_detect_cross_statement_bindings_returns_empty_when_clean() -> None:
    """Clean upload (new hash, new holder) → empty list."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="9999",
    )
    flags = detect_cross_statement_bindings(
        current,
        prior_documents=[],
        prior_analyses=[],
    )
    assert flags == []


# ---------------------------------------------------------------------------
# Source-id traceability — explicit per the U12 spec.


def test_emitted_flag_source_ids_reference_prior_document_ids() -> None:
    """Both sub-detectors emit flags whose source_ids are existing
    document_ids — the operator clicks them to drill back."""
    current = CurrentUploadContext(
        document_id=_DOC_CURRENT,
        sha256_original=_HASH_A,
        bank_name="Chase",
        account_holder="Acme LLC",
        account_last4="9999",
    )
    prior_documents = [
        PriorDocumentRef(
            document_id=_DOC_PRIOR_1,
            sha256_original=_HASH_A,
            uploaded_at=_utc(2026, 5, 1),
        ),
    ]
    prior_analyses = [
        PriorAnalysisIdentity(
            document_id=_DOC_PRIOR_2,
            bank_name="Chase",
            account_holder="Acme LLC",
            account_last4="1234",
        ),
    ]
    flags = detect_cross_statement_bindings(
        current,
        prior_documents=prior_documents,
        prior_analyses=prior_analyses,
    )
    # Each flag's source_ids must be a non-empty list of UUIDs from the
    # supplied priors — no synthesized / unrelated ids.
    valid_prior_doc_ids = {_DOC_PRIOR_1, _DOC_PRIOR_2}
    for f in flags:
        assert f.source_ids, "shadow flag missing audit-trail source_ids"
        assert all(isinstance(s, UUID) for s in f.source_ids)
        assert set(f.source_ids).issubset(valid_prior_doc_ids)
