"""Tests for ``run_track_a_regression_sentinel_cron`` (workers.py, weekly
Mon 06:00 UTC).

The cron walks the live document corpus via ``run_lookback`` (the same
core the operator-facing CLI script invokes) and writes one of two
audit rows per fire:

  * ``track_a_regression_sentinel.clean`` — 0 misses; the operator can
    confirm the sentinel is firing without action items.
  * ``track_a_regression_sentinel.miss_rows_found`` — >=1 miss row;
    surfaces on the dashboard recent-activity feed with sample
    merchant_ids + document_ids so the operator can triage via
    ``docs/STEP_2_CUTOVER_REVIEW.md``.

Tests inject an ``InMemoryDocumentRepository`` via the arq ctx dict +
an ``InMemoryAuditLog`` so the full clean / miss audit shapes are
asserted end-to-end without DB access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from aegis.audit import InMemoryAuditLog
from aegis.storage import DocumentRow, InMemoryDocumentRepository
from aegis.workers import run_track_a_regression_sentinel_cron


def _doc(
    *,
    fraud_score: int,
    metadata_score: int = 0,
    math_score: int = 0,
    metadata_flags: tuple[str, ...] = (),
    all_flags: tuple[str, ...] = (),
    merchant_id: UUID | None = None,
) -> DocumentRow:
    """Construct a DocumentRow shaped for ``run_lookback`` to evaluate."""
    return DocumentRow(
        id=uuid4(),
        file_hash=f"sha-{uuid4().hex}",
        byte_size=1024,
        original_filename="stmt.pdf",
        merchant_id=merchant_id if merchant_id is not None else uuid4(),
        parse_status="manual_review",
        fraud_score=fraud_score,
        # Canonical breakdown keys per parser.pipeline._fraud_score (the
        # ``_score`` suffix is required; the lookback's _read_score_component
        # falls back to suffix-less for legacy rows).
        fraud_score_breakdown={
            "metadata_score": metadata_score,
            "math_score": math_score,
        },
        all_flags=list(all_flags),
        metadata_flags=list(metadata_flags),
        uploaded_at=datetime.now(UTC),
    )


def _seed(repo: InMemoryDocumentRepository, doc: DocumentRow) -> None:
    """Push a DocumentRow into the in-memory repo's ``_docs`` dict."""
    repo._docs[doc.id] = doc


# ----------------------------------------------------------------------
# clean path — every legacy-declinable doc is correctly caught
# ----------------------------------------------------------------------


async def test_cron_emits_clean_audit_when_no_misses() -> None:
    """No documents above the legacy threshold → ``clean`` audit row."""
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    # Three docs all below threshold — none would have been declined,
    # so no rows emitted, no misses.
    for _ in range(3):
        _seed(repo, _doc(fraud_score=30))

    result = await run_track_a_regression_sentinel_cron({"repository": repo, "audit": audit})

    assert result == {"scanned": 0, "miss_count": 0}
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["actor"] == "worker"
    assert entry["action"] == "track_a_regression_sentinel.clean"
    assert entry["subject_type"] == "track_a_regression_sentinel"
    assert entry["details"]["scanned_count"] == 0
    assert entry["details"]["rows_with_legacy_decline"] == 0


async def test_cron_clean_when_track_a_catches_every_legacy_decline() -> None:
    """Docs above the threshold but Track A fails them → not a miss."""
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    # fraud_score=70 (above HARD_DECLINE_THRESHOLD=65) AND metadata_score
    # high enough that Track A returns "fail" — caught by the new engine,
    # so it's NOT a miss.
    _seed(
        repo,
        _doc(
            fraud_score=70,
            metadata_score=90,
            metadata_flags=("editor_detected",),
        ),
    )

    result = await run_track_a_regression_sentinel_cron({"repository": repo, "audit": audit})

    assert result["scanned"] == 1
    assert result["miss_count"] == 0
    assert len(audit.entries) == 1
    assert audit.entries[0]["action"] == "track_a_regression_sentinel.clean"


# ----------------------------------------------------------------------
# miss-rows path
# ----------------------------------------------------------------------


async def test_cron_emits_miss_audit_with_sample_ids() -> None:
    """A doc that meets the legacy decline gate but Track A says clean
    AND Track B reconstruction lands at ``low`` → counts as a miss."""
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    merchant = uuid4()
    miss_doc = _doc(
        fraud_score=70,  # >= HARD_DECLINE_THRESHOLD
        metadata_score=0,
        math_score=0,
        metadata_flags=(),  # Track A says clean
        all_flags=(),  # No PATTERN flags → Track B band=low
        merchant_id=merchant,
    )
    _seed(repo, miss_doc)

    result = await run_track_a_regression_sentinel_cron({"repository": repo, "audit": audit})

    assert result["scanned"] == 1
    assert result["miss_count"] == 1
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["actor"] == "worker"
    assert entry["action"] == "track_a_regression_sentinel.miss_rows_found"
    assert entry["subject_type"] == "track_a_regression_sentinel"
    details = entry["details"]
    assert details["scanned_count"] == 1
    assert details["miss_count"] == 1
    # Sample IDs surface for the operator's triage routing.
    assert details["sample_merchant_ids"] == [str(merchant)]
    assert details["sample_document_ids"] == [str(miss_doc.id)]
    # Pointers to the triage workflow.
    assert details["triage_doc"] == "docs/STEP_2_CUTOVER_REVIEW.md"
    assert "track_a_historical_lookback.py" in details["re_run_cmd"]


async def test_cron_caps_sample_ids_at_20() -> None:
    """30 miss rows → details carry sample[:20] only — the audit row is
    for routing, not bulk export. The full CSV comes from re-running
    the CLI script."""
    repo = InMemoryDocumentRepository()
    audit = InMemoryAuditLog()
    miss_docs = [
        _doc(
            fraud_score=70,
            metadata_score=0,
            math_score=0,
            merchant_id=uuid4(),
        )
        for _ in range(30)
    ]
    for d in miss_docs:
        _seed(repo, d)

    result = await run_track_a_regression_sentinel_cron({"repository": repo, "audit": audit})

    assert result["scanned"] == 30
    assert result["miss_count"] == 30
    entry = audit.entries[0]
    # 30 distinct merchant_ids; sample capped at 20.
    assert len(entry["details"]["sample_merchant_ids"]) == 20
    assert len(entry["details"]["sample_document_ids"]) == 20


# ----------------------------------------------------------------------
# WorkerSettings registration
# ----------------------------------------------------------------------


def test_cron_registered_in_worker_settings_at_mon_0600() -> None:
    """The cron must actually be wired into ``WorkerSettings.cron_jobs``
    or the arq worker won't pick it up. Schedule: weekday=mon, 06:00."""
    from aegis.workers import WorkerSettings

    # arq prefixes the coroutine name with ``cron:`` for its registered
    # job name; match against both shapes so the test doesn't depend on
    # arq's prefix convention.
    target_name = "run_track_a_regression_sentinel_cron"
    matches = [
        job
        for job in WorkerSettings.cron_jobs
        if getattr(job, "name", "") in (target_name, f"cron:{target_name}")
    ]
    assert len(matches) == 1, (
        f"expected one cron registered for {target_name!r}; "
        f"got {[getattr(j, 'name', '?') for j in WorkerSettings.cron_jobs]}"
    )
    cron_job = matches[0]
    # arq stores the schedule values as the literal type passed at
    # construction (string or int) — not normalised to a set.
    assert cron_job.weekday == "mon"
    assert cron_job.hour == 6
    assert cron_job.minute == 0
