"""Weekly funder folder monitor cron.

Scans the operator's funder-guidelines folder for new or changed PDFs and
PNGs and re-runs the extract + merge pipeline on each. The goal is to
catch funder-side criteria updates without manual operator intervention —
when a funder publishes a revised guidelines sheet and the operator drops
the file into the folder, the following Monday's run picks it up
automatically.

Graceful skip on path unavailability is the design, not a bug. The folder
lives on the operator's Windows OneDrive sync; on prod the path is
typically unmounted, so the cron audits
``funder_monitor.path_unavailable`` and returns zero work. When the
operator runs an arq worker on their local Windows box, the same cron
tick produces real work against the synced folder.

Idempotency: the ``guidelines_source_pdf_hash`` SHA-256 on each funder
row gates re-extraction. A file whose hash matches an existing funder is
skipped at near-zero cost (just one read + hash compute). Only NEW or
CHANGED files reach Bedrock.

Operator-curated fields are protected by the same
``PRESERVE_IF_POPULATED`` set the ``add_funder.py merge`` subcommand uses
— a re-extraction never silently overwrites operator-curated content.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from aegis.audit import AuditLog
from aegis.config import get_settings
from aegis.funders.extract import (
    FunderExtractionError,
    extract_funder_guidelines,
    extract_funder_guidelines_from_image,
)
from aegis.funders.merge_existing import merge_preview_with_existing
from aegis.funders.models import FunderRow
from aegis.funders.repository import FunderRepository
from aegis.llm import LLMClient

_log = logging.getLogger(__name__)

_PDF_SUFFIXES: frozenset[str] = frozenset({".pdf"})
_IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg"})


def _classify_media(path: Path) -> str:
    """Return ``"pdf"`` / ``"image"`` / ``""`` from the file suffix."""
    suffix = path.suffix.lower()
    if suffix in _PDF_SUFFIXES:
        return "pdf"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    return ""


def run_funder_monitor_pass(
    *,
    folder_path: str | None,
    funders_repo: FunderRepository,
    llm: LLMClient,
    audit: AuditLog,
) -> dict[str, int]:
    """Walk ``folder_path`` once and reconcile against the funders table.

    Counters returned:

    * ``files_seen`` — PDFs + PNGs/JPEGs found, recursive
    * ``unchanged`` — file's SHA-256 matched an existing funder's
      ``guidelines_source_pdf_hash``; no work
    * ``updated`` — extract + merge + upsert + ``funder.guidelines_updated``
      audit row written
    * ``failed`` — extraction or upsert errored; per-file
      ``funder_monitor.*_failed`` audit row, processing continues

    Graceful skip writes ``funder_monitor.path_unconfigured`` (no env
    setting) or ``funder_monitor.path_unavailable`` (path missing /
    not a directory) and returns zeroed counters.
    """
    summary: dict[str, int] = {
        "files_seen": 0,
        "unchanged": 0,
        "updated": 0,
        "failed": 0,
    }

    if not folder_path:
        audit.record(
            actor="funder_monitor",
            action="funder_monitor.path_unconfigured",
            details={},
        )
        return summary

    root = Path(folder_path)
    if not root.is_dir():
        audit.record(
            actor="funder_monitor",
            action="funder_monitor.path_unavailable",
            details={"path": str(root)},
        )
        return summary

    existing_rows = funders_repo.list_active()
    hash_to_funder: dict[str, FunderRow] = {
        f.guidelines_source_pdf_hash: f for f in existing_rows if f.guidelines_source_pdf_hash
    }
    name_to_funder: dict[str, FunderRow] = {f.name.lower(): f for f in existing_rows}

    # Deterministic order so audit-log replay is reproducible across runs.
    candidates = sorted(p for p in root.rglob("*") if p.is_file())
    for path in candidates:
        kind = _classify_media(path)
        if not kind:
            continue
        summary["files_seen"] += 1

        try:
            content = path.read_bytes()
        except OSError as exc:
            summary["failed"] += 1
            audit.record(
                actor="funder_monitor",
                action="funder_monitor.read_failed",
                details={
                    "path": str(path),
                    "error": type(exc).__name__,
                    "message": str(exc)[:200],
                },
            )
            continue

        file_hash = hashlib.sha256(content).hexdigest()
        if file_hash in hash_to_funder:
            summary["unchanged"] += 1
            continue

        try:
            if kind == "pdf":
                extraction = extract_funder_guidelines(content, llm)
            else:
                extraction = extract_funder_guidelines_from_image(content, llm)
        except FunderExtractionError as exc:
            summary["failed"] += 1
            audit.record(
                actor="funder_monitor",
                action="funder_monitor.extract_failed",
                details={"path": str(path), "error": str(exc)[:300]},
            )
            continue

        extracted_name = extraction.draft.name
        existing_funder = name_to_funder.get(extracted_name.lower()) if extracted_name else None

        if existing_funder is not None:
            existing_dict = existing_funder.model_dump(mode="json")
            preview_dict = {
                "draft": extraction.draft.model_dump(mode="json"),
                "confidence_by_field": dict(extraction.confidence_by_field),
                "unparseable_fragments": list(extraction.unparseable_fragments),
                "overall_confidence": extraction.overall_confidence,
            }
            merged = merge_preview_with_existing(existing_dict, preview_dict)
            try:
                draft = FunderRow.model_validate(merged["draft"])
            except Exception as exc:
                summary["failed"] += 1
                audit.record(
                    actor="funder_monitor",
                    action="funder_monitor.merge_failed",
                    details={
                        "path": str(path),
                        "funder_name": existing_funder.name,
                        "error": str(exc)[:300],
                    },
                )
                continue
        else:
            draft = extraction.draft

        try:
            saved = funders_repo.upsert(draft)
        except Exception as exc:
            summary["failed"] += 1
            audit.record(
                actor="funder_monitor",
                action="funder_monitor.upsert_failed",
                details={
                    "path": str(path),
                    "funder_name": draft.name,
                    "error": str(exc)[:300],
                },
            )
            continue

        summary["updated"] += 1
        audit.record(
            actor="funder_monitor",
            action="funder.guidelines_updated",
            subject_type="funder",
            subject_id=saved.id,
            details={
                "funder_name": saved.name,
                "source_path": str(path),
                "previous_hash": (
                    existing_funder.guidelines_source_pdf_hash if existing_funder else None
                ),
                "new_hash": file_hash,
                "merged_with_existing": existing_funder is not None,
            },
        )

    return summary


async def run_funder_monitor_cron(ctx: dict[str, Any]) -> dict[str, int]:
    """arq weekly cron entrypoint.

    Mirrors the renewal-reminder cron's DI pattern: reads collaborators
    from the arq ``ctx`` (tests inject in-memory fakes), falls back to
    the process-wide DI when not present.
    """
    from aegis.api.deps import get_audit, get_funder_repository, get_llm

    audit = ctx.get("audit") or get_audit()
    funders = ctx.get("funders") or get_funder_repository()
    llm = ctx.get("llm") or get_llm()

    folder = get_settings().aegis_funder_monitor_path

    summary = run_funder_monitor_pass(
        folder_path=folder,
        funders_repo=funders,
        llm=llm,
        audit=audit,
    )
    _log.info(
        "funder_monitor.run files_seen=%s unchanged=%s updated=%s failed=%s",
        summary["files_seen"],
        summary["unchanged"],
        summary["updated"],
        summary["failed"],
    )
    return summary


__all__ = [
    "run_funder_monitor_cron",
    "run_funder_monitor_pass",
]
