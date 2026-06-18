"""Tests for ``aegis.funders.monitor``.

Pure-function coverage of the weekly funder folder monitor: graceful
skips on unconfigured / unavailable paths, dedup against existing
guideline hashes, extract + merge + upsert when a new file is dropped
in, audit-row shapes per outcome.
"""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from PIL import Image

from aegis.audit import InMemoryAuditLog
from aegis.funders.models import FunderGuidelineExtraction, FunderRow
from aegis.funders.monitor import run_funder_monitor_pass
from aegis.funders.repository import InMemoryFunderRepository


class _StubLLM:
    """Stub LLM that returns a canned ``FunderGuidelineExtraction`` shape.

    The monitor calls ``extract_funder_guidelines`` /
    ``extract_funder_guidelines_from_image``, both of which delegate to
    ``llm.extract_raw_json{,_from_images}``. We canned the returned
    payload so the test stays Bedrock-free.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.pdf_calls = 0
        self.image_calls = 0

    def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, Any], bool]:
        _ = (pdf_bytes, prompt)
        self.pdf_calls += 1
        return self._payload, False

    def extract_raw_json_from_images(
        self, image_bytes_list: list[bytes], prompt: str
    ) -> tuple[dict[str, Any], bool]:
        _ = (image_bytes_list, prompt)
        self.image_calls += 1
        return self._payload, False

    def classify_batch_json(self, prompt: str) -> dict[str, Any]:
        _ = prompt
        raise NotImplementedError("monitor never classifies")


def _canned_payload(name: str = "Test Funder") -> dict[str, Any]:
    return {
        "draft": {
            "name": name,
            "min_monthly_revenue": 25000,
            "min_credit_score": 550,
            "accepts_stacking": False,
        },
        "confidence_by_field": {"min_monthly_revenue": 95},
        "unparseable_fragments": [],
        "overall_confidence": 88,
    }


def _make_pdf(tmp_path: Path, name: str = "guidelines.pdf", body: bytes = b"hello") -> Path:
    """Write a tiny non-empty file that the monitor will read+hash."""
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n" + body + b"\n%%EOF\n")
    return path


def _make_png(tmp_path: Path, name: str = "guidelines.png") -> Path:
    """Tiny PNG the monitor will hash. Doesn't need to be valid for
    extract — the stub LLM doesn't decode it."""
    image = Image.new("RGB", (4, 4), color=(255, 255, 255))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    path = tmp_path / name
    path.write_bytes(buf.getvalue())
    return path


def _make_existing_funder(
    *,
    name: str = "Test Funder",
    guidelines_hash: str | None = None,
) -> FunderRow:
    return FunderRow(
        id=uuid4(),
        name=name,
        active=True,
        operator_status="active",
        guidelines_source_pdf_hash=guidelines_hash,
        guidelines_extracted_at=datetime.now(UTC) if guidelines_hash else None,
    )


# ---------------------------------------------------------------------------
# Graceful skips
# ---------------------------------------------------------------------------


def test_path_unconfigured_audits_and_returns_zeroed_counters() -> None:
    audit = InMemoryAuditLog()
    repo = InMemoryFunderRepository()
    summary = run_funder_monitor_pass(
        folder_path=None,
        funders_repo=repo,
        llm=_StubLLM(_canned_payload()),
        audit=audit,
    )
    assert summary == {"files_seen": 0, "unchanged": 0, "updated": 0, "failed": 0}
    actions = [e["action"] for e in audit.entries]
    assert actions == ["funder_monitor.path_unconfigured"]


def test_path_empty_string_audits_unconfigured() -> None:
    audit = InMemoryAuditLog()
    summary = run_funder_monitor_pass(
        folder_path="",
        funders_repo=InMemoryFunderRepository(),
        llm=_StubLLM(_canned_payload()),
        audit=audit,
    )
    assert summary == {"files_seen": 0, "unchanged": 0, "updated": 0, "failed": 0}
    assert [e["action"] for e in audit.entries] == ["funder_monitor.path_unconfigured"]


def test_path_missing_audits_unavailable(tmp_path: Path) -> None:
    audit = InMemoryAuditLog()
    summary = run_funder_monitor_pass(
        folder_path=str(tmp_path / "no_such_dir"),
        funders_repo=InMemoryFunderRepository(),
        llm=_StubLLM(_canned_payload()),
        audit=audit,
    )
    assert summary["files_seen"] == 0
    actions = [e["action"] for e in audit.entries]
    assert actions == ["funder_monitor.path_unavailable"]


