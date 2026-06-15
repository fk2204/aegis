"""Unit tests for ``scripts/track_a_historical_lookback.py``.

Covers the pure-function core:

* ``_extract_math_failures`` parses the ``[MATH] ...`` prefix off the
  persisted ``all_flags`` list, leaves other prefixes alone.
* ``evaluate_document`` produces a LookbackRow with the legacy / Track A
  verdict pair AND the ``is_miss`` flag correctly set across all four
  outcome cases: clean-vs-clean, decline-vs-fail (caught),
  decline-vs-review (MISS), decline-vs-clean (MISS).
* ``run_lookback`` only emits rows for documents that would have hit
  the legacy decline gate — clean documents are filtered out.
* CSV header + serialisation lock the output shape so a future reader
  doesn't silently break the operator-facing format.

No DB calls. No real Supabase access. All in-memory.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aegis.parser.pipeline import HARD_DECLINE_THRESHOLD  # noqa: E402
from aegis.storage import DocumentRow  # noqa: E402
from scripts import track_a_historical_lookback as lookback  # noqa: E402


def _doc(
    *,
    fraud_score: int,
    metadata_score: int = 0,
    math_score: int = 0,
    metadata_flags: tuple[str, ...] = (),
    all_flags: tuple[str, ...] = (),
    merchant_id: object = ...,
) -> DocumentRow:
    """Minimal DocumentRow with the integrity-relevant fields populated.

    ``merchant_id`` defaults to a fresh UUID; pass ``None`` explicitly
    to build an orphaned document for the skip-orphans tests.
    """
    mid = uuid4() if merchant_id is ... else merchant_id
    return DocumentRow(
        id=uuid4(),
        file_hash=f"sha256-{uuid4().hex}",
        byte_size=1024,
        original_filename="stmt.pdf",
        merchant_id=mid,
        parse_status="manual_review",
        fraud_score=fraud_score,
        fraud_score_breakdown={"metadata": metadata_score, "math": math_score},
        all_flags=list(all_flags),
        metadata_flags=list(metadata_flags),
        uploaded_at=datetime.now(UTC),
    )


# ----------------------------------------------------------------------
# _extract_math_failures
# ----------------------------------------------------------------------


def test_extract_math_failures_strips_math_prefix() -> None:
    flags = [
        "[META] editor_detected: iText 2.1.7",
        "[MATH] reconciliation_failed_period: expected 1000 got 950",
        "[MATH] future_dated_period: ends 2099-01-01",
        "[PATTERN] customer_concentration: 1 source 65%",
    ]
    out = lookback._extract_math_failures(flags)
    assert out == (
        "reconciliation_failed_period: expected 1000 got 950",
        "future_dated_period: ends 2099-01-01",
    )


def test_extract_math_failures_empty_when_no_math() -> None:
    flags = ["[META] editor_detected: iText"]
    assert lookback._extract_math_failures(flags) == ()


def test_extract_math_failures_handles_empty() -> None:
    assert lookback._extract_math_failures([]) == ()


# ----------------------------------------------------------------------
# evaluate_document — the four corner cases
# ----------------------------------------------------------------------


def test_clean_legacy_clean_track_a_is_not_a_miss() -> None:
    """Document below threshold + Track A clean → no decline either side."""
    doc = _doc(fraud_score=20, metadata_score=10)
    row = lookback.evaluate_document(doc)
    assert row.legacy_would_decline is False
    assert row.track_a_verdict == "clean"
    assert row.is_miss is False


def test_decline_with_strong_metadata_track_a_fails_caught() -> None:
    """fraud_score=80, metadata_score=70 → both legacy and Track A
    block the deal. NOT a miss."""
    doc = _doc(fraud_score=80, metadata_score=70)
    row = lookback.evaluate_document(doc)
    assert row.legacy_would_decline is True
    assert row.track_a_verdict == "fail"
    assert row.track_a_branch == "strong_metadata"
    assert row.is_miss is False


def test_decline_but_track_a_review_is_a_miss() -> None:
    """fraud_score=75 (legacy declines) but metadata-only signals put
    Track A in ``review`` not ``fail`` — Step 2 would let it through.
    REGRESSION."""
    doc = _doc(
        fraud_score=75,
        metadata_score=30,  # medium band
        all_flags=("[MATH] reconciliation_failed_period: drift $5",),
    )
    row = lookback.evaluate_document(doc)
    assert row.legacy_would_decline is True
    assert row.track_a_verdict == "review"
    assert row.is_miss is True


def test_decline_but_track_a_clean_is_a_miss() -> None:
    """fraud_score=66 from pattern signals (not metadata or math) —
    legacy declines but Track A is clean. MISS."""
    doc = _doc(fraud_score=66, metadata_score=5, math_score=0)
    row = lookback.evaluate_document(doc)
    assert row.legacy_would_decline is True
    assert row.track_a_verdict == "clean"
    assert row.is_miss is True


def test_threshold_boundary_strict_gte() -> None:
    """Legacy gate is ``>=``. fraud_score=65 declines; fraud_score=64 doesn't."""
    at = lookback.evaluate_document(_doc(fraud_score=HARD_DECLINE_THRESHOLD))
    just_under = lookback.evaluate_document(_doc(fraud_score=HARD_DECLINE_THRESHOLD - 1))
    assert at.legacy_would_decline is True
    assert just_under.legacy_would_decline is False


