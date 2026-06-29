"""Unit tests for ``scripts/ingest_training_corpus.py`` (build plan §6.5).

Covers:

* ``a)`` SHA-256 dedup against ``corpus_documents.file_hash`` — a file
        already on record is skipped, no corpus row, no bank_layouts
        bump, no audit row.
* ``b)`` Clean statement → hint + fingerprint update — a no-signal
        PDF with a known bank bumps ``bank_layouts.successful_parses``
        and appends a ``(creator, producer)`` pair to
        ``layout_fingerprint["creator_observations"]``.
* ``c)`` Fraud-signal-firing statement → corpus_documents row written
        but NO bank_layouts hint / fingerprint update (clean-only
        seeding policy).
* ``d)`` ``bank_name=None`` (unrecognised header) → corpus row written
        but NO bank_layouts write (can't seed an unknown bank).
* ``e)`` Live tables never touched — the corpus repo, the bank-layout
        repo, and the audit log are the ONLY persistence the script
        reaches. We assert via the in-memory shapes (which don't
        define a merchants/documents/analyses surface) plus a module-
        level import-set check that the script never imports a live-
        pipeline write path.

No Supabase calls. No real PDFs needed — the forensic + metadata
helpers are monkeypatched per test so we control exactly what each
file looks like.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aegis.audit import InMemoryAuditLog  # noqa: E402
from aegis.bank_layouts.repository import InMemoryBankLayoutRepository  # noqa: E402
from scripts import ingest_training_corpus as ingest  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers — fake PDFs (bytes on disk) + fake forensic results
# ---------------------------------------------------------------------------


@dataclass
class _FakeForensicConfig:
    """One row's planned detector output. The monkeypatch returns this
    for whatever PDF path the script asks about."""

    has_font_inconsistency: bool = False
    has_text_overlay: bool = False
    has_creator_mismatch: bool = False
    creator: str = "Bank of America"
    producer: str = "TargetStream StreamEDS rv1.7.161 for Bank of America"
    page_count: int = 6
    first_page_text: str = "Bank of America account statement"


def _make_pdf_file(folder: Path, name: str, content_bytes: bytes) -> Path:
    """Drop a fake PDF (any bytes, just for SHA-256 differentiation)."""
    path = folder / name
    path.write_bytes(content_bytes)
    return path


def _patch_detectors(
    monkeypatch: pytest.MonkeyPatch,
    config_by_name: dict[str, _FakeForensicConfig],
    *,
    default: _FakeForensicConfig | None = None,
) -> None:
    """Replace ``run_forensic_pass`` + ``_read_first_page_text`` to
    return per-filename canned shapes. Filenames not in ``config_by_name``
    use ``default`` (or an all-clean fallback).
    """
    fallback = default or _FakeForensicConfig()

    def fake_run(pdf_path: Path) -> ingest.ForensicResult:
        cfg = config_by_name.get(pdf_path.name, fallback)
        return ingest.ForensicResult(
            has_font_inconsistency=cfg.has_font_inconsistency,
            has_text_overlay=cfg.has_text_overlay,
            has_creator_mismatch=cfg.has_creator_mismatch,
            creator=cfg.creator,
            producer=cfg.producer,
            page_count=cfg.page_count,
        )

    def fake_first_page(pdf_path: Path) -> str:
        cfg = config_by_name.get(pdf_path.name, fallback)
        return cfg.first_page_text

    monkeypatch.setattr(ingest, "run_forensic_pass", fake_run)
    monkeypatch.setattr(ingest, "_read_first_page_text", fake_first_page)


# ---------------------------------------------------------------------------
# (a) Dedup
# ---------------------------------------------------------------------------


def test_dedup_skips_file_with_known_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second ingest of the same byte payload skips on file_hash."""
    pdf = _make_pdf_file(tmp_path, "chase.pdf", b"fake-pdf-bytes-A")
    expected_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()

    _patch_detectors(
        monkeypatch,
        {
            "chase.pdf": _FakeForensicConfig(
                creator="",
                producer="OpenText Output Transformation Engine - 23.4.25",
                page_count=8,
                first_page_text="JPMorgan Chase business statement",
            )
        },
    )

    corpus = ingest.InMemoryCorpusRepository()
    bank = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()

    # First pass — ingest.
    results1, summary1 = ingest.ingest_folder(
        tmp_path,
        corpus_repo=corpus,
        bank_repo=bank,
        audit=audit,
        apply=True,
    )
    assert summary1.files_ingested == 1
    assert summary1.files_skipped_dedup == 0
    assert results1[0].action == "ingested"
    assert results1[0].file_hash == expected_hash
    assert expected_hash in corpus.rows

    # Second pass — same folder, same file. Dedup must skip.
    results2, summary2 = ingest.ingest_folder(
        tmp_path,
        corpus_repo=corpus,
        bank_repo=bank,
        audit=audit,
        apply=True,
    )
    assert summary2.files_ingested == 0
    assert summary2.files_skipped_dedup == 1
    assert results2[0].action == "dedup_skip"
    assert results2[0].file_hash == expected_hash
    # bank_layouts NOT bumped a second time.
    chase_row = bank.find_by_bank_name("JPMorgan Chase Bank, N.A.")
    assert chase_row is not None
    assert chase_row.successful_parses == 1
    # Audit log: one record from the first ingest, none from the skip.
    assert len(audit.entries) == 1


