"""Unit tests for ``scripts/add_funder.py``.

Covers the pure-function core (classify_media / low_confidence_fields /
summary_lines / preview round-trip) and the two CLI subcommands wired
via dependency-injection (stub LLM client + in-memory repository).
No Bedrock calls, no Supabase access.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pikepdf
import pytest
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aegis.audit import InMemoryAuditLog  # noqa: E402
from aegis.funders.models import FunderGuidelineExtraction  # noqa: E402
from aegis.funders.repository import InMemoryFunderRepository  # noqa: E402
from scripts import add_funder  # noqa: E402

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


class _StubLLM:
    """Returns a canned extraction payload regardless of input bytes."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, Any], bool]:
        _ = (pdf_bytes, prompt)
        return self._payload, False

    def extract_raw_json_from_images(
        self, page_images_png: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (page_images_png, prompt)
        return self._payload, False

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        _ = prompt
        raise NotImplementedError("add_funder does not run a classification pass")


@pytest.fixture
def small_pdf_bytes(tmp_path: Path) -> bytes:
    """A tiny one-page PDF the extract path can ingest without parsing."""
    p = tmp_path / "guidelines.pdf"
    pdf = pikepdf.Pdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.save(str(p))
    pdf.close()
    return p.read_bytes()


@pytest.fixture
def stub_payload() -> dict[str, Any]:
    return {
        "draft": {
            "name": "Acme Capital Funding",
            "min_monthly_revenue": 25000,
            "min_avg_daily_balance": 3000,
            "min_credit_score": 580,
            "min_months_in_business": 12,
            "max_positions": 1,
            "accepts_stacking": False,
            "min_advance": 5000,
            "max_advance": 250000,
            "max_nsf_tolerance": 5,
            "typical_factor_low": 1.25,
            "typical_factor_high": 1.42,
            "typical_holdback_low": 0.10,
            "typical_holdback_high": 0.18,
            "excluded_industries": ["adult-entertainment"],
            "excluded_states": ["CA"],
            "notes": "focus on retail",
        },
        "confidence_by_field": {
            "min_monthly_revenue": 95,
            "min_credit_score": 92,
            "min_months_in_business": 88,
            "max_positions": 80,
            "typical_holdback_low": 55,  # below threshold
            "typical_holdback_high": 50,  # below threshold
        },
        "unparseable_fragments": ["renewals: case-by-case"],
        "overall_confidence": 78,
    }


@pytest.fixture
def stub_llm(stub_payload: dict[str, Any]) -> Iterator[_StubLLM]:
    yield _StubLLM(json.loads(json.dumps(stub_payload)))


@pytest.fixture
def stub_extraction(small_pdf_bytes: bytes, stub_llm: _StubLLM) -> FunderGuidelineExtraction:
    """Exercise the real extract path against the stub LLM so the
    Pydantic-validated FunderGuidelineExtraction in tests matches the
    same shape the CLI emits at runtime."""
    return add_funder.extract_one(small_pdf_bytes, "pdf", stub_llm)


# ----------------------------------------------------------------------
# classify_media — pure
# ----------------------------------------------------------------------


def test_classify_media_pdf() -> None:
    assert add_funder.classify_media(Path("shor.pdf")) == "pdf"
    assert add_funder.classify_media(Path("guidelines.PDF")) == "pdf"


def test_classify_media_image_variants() -> None:
    assert add_funder.classify_media(Path("x.png")) == "image"
    assert add_funder.classify_media(Path("x.PNG")) == "image"
    assert add_funder.classify_media(Path("x.jpg")) == "image"
    assert add_funder.classify_media(Path("x.jpeg")) == "image"


def test_classify_media_unknown_returns_empty() -> None:
    assert add_funder.classify_media(Path("x.docx")) == ""
    assert add_funder.classify_media(Path("noext")) == ""


# ----------------------------------------------------------------------
# low_confidence_fields + summary_lines
# ----------------------------------------------------------------------


def test_low_confidence_fields_orders_ascending_under_threshold(
    stub_extraction: FunderGuidelineExtraction,
) -> None:
    low = add_funder.low_confidence_fields(stub_extraction)
    # typical_holdback_high (50) before typical_holdback_low (55).
    assert low == ("typical_holdback_high", "typical_holdback_low")


def test_low_confidence_fields_respects_custom_threshold(
    stub_extraction: FunderGuidelineExtraction,
) -> None:
    # threshold above max_positions (80) but below min_months_in_business (88)
    low = add_funder.low_confidence_fields(stub_extraction, threshold=85)
    assert "max_positions" in low
    assert "min_credit_score" not in low  # 92 stays above threshold


def test_low_confidence_fields_empty_when_all_above() -> None:
    extraction = FunderGuidelineExtraction(
        draft={"name": "X"},
        confidence_by_field={"name": 99},
        unparseable_fragments=[],
        overall_confidence=99,
    )
    assert add_funder.low_confidence_fields(extraction) == ()


def test_summary_lines_includes_low_and_unparseable(
    stub_extraction: FunderGuidelineExtraction,
) -> None:
    lines = add_funder.summary_lines(stub_extraction)
    text = "\n".join(lines)
    assert "funder: Acme Capital Funding" in text
    assert "overall_confidence: 78" in text
    assert "typical_holdback_high" in text  # surfaced as low-confidence
    assert "renewals: case-by-case" in text  # unparseable fragment


def test_summary_lines_says_none_when_all_confident() -> None:
    extraction = FunderGuidelineExtraction(
        draft={"name": "X"},
        confidence_by_field={"name": 99},
        unparseable_fragments=[],
        overall_confidence=99,
    )
    lines = add_funder.summary_lines(extraction)
    assert any("low_confidence_fields (<60): none" in line for line in lines)


# ----------------------------------------------------------------------
# JSON round-trip — preserves Decimal / UUID / datetime via Pydantic
# ----------------------------------------------------------------------


def test_preview_json_roundtrip_preserves_draft(
    stub_extraction: FunderGuidelineExtraction,
) -> None:
    blob = add_funder.preview_to_json(stub_extraction)
    restored = add_funder.preview_from_json(blob)
    assert restored.draft.id == stub_extraction.draft.id
    assert restored.draft.name == stub_extraction.draft.name
    assert restored.draft.min_monthly_revenue == stub_extraction.draft.min_monthly_revenue
    assert restored.confidence_by_field == stub_extraction.confidence_by_field
    assert restored.overall_confidence == stub_extraction.overall_confidence


def test_preview_from_json_rejects_malformed() -> None:
    with pytest.raises(ValidationError):
        add_funder.preview_from_json('{"draft": "not-an-object"}')


# ----------------------------------------------------------------------
# extract_many — merge path
# ----------------------------------------------------------------------


def test_extract_many_single_item_is_pass_through(
    small_pdf_bytes: bytes, stub_llm: _StubLLM
) -> None:
    out = add_funder.extract_many([(small_pdf_bytes, "pdf")], stub_llm)
    assert out.draft.name == "Acme Capital Funding"


def test_extract_many_empty_raises() -> None:
    with pytest.raises(ValueError, match="no documents"):
        add_funder.extract_many([], _StubLLM({}))


def test_extract_one_unknown_kind_raises(small_pdf_bytes: bytes) -> None:
    with pytest.raises(ValueError, match="unknown media kind"):
        add_funder.extract_one(small_pdf_bytes, "xml", _StubLLM({}))


# ----------------------------------------------------------------------
# save_extraction — repo upsert pass-through
# ----------------------------------------------------------------------


def test_save_extraction_writes_via_repo(
    stub_extraction: FunderGuidelineExtraction,
) -> None:
    repo = InMemoryFunderRepository()
    saved = add_funder.save_extraction(stub_extraction, repo)
    assert saved.id == stub_extraction.draft.id
    assert saved.name == "Acme Capital Funding"
    # Verify the repo actually has the row by id.
    assert repo.get(saved.id).name == saved.name


def test_save_extraction_surfaces_uniqueness_conflict(
    stub_extraction: FunderGuidelineExtraction,
) -> None:
    repo = InMemoryFunderRepository()
    repo.upsert(stub_extraction.draft)
    # Build a new extraction with the same NAME but a fresh id — repo
    # treats it as a name collision and raises ValueError.
    duplicate_draft = stub_extraction.draft.model_copy(
        update={"id": stub_extraction.draft.id.__class__(int=42)}
    )
    duplicate = stub_extraction.model_copy(update={"draft": duplicate_draft})
    with pytest.raises(ValueError, match="funder name conflict"):
        add_funder.save_extraction(duplicate, repo)


# ----------------------------------------------------------------------
# run_extract — CLI body via DI
# ----------------------------------------------------------------------


def test_run_extract_writes_json_to_stdout_and_summary_to_stderr(
    small_pdf_bytes: bytes, stub_llm: _StubLLM, tmp_path: Path
) -> None:
    file_path = tmp_path / "guidelines.pdf"
    file_path.write_bytes(small_pdf_bytes)
    args = argparse.Namespace(command="extract", files=[file_path], output=None)
    out_buf, err_buf = io.StringIO(), io.StringIO()

    def _reader(path: Path) -> tuple[bytes, str]:
        return path.read_bytes(), "pdf"

    rc = add_funder.run_extract(
        args,
        llm_factory=lambda: stub_llm,
        bytes_reader=_reader,
        stdout=out_buf,
        stderr=err_buf,
    )

    assert rc == add_funder.EXIT_OK
    payload = json.loads(out_buf.getvalue())
    assert payload["draft"]["name"] == "Acme Capital Funding"
    summary = err_buf.getvalue()
    assert "funder: Acme Capital Funding" in summary
    assert "low_confidence_fields" in summary


def test_run_extract_with_output_path_writes_file(
    small_pdf_bytes: bytes, stub_llm: _StubLLM, tmp_path: Path
) -> None:
    file_path = tmp_path / "g.pdf"
    file_path.write_bytes(small_pdf_bytes)
    output = tmp_path / "preview.json"
    args = argparse.Namespace(command="extract", files=[file_path], output=output)
    out_buf, err_buf = io.StringIO(), io.StringIO()

    def _reader(path: Path) -> tuple[bytes, str]:
        return path.read_bytes(), "pdf"

    rc = add_funder.run_extract(
        args,
        llm_factory=lambda: stub_llm,
        bytes_reader=_reader,
        stdout=out_buf,
        stderr=err_buf,
    )

    assert rc == add_funder.EXIT_OK
    assert out_buf.getvalue() == ""  # nothing on stdout when --output set
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["draft"]["name"] == "Acme Capital Funding"


def test_run_extract_unknown_media_returns_error(stub_llm: _StubLLM, tmp_path: Path) -> None:
    bad_path = tmp_path / "guidelines.docx"
    bad_path.write_bytes(b"not-a-pdf")
    args = argparse.Namespace(command="extract", files=[bad_path], output=None)
    out_buf, err_buf = io.StringIO(), io.StringIO()

    def _reader(path: Path) -> tuple[bytes, str]:
        kind = add_funder.classify_media(path)
        if kind == "":
            raise ValueError(f"unsupported media for {path.name!r}")
        return path.read_bytes(), kind

    rc = add_funder.run_extract(
        args,
        llm_factory=lambda: stub_llm,
        bytes_reader=_reader,
        stdout=out_buf,
        stderr=err_buf,
    )

    assert rc == add_funder.EXIT_RUNTIME_ERROR
    assert "unsupported media" in err_buf.getvalue()


# ----------------------------------------------------------------------
# run_save — CLI body via DI
# ----------------------------------------------------------------------


def test_run_save_upserts_via_repo(
    stub_extraction: FunderGuidelineExtraction, tmp_path: Path
) -> None:
    preview_path = tmp_path / "preview.json"
    preview_path.write_text(add_funder.preview_to_json(stub_extraction), encoding="utf-8")
    args = argparse.Namespace(command="save", from_path=preview_path, dry_run=False)
    repo = InMemoryFunderRepository()
    audit = InMemoryAuditLog()
    out_buf, err_buf = io.StringIO(), io.StringIO()

    rc = add_funder.run_save(
        args,
        repo_factory=lambda: repo,
        audit_factory=lambda: audit,
        stdout=out_buf,
        stderr=err_buf,
    )

    assert rc == add_funder.EXIT_OK
    assert "saved funder" in out_buf.getvalue()
    assert "Acme Capital Funding" in out_buf.getvalue()
    # Repo received the upsert.
    assert repo.get(stub_extraction.draft.id).name == "Acme Capital Funding"


def test_run_save_dry_run_skips_repo(
    stub_extraction: FunderGuidelineExtraction, tmp_path: Path
) -> None:
    preview_path = tmp_path / "preview.json"
    preview_path.write_text(add_funder.preview_to_json(stub_extraction), encoding="utf-8")
    args = argparse.Namespace(command="save", from_path=preview_path, dry_run=True)
    out_buf, err_buf = io.StringIO(), io.StringIO()

    called: list[str] = []

    def _factory() -> InMemoryFunderRepository:
        called.append("yes")
        return InMemoryFunderRepository()

    rc = add_funder.run_save(
        args,
        repo_factory=_factory,
        stdout=out_buf,
        stderr=err_buf,
    )

    assert rc == add_funder.EXIT_OK
    assert called == []  # factory never invoked
    assert "DRY-RUN" in out_buf.getvalue()


def test_run_save_missing_file_returns_error(tmp_path: Path) -> None:
    args = argparse.Namespace(
        command="save", from_path=tmp_path / "does-not-exist.json", dry_run=False
    )
    out_buf, err_buf = io.StringIO(), io.StringIO()
    rc = add_funder.run_save(
        args,
        repo_factory=lambda: InMemoryFunderRepository(),
        stdout=out_buf,
        stderr=err_buf,
    )
    assert rc == add_funder.EXIT_RUNTIME_ERROR
    assert "could not read preview" in err_buf.getvalue()


def test_run_save_malformed_json_returns_error(tmp_path: Path) -> None:
    preview_path = tmp_path / "preview.json"
    preview_path.write_text("{not-json", encoding="utf-8")
    args = argparse.Namespace(command="save", from_path=preview_path, dry_run=False)
    out_buf, err_buf = io.StringIO(), io.StringIO()
    rc = add_funder.run_save(
        args,
        repo_factory=lambda: InMemoryFunderRepository(),
        stdout=out_buf,
        stderr=err_buf,
    )
    assert rc == add_funder.EXIT_RUNTIME_ERROR
    err_text = err_buf.getvalue()
    assert "ERROR" in err_text


def test_run_save_repo_upsert_failure_returns_error(
    stub_extraction: FunderGuidelineExtraction, tmp_path: Path
) -> None:
    preview_path = tmp_path / "preview.json"
    preview_path.write_text(add_funder.preview_to_json(stub_extraction), encoding="utf-8")
    args = argparse.Namespace(command="save", from_path=preview_path, dry_run=False)
    out_buf, err_buf = io.StringIO(), io.StringIO()

    # Pre-populate a different funder with the SAME name — triggers
    # InMemoryFunderRepository's uniqueness check and a ValueError on
    # upsert, which the CLI catches.
    repo = InMemoryFunderRepository()
    conflict = stub_extraction.draft.model_copy(
        update={"id": stub_extraction.draft.id.__class__(int=99)}
    )
    repo.upsert(conflict)

    rc = add_funder.run_save(
        args,
        repo_factory=lambda: repo,
        stdout=out_buf,
        stderr=err_buf,
    )

    assert rc == add_funder.EXIT_RUNTIME_ERROR
    assert "upsert failed" in err_buf.getvalue()


def test_run_save_emits_audit_row(
    stub_extraction: FunderGuidelineExtraction, tmp_path: Path
) -> None:
    """Happy path: after a successful upsert, ``run_save`` writes a
    ``funder.imported`` audit row via the injected ``audit_factory``.
    Mirrors the route-side audit emit; CLAUDE.md requires audit rows
    for every state change."""
    preview_path = tmp_path / "preview.json"
    preview_path.write_text(add_funder.preview_to_json(stub_extraction), encoding="utf-8")
    args = argparse.Namespace(command="save", from_path=preview_path, dry_run=False)
    repo = InMemoryFunderRepository()
    audit = InMemoryAuditLog()
    out_buf, err_buf = io.StringIO(), io.StringIO()

    rc = add_funder.run_save(
        args,
        repo_factory=lambda: repo,
        audit_factory=lambda: audit,
        stdout=out_buf,
        stderr=err_buf,
    )

    assert rc == add_funder.EXIT_OK
    rows = [e for e in audit.entries if e["action"] == "funder.imported"]
    assert len(rows) == 1
    row = rows[0]
    assert row["actor"] == "claude_code"
    assert row["subject_type"] == "funder"
    assert row["subject_id"] == str(stub_extraction.draft.id)
    assert row["details"]["funder_name"] == stub_extraction.draft.name
    assert row["details"]["source"] == "scripts/add_funder.py"


def test_run_save_audit_failure_fails_the_command(
    stub_extraction: FunderGuidelineExtraction, tmp_path: Path
) -> None:
    """Per CLAUDE.md: audit-write failures FAIL the operation, never
    silently log-and-continue. A raising audit_factory must surface as
    ``EXIT_RUNTIME_ERROR`` with a clear stderr message even though the
    upsert itself succeeded."""
    preview_path = tmp_path / "preview.json"
    preview_path.write_text(add_funder.preview_to_json(stub_extraction), encoding="utf-8")
    args = argparse.Namespace(command="save", from_path=preview_path, dry_run=False)
    repo = InMemoryFunderRepository()
    out_buf, err_buf = io.StringIO(), io.StringIO()

    def _broken_audit() -> InMemoryAuditLog:
        raise RuntimeError("audit backend offline")

    rc = add_funder.run_save(
        args,
        repo_factory=lambda: repo,
        audit_factory=_broken_audit,
        stdout=out_buf,
        stderr=err_buf,
    )

    assert rc == add_funder.EXIT_RUNTIME_ERROR
    err_text = err_buf.getvalue()
    assert "audit emit failed" in err_text
    assert "audit backend offline" in err_text
    # The upsert itself DID happen — the audit failure leaves the repo
    # in a state where the funder exists but no audit row was written.
    # That's the explicit failure path; reconciliation is operator-level.
    assert repo.get(stub_extraction.draft.id).name == stub_extraction.draft.name


# ----------------------------------------------------------------------
# main — argparse dispatch
# ----------------------------------------------------------------------


def test_main_no_subcommand_exits_with_argparse_error() -> None:
    with pytest.raises(SystemExit) as exc:
        add_funder.main([])
    assert exc.value.code != 0


def test_main_extract_dispatches_to_run_extract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    f = tmp_path / "g.pdf"
    f.write_bytes(b"x")
    called: dict[str, object] = {}

    def _fake_run(args: argparse.Namespace) -> int:
        called["command"] = args.command
        return 0

    monkeypatch.setattr(add_funder, "run_extract", _fake_run)
    rc = add_funder.main(["extract", str(f)])
    assert rc == 0
    assert called["command"] == "extract"


def test_main_save_dispatches_to_run_save(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    def _fake_run(args: argparse.Namespace) -> int:
        called["command"] = args.command
        return 0

    monkeypatch.setattr(add_funder, "run_save", _fake_run)
    rc = add_funder.main(["save", "--from", str(tmp_path / "x.json")])
    assert rc == 0
    assert called["command"] == "save"


# ----------------------------------------------------------------------
# merge subcommand
# ----------------------------------------------------------------------


def _existing_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "Acme Capital",
        "active": True,
        "operator_status": "active",
        "min_monthly_revenue": 40000,
        "min_credit_score": None,
        "min_months_in_business": None,
        "max_positions": None,
        "accepts_stacking": True,  # existing said True
        "min_advance": None,
        "max_advance": None,
        "typical_factor_low": None,
        "typical_factor_high": None,
        "excluded_industries": [],
        "excluded_states": [],
        "deal_types_accepted": [],
        "contact_name": "",
        "contact_phone": "646-OLD-LINE",  # existing populated
        "contact_email": "",
        "submission_email": "",
        "tiers": [],
        "auto_decline_conditions": [],
        "conditional_requirements": [],
        "notes": "",
        "notes_residual": "",
        "operator_notes": "",
        "guidelines_extracted_at": "2026-06-10T00:00:00+00:00",
        "guidelines_source_pdf_hash": "old",
        "created_at": "2026-06-10T00:00:00+00:00",
        "updated_at": "2026-06-10T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def _preview_blob(**draft_overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "id": "99999999-9999-9999-9999-999999999999",
        "name": "ACME LLC",
        "active": True,
        "operator_status": "active",
        "min_monthly_revenue": 25000,
        "min_credit_score": 500,
        "min_months_in_business": 12,
        "max_positions": None,
        "accepts_stacking": False,  # would have overwritten existing True
        "min_advance": "5000",
        "max_advance": "250000",
        "typical_factor_low": "1.3",
        "typical_factor_high": "1.5",
        "excluded_industries": [],
        "excluded_states": [],
        "deal_types_accepted": ["mca"],
        "contact_name": "",
        "contact_phone": "855-NEW-LINE",  # would have overwritten existing
        "contact_email": "",
        "submission_email": "",
        "tiers": [],
        "auto_decline_conditions": [],
        "conditional_requirements": ["application", "bank statements"],
        "notes": "",
        "notes_residual": "fresh extract",
        "operator_notes": "",
        "guidelines_extracted_at": "2026-06-18T12:00:00+00:00",
        "guidelines_source_pdf_hash": "new",
    }
    draft.update(draft_overrides)
    return {
        "draft": draft,
        "confidence_by_field": {},
        "unparseable_fragments": [],
        "overall_confidence": 80,
    }


def test_run_merge_preserves_operator_curated_fields(tmp_path: Path) -> None:
    """Headline contract: PRESERVE_IF_POPULATED fields keep existing values."""
    preview_path = tmp_path / "preview.json"
    preview_path.write_text(json.dumps(_preview_blob()), encoding="utf-8")

    existing = _existing_row()

    def fake_loader(by: str, key: str) -> dict[str, Any]:
        assert by == "name"
        assert key == "ACME LLC"
        return existing

    args = argparse.Namespace(preview_path=preview_path, by="name")
    rc = add_funder.run_merge(args, existing_loader=fake_loader)
    assert rc == 0

    merged = json.loads(preview_path.read_text(encoding="utf-8"))
    draft = merged["draft"]
    # id / name swapped back to existing
    assert draft["id"] == "11111111-1111-1111-1111-111111111111"
    assert draft["name"] == "Acme Capital"
    # PRESERVE_IF_POPULATED held the line
    assert draft["accepts_stacking"] is True  # not False
    assert draft["contact_phone"] == "646-OLD-LINE"  # not 855-NEW-LINE
    # Default merge took new values for non-preserved fields
    assert draft["min_monthly_revenue"] == 25000
    assert draft["typical_factor_low"] == "1.3"


def test_run_merge_missing_existing_returns_error(tmp_path: Path) -> None:
    preview_path = tmp_path / "preview.json"
    preview_path.write_text(json.dumps(_preview_blob()), encoding="utf-8")

    def fake_loader(by: str, key: str) -> dict[str, Any]:
        raise ValueError(f"no funder with {by}={key!r}")

    args = argparse.Namespace(preview_path=preview_path, by="name")
    rc = add_funder.run_merge(args, existing_loader=fake_loader, stderr=io.StringIO())
    assert rc == add_funder.EXIT_RUNTIME_ERROR


def test_run_merge_malformed_preview_returns_error(tmp_path: Path) -> None:
    preview_path = tmp_path / "preview.json"
    preview_path.write_text("{ not valid json", encoding="utf-8")

    def fake_loader(by: str, key: str) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("loader must not be called when preview is malformed")

    args = argparse.Namespace(preview_path=preview_path, by="name")
    rc = add_funder.run_merge(args, existing_loader=fake_loader, stderr=io.StringIO())
    assert rc == add_funder.EXIT_RUNTIME_ERROR


def test_run_merge_missing_lookup_key_returns_error(tmp_path: Path) -> None:
    """preview.draft.name empty + by=name → error before any DB call."""
    blob = _preview_blob()
    blob["draft"]["name"] = ""
    preview_path = tmp_path / "preview.json"
    preview_path.write_text(json.dumps(blob), encoding="utf-8")

    def fake_loader(by: str, key: str) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("loader must not be called when lookup key is missing")

    args = argparse.Namespace(preview_path=preview_path, by="name")
    rc = add_funder.run_merge(args, existing_loader=fake_loader, stderr=io.StringIO())
    assert rc == add_funder.EXIT_RUNTIME_ERROR


def test_run_merge_by_id_uses_existing_id_field(tmp_path: Path) -> None:
    """by=id should look up via preview.draft.id, not name."""
    preview_path = tmp_path / "preview.json"
    preview_path.write_text(json.dumps(_preview_blob()), encoding="utf-8")
    existing = _existing_row()

    def fake_loader(by: str, key: str) -> dict[str, Any]:
        assert by == "id"
        assert key == "99999999-9999-9999-9999-999999999999"
        return existing

    args = argparse.Namespace(preview_path=preview_path, by="id")
    rc = add_funder.run_merge(args, existing_loader=fake_loader)
    assert rc == 0


def test_main_merge_dispatches_to_run_merge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    called: dict[str, object] = {}

    def _fake_run(args: argparse.Namespace) -> int:
        called["command"] = args.command
        return 0

    monkeypatch.setattr(add_funder, "run_merge", _fake_run)
    rc = add_funder.main(["merge", "--preview", str(tmp_path / "p.json"), "--by", "name"])
    assert rc == 0
    assert called["command"] == "merge"
