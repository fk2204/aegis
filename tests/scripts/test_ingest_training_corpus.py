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
        # 2026-06-30 second-pass additions — verified against real
        # NULL-bank cohort samples from the training corpus.
        (
            "000000812355516 ... JPMorgan Chase Bank, N",
            "JPMorgan Chase Bank, N.A.",
        ),
        ("Discover Bank Statement Period 5/1/26", "Discover Bank"),
        ("Ally Bank checking account", "Ally Bank"),
        ("SouthState Bank business account", "SouthState Bank"),
        ("BankUnited statement", "BankUnited, N.A."),
        ("First Citizens Bank statement", "First Citizens Bank"),
        ("Santander Bank, N.A. monthly", "Santander Bank, N.A."),
        ("Webster Bank, N.A. account summary", "Webster Bank, N.A."),
        ("Comerica Bank business checking", "Comerica Bank"),
        ("Citizens Bank statement", "Citizens Bank, N.A."),
        ("Navy Federal Credit Union member", "Navy Federal Credit Union"),
        ("Axos Bank checking account", "Axos Bank"),
    ],
)
def test_detect_bank_name(text: str, expected: str | None) -> None:
    assert ingest.detect_bank_name(text) == expected


# ---------------------------------------------------------------------------
# Application-form filter (skip non-statement PDFs)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Real "USE_THIS_APP.pdf" first-page text from Filip's corpus.
        (
            "Company Information Legal Company Name: AF&F Carpentry LLC "
            "Website: Industry: Carpentry Incorporation State: ME Tax ID: "
            "33-4598538 Legal Entity: LLC Corp Sole Prop. Business Address: "
            "51 Ferry St Ste 2"
        ),
        # Blank-form template variant — same dompdf shape, no merchant data.
        (
            "Company Information Legal Company Name: Website: Industry: "
            "Incorporation State: Tax ID: Legal Entity: LLC Corp Sole Prop. "
            "Business Address: City: State: Zip: Business Start Date: "
            "Business Telephone#: "
        ),
        # Marker order shuffled — still trips the >=2 threshold.
        ("MERCHANT APPLICATION Tax ID: 12-3456789 Legal Entity: LLC Business Address: 123 Main St"),
    ],
)
def test_is_application_form_detects_app_pdfs(text: str) -> None:
    assert ingest._is_application_form(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # Real BoA statement excerpt — bank statements DON'T trip
        # the heuristic.
        (
            "Bank of America, N.A. Statement Period: 03/01/2026 to "
            "03/31/2026 Account Number: XX-XXXX-1234 Beginning Balance"
        ),
        # Truist statement excerpt with a single legal-context line —
        # one marker is below the threshold.
        (
            "TRUIST BANK Statement Period: April 2026 Account XXXXXX8865 "
            "Tax ID: see enclosed disclosure"
        ),
        # Empty / very short text — no markers, no match.
        ("",),
        ("Bank statement",),
    ],
)
def test_is_application_form_does_not_trip_on_statements(text: tuple[str, ...] | str) -> None:
    # Allow single-string args from parametrize.
    if isinstance(text, tuple):
        text = text[0]
    assert ingest._is_application_form(text) is False


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


# ---------------------------------------------------------------------------
# ZIP ingestion + recursive walk
# ---------------------------------------------------------------------------