def test_path_pointing_at_a_file_audits_unavailable(tmp_path: Path) -> None:
    """A path that exists but isn't a directory is treated as unavailable."""
    f = tmp_path / "not_a_dir"
    f.write_bytes(b"")
    audit = InMemoryAuditLog()
    summary = run_funder_monitor_pass(
        folder_path=str(f),
        funders_repo=InMemoryFunderRepository(),
        llm=_StubLLM(_canned_payload()),
        audit=audit,
    )
    assert summary["files_seen"] == 0
    assert audit.entries[0]["action"] == "funder_monitor.path_unavailable"


def test_empty_folder_audits_nothing_and_returns_zero(tmp_path: Path) -> None:
    audit = InMemoryAuditLog()
    summary = run_funder_monitor_pass(
        folder_path=str(tmp_path),
        funders_repo=InMemoryFunderRepository(),
        llm=_StubLLM(_canned_payload()),
        audit=audit,
    )
    assert summary == {"files_seen": 0, "unchanged": 0, "updated": 0, "failed": 0}
    assert audit.entries == []


# ---------------------------------------------------------------------------
# Dedup against existing hashes
# ---------------------------------------------------------------------------


def test_pdf_with_matching_hash_is_skipped_no_llm_call(tmp_path: Path) -> None:
    pdf = _make_pdf(tmp_path)
    pdf_bytes = pdf.read_bytes()
    known_hash = hashlib.sha256(pdf_bytes).hexdigest()

    repo = InMemoryFunderRepository()
    repo.upsert(_make_existing_funder(guidelines_hash=known_hash))
    audit = InMemoryAuditLog()
    llm = _StubLLM(_canned_payload())

    summary = run_funder_monitor_pass(
        folder_path=str(tmp_path),
        funders_repo=repo,
        llm=llm,
        audit=audit,
    )
    assert summary["files_seen"] == 1
    assert summary["unchanged"] == 1
    assert summary["updated"] == 0
    assert llm.pdf_calls == 0
    assert audit.entries == []


def test_non_media_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("ignore me")
    (tmp_path / "manifest.json").write_text("{}")
    audit = InMemoryAuditLog()
    summary = run_funder_monitor_pass(
        folder_path=str(tmp_path),
        funders_repo=InMemoryFunderRepository(),
        llm=_StubLLM(_canned_payload()),
        audit=audit,
    )
    assert summary["files_seen"] == 0


# ---------------------------------------------------------------------------
# Extract + merge + upsert
# ---------------------------------------------------------------------------


def test_new_pdf_for_unknown_funder_inserts_and_audits(tmp_path: Path) -> None:
    """File hash isn't in DB AND no funder matches the extracted name →
    new funder row inserted, ``funder.guidelines_updated`` audit row."""
    _make_pdf(tmp_path, name="brand_new.pdf")
    audit = InMemoryAuditLog()
    repo = InMemoryFunderRepository()
    llm = _StubLLM(_canned_payload(name="Brand New Capital"))

    summary = run_funder_monitor_pass(
        folder_path=str(tmp_path),
        funders_repo=repo,
        llm=llm,
        audit=audit,
    )
    assert summary["files_seen"] == 1
    assert summary["updated"] == 1
    assert llm.pdf_calls == 1

    rows = repo.list_active()
    assert len(rows) == 1
    assert rows[0].name == "Brand New Capital"

    updates = [e for e in audit.entries if e["action"] == "funder.guidelines_updated"]
    assert len(updates) == 1
    details = updates[0]["details"]
    assert details["funder_name"] == "Brand New Capital"
    assert details["merged_with_existing"] is False
    assert details["previous_hash"] is None
    assert isinstance(details["new_hash"], str) and len(details["new_hash"]) == 64


def test_changed_pdf_for_existing_funder_merges_via_preserve_policy(
    tmp_path: Path,
) -> None:
    """Existing funder by name; old hash is different → merge applies, the
    ``PRESERVE_IF_POPULATED`` set keeps operator-curated existing values."""
    _make_pdf(tmp_path, name="updated.pdf", body=b"v2")

    # Existing funder has operator-curated contact info + a different old
    # hash. The new extract supplies a NEW phone number; the
    # PRESERVE_IF_POPULATED policy must keep the old one.
    existing = FunderRow(
        id=uuid4(),
        name="Test Funder",
        active=True,
        operator_status="active",
        guidelines_source_pdf_hash="old_hash_x" * 6 + "deadbeef",  # any stale hash
        contact_phone="555-OLD-LINE",
    )
    repo = InMemoryFunderRepository()
    repo.upsert(existing)

    payload = _canned_payload(name="Test Funder")
    payload["draft"]["contact_phone"] = "555-NEW-LINE"
    audit = InMemoryAuditLog()
    llm = _StubLLM(payload)

    summary = run_funder_monitor_pass(
        folder_path=str(tmp_path),
        funders_repo=repo,
        llm=llm,
        audit=audit,
    )
    assert summary["updated"] == 1

    saved = next(f for f in repo.list_active() if f.name == "Test Funder")
    # PRESERVE_IF_POPULATED protected the operator-curated phone.
    assert saved.contact_phone == "555-OLD-LINE"

    # The merged audit row records the merge.
    update_row = next(e for e in audit.entries if e["action"] == "funder.guidelines_updated")
    assert update_row["details"]["merged_with_existing"] is True
    assert update_row["details"]["previous_hash"].startswith("old_hash_x")