# ---------------------------------------------------------------------------
# (b) Clean statement → hint + fingerprint update
# ---------------------------------------------------------------------------


def test_clean_statement_seeds_hint_and_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean PDF with a known bank bumps successful_parses AND
    appends the (creator, producer) pair to the bank's
    ``layout_fingerprint["creator_observations"]`` list."""
    _make_pdf_file(tmp_path, "boa-clean.pdf", b"fake-bytes-B")

    _patch_detectors(
        monkeypatch,
        {
            "boa-clean.pdf": _FakeForensicConfig(
                creator="Bank of America",
                producer="TargetStream StreamEDS rv1.7.161 for Bank of America",
                first_page_text="Bank of America business statement",
                page_count=5,
            )
        },
    )

    corpus = ingest.InMemoryCorpusRepository()
    bank = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()

    _, summary = ingest.ingest_folder(
        tmp_path,
        corpus_repo=corpus,
        bank_repo=bank,
        audit=audit,
        apply=True,
    )
    assert summary.files_ingested == 1
    assert summary.hints_updated == 1
    assert summary.fingerprints_added == 1

    boa_row = bank.find_by_bank_name("Bank of America, N.A.")
    assert boa_row is not None
    assert boa_row.successful_parses == 1
    observations = boa_row.layout_fingerprint.get("creator_observations")
    assert observations == [
        {
            "creator": "Bank of America",
            "producer": "TargetStream StreamEDS rv1.7.161 for Bank of America",
        }
    ]
    # Audit row carries the signal booleans + bank (mask-redacted).
    # ``bank_name`` is in ``aegis.logger._PII_KEYS`` so the
    # ``audit._mask_value`` pass redacts the value to "***" — the
    # presence of the key still confirms the record was written.
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["action"] == "corpus.document_ingested"
    assert "bank_name" in entry["details"]
    assert entry["details"]["bank_name"] == "***"  # PII-masked
    assert entry["details"]["fraud_signals_fired"] is False
    assert entry["details"]["hint_updated"] is True
    assert entry["details"]["fingerprint_added"] is True

    # Re-ingest with the SAME creator/producer pair on a DIFFERENT
    # file — successful_parses bumps but fingerprint_added stays False
    # (de-dup of observation pair).
    _make_pdf_file(tmp_path, "boa-clean-2.pdf", b"fake-bytes-B2")
    _patch_detectors(
        monkeypatch,
        {
            "boa-clean.pdf": _FakeForensicConfig(
                creator="Bank of America",
                producer="TargetStream StreamEDS rv1.7.161 for Bank of America",
                first_page_text="Bank of America business statement",
                page_count=5,
            ),
            "boa-clean-2.pdf": _FakeForensicConfig(
                creator="Bank of America",
                producer="TargetStream StreamEDS rv1.7.161 for Bank of America",
                first_page_text="Bank of America business statement",
                page_count=5,
            ),
        },
    )
    _, summary2 = ingest.ingest_folder(
        tmp_path,
        corpus_repo=corpus,
        bank_repo=bank,
        audit=audit,
        apply=True,
    )
    # File 1 was dedup-skipped; file 2 ingested.
    assert summary2.files_ingested == 1
    assert summary2.files_skipped_dedup == 1
    # Counter bumped — but fingerprints_added stays 0 (same pair).
    assert summary2.hints_updated == 1
    assert summary2.fingerprints_added == 0
    boa_row2 = bank.find_by_bank_name("Bank of America, N.A.")
    assert boa_row2 is not None
    assert boa_row2.successful_parses == 2
    assert (
        boa_row2.layout_fingerprint.get("creator_observations") == observations  # unchanged
    )


# ---------------------------------------------------------------------------
# (c) Fraud-signal statement → corpus row, NO hint/fingerprint update
# ---------------------------------------------------------------------------


def test_fraud_signal_statement_writes_corpus_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ANY forensic detector fires, the corpus row is still
    written (so the operator has the data point), but bank_layouts is
    NOT touched — clean-only seeding policy."""
    _make_pdf_file(tmp_path, "td-bad.pdf", b"fake-bytes-C")

    _patch_detectors(
        monkeypatch,
        {
            "td-bad.pdf": _FakeForensicConfig(
                has_text_overlay=True,
                creator="iText",
                producer="some-editor",
                first_page_text="TD Bank statement of account",
                page_count=4,
            )
        },
    )

    corpus = ingest.InMemoryCorpusRepository()
    bank = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()

    _, summary = ingest.ingest_folder(
        tmp_path,
        corpus_repo=corpus,
        bank_repo=bank,
        audit=audit,
        apply=True,
    )
    assert summary.files_ingested == 1
    assert summary.hints_updated == 0
    assert summary.fingerprints_added == 0

    # Corpus row exists with signals captured.
    assert len(corpus.rows) == 1
    row = next(iter(corpus.rows.values()))
    assert row["fraud_signals_fired"] is True
    assert row["has_text_overlay"] is True
    assert row["bank_name"] == "TD Bank, N.A."

    # bank_layouts NOT touched.
    td_row = bank.find_by_bank_name("TD Bank, N.A.")
    assert td_row is None

    # Audit row written with signals — operator-facing record.
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["details"]["fraud_signals_fired"] is True
    assert entry["details"]["hint_updated"] is False
    assert entry["details"]["fingerprint_added"] is False