def test_custom_threshold_overrides_default() -> None:
    """Operator can widen / narrow the sweep via ``--threshold``."""
    row = lookback.evaluate_document(_doc(fraud_score=50), threshold=50)
    assert row.legacy_would_decline is True


# ----------------------------------------------------------------------
# run_lookback — repository iteration
# ----------------------------------------------------------------------


@dataclass
class _FakeRepo:
    docs: list[DocumentRow]

    def list_documents(
        self,
        *,
        parse_status: object = None,
        merchant_id: object = None,
        limit: int = 100,
    ) -> list[DocumentRow]:
        return self.docs[:limit]


def test_run_lookback_filters_to_legacy_declines_only() -> None:
    """A clean document never appears in the output — the lookback
    answers ``did Track A catch what the legacy rule caught``, not
    ``do they agree on clean deals``."""
    repo = _FakeRepo(
        docs=[
            _doc(fraud_score=10, metadata_score=0),  # clean
            _doc(fraud_score=70, metadata_score=70),  # decline + Track A fail
            _doc(fraud_score=20, metadata_score=5),  # clean
            _doc(
                fraud_score=66,
                metadata_score=30,
                all_flags=("[MATH] reconciliation_failed_period: x",),
            ),  # decline + Track A review (MISS)
        ]
    )
    rows = lookback.run_lookback(repo)
    assert len(rows) == 2
    assert {r.track_a_verdict for r in rows} == {"fail", "review"}


def test_run_lookback_honors_limit() -> None:
    """The repo's ``limit`` is forwarded verbatim — the lookback is
    paginated by the caller's choice, not silently widened."""
    repo = _FakeRepo(docs=[_doc(fraud_score=80, metadata_score=70) for _ in range(20)])
    rows = lookback.run_lookback(repo, limit=5)
    assert len(rows) == 5


# ----------------------------------------------------------------------
# --skip-orphans
# ----------------------------------------------------------------------


def test_run_lookback_default_includes_orphans() -> None:
    """An orphan (merchant_id IS NULL) that legacy would decline AND
    Track A would NOT fail is a miss in the default mode — operators
    running ad-hoc audits still want to see orphans surface."""
    repo = _FakeRepo(
        docs=[
            _doc(
                fraud_score=66,
                metadata_score=30,
                all_flags=("[MATH] reconciliation_failed_period: x",),
                merchant_id=None,
            ),
        ]
    )
    rows = lookback.run_lookback(repo)
    assert len(rows) == 1
    assert rows[0].merchant_id == ""  # serialised empty when no merchant
    assert rows[0].is_miss is True