def test_extract_failure_audits_and_skips_to_next_file(tmp_path: Path) -> None:
    """A FunderExtractionError on one file audits but doesn't abort the
    pass — the second file in the folder still gets processed."""
    _make_pdf(tmp_path, name="01_bad.pdf")
    _make_pdf(tmp_path, name="02_good.pdf", body=b"good")

    class _RaisingThenCanned:
        def __init__(self, ok_payload: dict[str, Any]) -> None:
            self._ok = ok_payload
            self._n = 0

        def extract_raw_json(self, pdf_bytes: bytes, prompt: str) -> tuple[dict[str, Any], bool]:
            _ = (pdf_bytes, prompt)
            self._n += 1
            if self._n == 1:
                raise ValueError("malformed_json")
            return self._ok, False

        def extract_raw_json_from_images(
            self, image_bytes_list: list[bytes], prompt: str
        ) -> tuple[dict[str, Any], bool]:
            _ = (image_bytes_list, prompt)
            return self._ok, False

        def classify_batch_json(self, prompt: str) -> dict[str, Any]:  # pragma: no cover
            _ = prompt
            raise NotImplementedError

    repo = InMemoryFunderRepository()
    audit = InMemoryAuditLog()
    llm = _RaisingThenCanned(_canned_payload(name="Good Funder"))

    summary = run_funder_monitor_pass(
        folder_path=str(tmp_path),
        funders_repo=repo,
        llm=llm,
        audit=audit,
    )
    assert summary["files_seen"] == 2
    assert summary["failed"] == 1
    assert summary["updated"] == 1

    actions = [e["action"] for e in audit.entries]
    assert "funder_monitor.extract_failed" in actions
    assert "funder.guidelines_updated" in actions


def test_png_path_routes_through_image_extractor(tmp_path: Path) -> None:
    _make_png(tmp_path)
    audit = InMemoryAuditLog()
    repo = InMemoryFunderRepository()
    llm = _StubLLM(_canned_payload(name="PNG Funder"))

    summary = run_funder_monitor_pass(
        folder_path=str(tmp_path),
        funders_repo=repo,
        llm=llm,
        audit=audit,
    )
    assert summary["updated"] == 1
    assert llm.image_calls == 1
    assert llm.pdf_calls == 0


def test_files_in_subfolders_are_found(tmp_path: Path) -> None:
    """The folder name → funder mapping is operator-discretionary; we
    recursively walk so nested ``Funder/Highland capital/guidelines.pdf``
    layouts work."""
    sub = tmp_path / "Highland capital"
    sub.mkdir()
    _make_pdf(sub, name="guidelines.pdf")

    audit = InMemoryAuditLog()
    repo = InMemoryFunderRepository()
    llm = _StubLLM(_canned_payload(name="Highland Hill Capital"))

    summary = run_funder_monitor_pass(
        folder_path=str(tmp_path),
        funders_repo=repo,
        llm=llm,
        audit=audit,
    )
    assert summary["files_seen"] == 1
    assert summary["updated"] == 1


# ---------------------------------------------------------------------------
# Cron entrypoint smoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_entrypoint_propagates_ctx_dependencies(tmp_path: Path) -> None:
    """The arq entrypoint reads audit / funders / llm out of ``ctx`` and
    runs the same pass. Tests inject in-memory fakes via the dict so the
    cron never reaches the process-wide DI."""
    from aegis.funders.monitor import run_funder_monitor_cron

    repo = InMemoryFunderRepository()
    audit = InMemoryAuditLog()
    llm = _StubLLM(_canned_payload())

    ctx = {
        "audit": audit,
        "funders": repo,
        "llm": llm,
    }
    summary = await run_funder_monitor_cron(ctx)
    # No env path configured → graceful skip is the expected behaviour.
    assert summary == {"files_seen": 0, "unchanged": 0, "updated": 0, "failed": 0}
    assert [e["action"] for e in audit.entries] == ["funder_monitor.path_unconfigured"]


# Pin: ensure FunderGuidelineExtraction stays parseable from our canned
# payload shape — guards against silent schema drift if the model adds a
# required field. If this breaks, the canned _canned_payload() above
# needs to grow alongside the model.
def test_canned_payload_validates_through_funder_guideline_extraction() -> None:
    payload = _canned_payload()
    obj = FunderGuidelineExtraction.model_validate(payload)
    assert obj.draft.name == "Test Funder"