# ---------------------------------------------------------------------------
# (d) bank_name=None → corpus row written but NO fingerprint
# ---------------------------------------------------------------------------


def test_unknown_bank_writes_corpus_only_no_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean PDF whose first-page text does NOT match any bank
    pattern lands in corpus_documents (so the operator sees the
    coverage gap) but does NOT seed bank_layouts."""
    _make_pdf_file(tmp_path, "unknown.pdf", b"fake-bytes-D")

    _patch_detectors(
        monkeypatch,
        {
            "unknown.pdf": _FakeForensicConfig(
                creator="SomeBank Producer",
                producer="SomeBank-Internal-Tool",
                first_page_text="A regional bank statement (no known token)",
                page_count=3,
            )
        },
    )

    corpus = ingest.InMemoryCorpusRepository()
    bank = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()

    _, summary = ingest.ingest_folder(
        tmp_path,
        corpus_repo=corpus,
        bank_repo=bank,
        audit=audit,
        apply=True,
    )
    assert summary.files_ingested == 1
    assert summary.fingerprints_added == 0
    assert len(corpus.rows) == 1
    row = next(iter(corpus.rows.values()))
    assert row["bank_name"] is None
    # No bank_layouts row created.
    assert bank.list_all() == []
    # Audit row written; bank_name lives under aegis.logger._PII_KEYS so
    # the value is redacted to "***" regardless of source value. What
    # matters is the audit record fired with the correct ``action`` +
    # signal booleans.
    entry = audit.entries[0]
    assert entry["action"] == "corpus.document_ingested"
    assert entry["details"]["fraud_signals_fired"] is False
    assert entry["details"]["hint_updated"] is False
    assert entry["details"]["fingerprint_added"] is False


# ---------------------------------------------------------------------------
# (e) Live tables never touched
# ---------------------------------------------------------------------------


def test_script_does_not_import_live_pipeline_write_paths() -> None:
    """Structural invariant: the script must not import any live-
    pipeline persistence module. A future refactor that bypasses the
    in-memory repos and reaches for, say, ``aegis.merchants`` would
    silently expand the script's blast radius — catch it at the
    module-import level.

    The allow-list is conservative: it covers everything the script
    legitimately depends on. Adding a new live-pipeline writer? Add
    it to BANNED first and watch this test fail.
    """
    import scripts.ingest_training_corpus as module

    banned_substrings = (
        "aegis.merchants",
        "aegis.deals",
        "aegis.submissions",
        "aegis.funder_note_submissions",
        "aegis.compliance.snapshot",
        "aegis.compliance.overrides",
        "aegis.scoring",
        "aegis.scoring_v2",
        "aegis.counterparty",
        "aegis.business_intel",
        "aegis.web_presence",
        "aegis.close",
        "aegis.zoho",
        "aegis.funders.replies",
        "aegis.pdf_store",
    )
    source = Path(module.__file__).read_text(encoding="utf-8")
    for needle in banned_substrings:
        assert needle not in source, (
            f"script reached into banned live-pipeline module: {needle}. "
            f"Corpus ingestion must stay isolated from the live pipeline."
        )


def test_script_module_does_not_call_live_repos(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioural invariant: a full ingest_folder pass on
    in-memory repos must NEVER reach a real Supabase client. We
    monkey-patch ``aegis.db.get_supabase`` to a sentinel that raises
    on call; a green test proves nothing in the script's hot path
    ended up dereferencing the live client."""
    import aegis.db as db_module

    sentinel_calls: list[str] = []

    def boom() -> Any:
        sentinel_calls.append("called")
        raise AssertionError(
            "ingest_training_corpus must NEVER call get_supabase() in the in-memory test path"
        )

    monkeypatch.setattr(db_module, "get_supabase", boom)

    _make_pdf_file(tmp_path, "boa.pdf", b"x")
    _patch_detectors(
        monkeypatch,
        {
            "boa.pdf": _FakeForensicConfig(
                first_page_text="Bank of America account",
            )
        },
    )

    corpus = ingest.InMemoryCorpusRepository()
    bank = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()

    ingest.ingest_folder(
        tmp_path,
        corpus_repo=corpus,
        bank_repo=bank,
        audit=audit,
        apply=True,
    )
    assert sentinel_calls == []