def _make_zip(
    zip_path: Path,
    pdfs: dict[str, bytes],
) -> Path:
    """Build a zip archive whose member paths are ``pdfs`` keys (allow
    nested folder structure like ``lead-1/chase.pdf``)."""
    import zipfile as _zipfile

    with _zipfile.ZipFile(zip_path, "w", compression=_zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in pdfs.items():
            zf.writestr(arcname, content)
    return zip_path


def test_recursive_walk_finds_pdfs_in_nested_folders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A folder with nested per-lead subdirs (the shape of the
    operator's Drive ZIP) must yield every PDF — ``rglob`` form."""
    lead_a = tmp_path / "Lead A"
    lead_b = tmp_path / "Lead B" / "statements"
    lead_a.mkdir(parents=True)
    lead_b.mkdir(parents=True)
    _make_pdf_file(lead_a, "chase-jan.pdf", b"lead-a-jan")
    _make_pdf_file(lead_a, "chase-feb.pdf", b"lead-a-feb")
    _make_pdf_file(lead_b, "boa-jan.pdf", b"lead-b-jan")

    _patch_detectors(
        monkeypatch,
        {
            "chase-jan.pdf": _FakeForensicConfig(first_page_text="JPMorgan Chase"),
            "chase-feb.pdf": _FakeForensicConfig(first_page_text="Chase business"),
            "boa-jan.pdf": _FakeForensicConfig(first_page_text="Bank of America"),
        },
    )

    pdfs = ingest._find_pdf_files(tmp_path)
    assert len(pdfs) == 3
    assert {p.name for p in pdfs} == {"chase-jan.pdf", "chase-feb.pdf", "boa-jan.pdf"}

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
    assert summary.files_ingested == 3
    # Per-bank breakdown captured.
    assert summary.bank_counts["JPMorgan Chase Bank, N.A."] == 2
    assert summary.bank_counts["Bank of America, N.A."] == 1


def test_zip_extraction_exercises_existing_folder_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End-to-end ``--zip`` path: the script extracts the archive to a
    tempdir, walks recursively, ingests via the in-memory repos
    (dry-run), and the extracted tempdir is gone after main() returns.

    The PDFs live under nested folders inside the archive — proves the
    extracted tree is walked recursively, same as the ``--folder``
    code path.
    """
    zip_path = tmp_path / "leads.zip"
    _make_zip(
        zip_path,
        {
            "Lead A/chase-jan.pdf": b"zip-chase-jan",
            "Lead A/chase-feb.pdf": b"zip-chase-feb",
            "Lead B/statements/boa-jan.pdf": b"zip-boa-jan",
        },
    )

    _patch_detectors(
        monkeypatch,
        {
            "chase-jan.pdf": _FakeForensicConfig(first_page_text="JPMorgan Chase"),
            "chase-feb.pdf": _FakeForensicConfig(first_page_text="Chase business"),
            "boa-jan.pdf": _FakeForensicConfig(first_page_text="Bank of America"),
        },
    )

    # Spy on _find_pdf_files to confirm the walk root is a tempdir, not
    # the zip path itself, and that the same recursive walker is reused.
    observed_roots: list[Path] = []
    real_find = ingest._find_pdf_files

    def spy_find(root: Path) -> list[Path]:
        observed_roots.append(root)
        return real_find(root)

    monkeypatch.setattr(ingest, "_find_pdf_files", spy_find)

    exit_code = ingest.main(["--zip", str(zip_path)])
    assert exit_code == ingest.EXIT_OK

    # Exactly one walk root, and it was NOT the zip path nor the
    # operator-supplied tmp_path — it was a private tempdir.
    assert len(observed_roots) == 1
    extracted_root = observed_roots[0]
    assert extracted_root != zip_path
    assert extracted_root != tmp_path
    # The tempdir was cleaned up when main() returned.
    assert not extracted_root.exists(), (
        f"zip-extraction tempdir should be removed after main(); still exists: {extracted_root}"
    )

    # Streamed progress lines (3 files) plus the trailing banner.
    out = capsys.readouterr().out
    assert "[1/3]" in out
    assert "[3/3]" in out
    assert "Banks detected:" in out
    assert "JPMorgan Chase Bank, N.A." in out
    assert "Bank of America, N.A." in out


def test_argparse_no_flags_falls_back_to_auto_detect(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither --folder nor --zip → auto-detect via settings.

    With T1 (rclone OneDrive automation), the mutually-exclusive group
    is no longer ``required=True``. When neither flag is given, the
    script calls ``find_corpus_source(settings)`` and exits with
    ``EXIT_CALLER_ERROR`` only when nothing is found.

    Stub the auto-detect to return the "nothing found" tuple so the test
    doesn't depend on the host's actual filesystem state.
    """
    monkeypatch.setattr(ingest, "find_corpus_source", lambda _settings: ("none", None, None))
    rc = ingest.main([])
    assert rc != 0
    err = capsys.readouterr().err
    assert "auto-detect found no corpus" in err


def test_argparse_rejects_both_folder_and_zip(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Passing both --folder and --zip must error out."""
    folder = tmp_path / "folder"
    folder.mkdir()
    zip_path = tmp_path / "x.zip"
    _make_zip(zip_path, {"a.pdf": b"x"})
    with pytest.raises(SystemExit):
        ingest.main(["--folder", str(folder), "--zip", str(zip_path)])
    err = capsys.readouterr().err
    assert "not allowed with argument" in err or "argument --zip" in err


def test_zip_missing_file_returns_caller_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A --zip pointing at a nonexistent file → EXIT_CALLER_ERROR."""
    missing = tmp_path / "nope.zip"
    exit_code = ingest.main(["--zip", str(missing)])
    assert exit_code == ingest.EXIT_CALLER_ERROR
    assert "zip file not found" in capsys.readouterr().err


def test_zip_not_a_zip_returns_caller_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A --zip pointing at a non-zip file → EXIT_CALLER_ERROR."""
    fake = tmp_path / "not-a-zip.zip"
    fake.write_bytes(b"this is not a zip archive")
    exit_code = ingest.main(["--zip", str(fake)])
    assert exit_code == ingest.EXIT_CALLER_ERROR
    assert "not a valid zip archive" in capsys.readouterr().err


def test_render_progress_line_format() -> None:
    """The progress line follows ``[N/total] {file} — bank: X — clean/fraud``."""
    ok_clean = ingest.IngestResult(
        file_path="/x/chase.pdf",
        file_hash="a" * 64,
        bank_name="JPMorgan Chase Bank, N.A.",
        page_count=5,
        fraud_signals_fired=False,
        has_font_inconsistency=False,
        has_text_overlay=False,
        has_creator_mismatch=False,
        hint_updated=True,
        fingerprint_added=True,
        action="ingested",
    )
    line = ingest.render_progress_line(ok_clean, index=1, total=3)
    assert line == "[1/3] chase.pdf — bank: JPMorgan Chase Bank, N.A. — clean"

    fraud = ingest.IngestResult(
        file_path="/x/td.pdf",
        file_hash="b" * 64,
        bank_name="TD Bank, N.A.",
        page_count=4,
        fraud_signals_fired=True,
        has_font_inconsistency=False,
        has_text_overlay=True,
        has_creator_mismatch=False,
        hint_updated=False,
        fingerprint_added=False,
        action="ingested",
    )
    assert (
        ingest.render_progress_line(fraud, index=2, total=3)
        == "[2/3] td.pdf — bank: TD Bank, N.A. — fraud"
    )

    skip = ingest.IngestResult(
        file_path="/x/dup.pdf",
        file_hash="c" * 64,
        bank_name=None,
        page_count=0,
        fraud_signals_fired=False,
        has_font_inconsistency=False,
        has_text_overlay=False,
        has_creator_mismatch=False,
        hint_updated=False,
        fingerprint_added=False,
        action="dedup_skip",
    )
    assert (
        ingest.render_progress_line(skip, index=3, total=3)
        == "[3/3] dup.pdf — bank: unknown — skip (dedup)"
    )