def test_run_lookback_skip_orphans_drops_orphan_misses() -> None:
    """``skip_orphans=True`` filters documents with merchant_id IS NULL
    BEFORE evaluation — they don't surface as misses and don't
    contribute to the cutover-gate failure count."""
    repo = _FakeRepo(
        docs=[
            # Orphan that WOULD be a miss without skipping
            _doc(
                fraud_score=66,
                metadata_score=30,
                all_flags=("[MATH] reconciliation_failed_period: x",),
                merchant_id=None,
            ),
            # Real-merchant decline that Track A correctly fails (not a miss)
            _doc(fraud_score=80, metadata_score=70),
        ]
    )
    rows = lookback.run_lookback(repo, skip_orphans=True)
    assert len(rows) == 1
    assert rows[0].track_a_verdict == "fail"
    assert rows[0].is_miss is False


def test_run_lookback_skip_orphans_preserves_real_misses() -> None:
    """skip_orphans must NOT silently swallow real regressions on
    merchant-linked documents — orphan filter applies ONLY to NULL
    merchant_id rows."""
    repo = _FakeRepo(
        docs=[
            _doc(
                fraud_score=66,
                metadata_score=30,
                all_flags=("[MATH] reconciliation_failed_period: x",),
            ),
            _doc(fraud_score=20, metadata_score=5, merchant_id=None),  # orphan, clean
        ]
    )
    rows = lookback.run_lookback(repo, skip_orphans=True)
    assert len(rows) == 1
    assert rows[0].is_miss is True


def test_cli_accepts_skip_orphans_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """argparse must accept --skip-orphans without raising. The flag
    threads through to run_lookback via main(); cutover_check passes
    it verbatim."""
    import argparse

    monkeypatch.setattr(sys, "argv", ["lookback", "--skip-orphans"])
    ns = lookback._parse_args()
    assert isinstance(ns, argparse.Namespace)
    assert ns.skip_orphans is True


def test_cli_default_skip_orphans_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default argv (no flag) leaves orphans visible so ad-hoc audits
    catch them."""
    monkeypatch.setattr(sys, "argv", ["lookback"])
    ns = lookback._parse_args()
    assert ns.skip_orphans is False


# ----------------------------------------------------------------------
# CSV output shape
# ----------------------------------------------------------------------


def test_csv_header_is_locked() -> None:
    """The operator's downstream tooling (grep, awk, spreadsheet
    imports) keys off the column names — lock them so a future tweak
    surfaces here, not at the wrong moment."""
    assert lookback._CSV_HEADER == (
        "merchant_id",
        "document_id",
        "original_fraud_score",
        "metadata_score",
        "math_score",
        "legacy_would_decline",
        "track_a_verdict",
        "track_a_branch",
        "miss",
    )


def test_csv_serialises_miss_row_correctly() -> None:
    """A miss row should render with ``miss=true`` so a shell-grep
    ``grep ,true$`` picks it up."""
    row = lookback.evaluate_document(
        _doc(
            fraud_score=66,
            metadata_score=30,
            all_flags=("[MATH] reconciliation_failed_period: x",),
        )
    )
    assert row.is_miss is True

    stream = io.StringIO()
    lookback.write_csv([row], stream)
    output = stream.getvalue()
    lines = output.strip().splitlines()
    assert len(lines) == 2  # header + 1 row
    assert lines[0] == ",".join(lookback._CSV_HEADER)
    assert lines[1].endswith(",true")  # miss flag at end of row
    assert "review" in lines[1]  # track_a_verdict


# ----------------------------------------------------------------------
# Exit-code constants — pin against the documented contract
# ----------------------------------------------------------------------


def test_exit_codes_are_documented_values() -> None:
    """Mirror the shadow-comparison script's exit-3 convention; pin
    so a refactor doesn't silently move the regression-signal code."""
    assert lookback.EXIT_OK == 0
    assert lookback.EXIT_RUNTIME_ERROR == 1
    assert lookback.EXIT_MISSES_PRESENT == 3