# ---------------------------------------------------------------------------
# Bank-name regex coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("JPMorgan Chase Bank, N.A.", "JPMorgan Chase Bank, N.A."),
        ("CHASE business checking statement", "JPMorgan Chase Bank, N.A."),
        ("Bank of America, N.A. monthly statement", "Bank of America, N.A."),
        ("TD Bank statement", "TD Bank, N.A."),
        ("Wells Fargo business checking", "Wells Fargo Bank, N.A."),
        ("Mercury business account", "Mercury"),
        ("Lili statement period", "Lili"),
        ("Third Coast Bank monthly statement", "Third Coast Bank, SSB"),
        ("Some unknown regional bank", None),
        ("", None),
    ],
)
def test_detect_bank_name(text: str, expected: str | None) -> None:
    assert ingest.detect_bank_name(text) == expected


# ---------------------------------------------------------------------------
# Dry-run never writes
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``--apply``, the script reports per-file outcomes but
    never inserts into corpus_documents / bank_layouts / audit_log."""
    _make_pdf_file(tmp_path, "boa.pdf", b"y")
    _patch_detectors(
        monkeypatch,
        {"boa.pdf": _FakeForensicConfig(first_page_text="Bank of America")},
    )

    corpus = ingest.InMemoryCorpusRepository()
    bank = InMemoryBankLayoutRepository()
    audit = InMemoryAuditLog()

    _, summary = ingest.ingest_folder(
        tmp_path,
        corpus_repo=corpus,
        bank_repo=bank,
        audit=audit,
        apply=False,
    )
    assert summary.files_ingested == 1  # walked + reported
    assert summary.fingerprints_added == 0
    assert corpus.rows == {}
    assert bank.list_all() == []
    assert audit.entries == []
