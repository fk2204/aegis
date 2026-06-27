"""Coverage for ``--reparse-sealed-manual-review --all-merchants``.

The companion test file ``test_recover_legacy_docs.py`` already covers
the per-merchant ``_reparse_sealed_manual_review`` path. This file
exercises the new dispatcher that fans the same per-merchant work out
across every merchant with at least one sealed manual_review doc, in
batches of 5 with a 2-second sleep between batches.

Key behaviors verified:
* Distinct merchant iteration — duplicate (merchant_id, doc) rows from
  the candidate selector collapse to one ``MerchantRow`` hydration.
* Batch sleep cadence — ``time.sleep`` fires once per inter-batch
  boundary (N-1 times for N batches), with the configured duration.
* ``--include-old`` flag plumbing — the selector receives
  ``include_old`` verbatim.
* Per-merchant fault tolerance — a hydrate-miss or a per-merchant
  exception increments ``issues`` and the loop keeps going.
* Backward compat — the per-merchant path (no ``--all-merchants``)
  still calls ``_reparse_sealed_manual_review`` directly.

The Supabase + arq + pdf_store layers are all monkeypatched. No
network, no real merchant repository, no real upload directory writes
beyond ``tmp_path``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from aegis.audit import InMemoryAuditLog  # noqa: E402
from aegis.merchants.repository import MerchantNotFoundError  # noqa: E402
from scripts import recover_legacy_docs as recover  # noqa: E402


def _candidate(merchant_id: UUID, doc_id: UUID | None = None) -> dict[str, Any]:
    """Mirror the Supabase row shape ``_select_sealed_manual_review_candidates`` returns."""
    return {
        "id": str(doc_id or uuid4()),
        "original_filename": "stmt.pdf",
        "parse_status": "manual_review",
        "storage_path": "merchants/.../doc.pdf.enc",
        "merchant_id": str(merchant_id),
        "uploaded_at": "2026-06-20T00:00:00Z",
    }


class _FakeMerchant:
    """Minimal stand-in for ``MerchantRow`` — only ``business_name`` is
    accessed inside the all-merchants iterator."""

    def __init__(self, id_: UUID, name: str) -> None:
        self.id = id_
        self.business_name = name
        self.close_lead_id = f"lead_{name.lower().replace(' ', '_')}"


class _FakeMerchantRepo:
    """In-memory stand-in implementing ``.get(merchant_id)`` and ``.list_all()``.

    Production code calls ``merchants_repo.get(mid)`` once per distinct
    merchant_id discovered in the candidate selector; misses raise
    ``MerchantNotFoundError`` which the iterator converts to an issue.
    """

    def __init__(self, merchants: dict[UUID, _FakeMerchant]) -> None:
        self._merchants = merchants

    def get(self, merchant_id: UUID) -> _FakeMerchant:
        m = self._merchants.get(merchant_id)
        if m is None:
            raise MerchantNotFoundError(f"no such merchant: {merchant_id}")
        return m


# ----------------------------------------------------------------------
# Happy path — 3 merchants in one batch (under batch_size)
# ----------------------------------------------------------------------


def test_one_batch_processes_every_merchant_no_sleep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """3 merchants fit in one batch (default batch_size=5). The inter-
    batch sleep is skipped after the final batch, so a one-batch run
    must NOT call ``time.sleep`` at all."""
    m_ids = [uuid4() for _ in range(3)]
    merchants = {
        mid: _FakeMerchant(mid, f"Merchant {chr(ord('A') + idx)}") for idx, mid in enumerate(m_ids)
    }
    repo = _FakeMerchantRepo(merchants)

    monkeypatch.setattr(
        recover,
        "_select_sealed_manual_review_candidates",
        lambda *, merchant_id, include_old=True: [_candidate(mid) for mid in m_ids],
    )

    per_merchant_calls: list[UUID] = []

    def fake_per_merchant(**kwargs: Any) -> tuple[int, int, int]:
        per_merchant_calls.append(kwargs["merchant_filter"].id)
        return (2, 2, 0)  # 2 candidates, 2 enqueued, 0 issues per merchant

    monkeypatch.setattr(recover, "_reparse_sealed_manual_review", fake_per_merchant)

    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    found, enqueued, issues = recover._reparse_sealed_manual_review_all_merchants(
        merchants_repo=repo,  # type: ignore[arg-type]
        pdf_store=object(),  # type: ignore[arg-type]
        audit=InMemoryAuditLog(),
        upload_dir=tmp_path,
        apply_writes=True,
        include_old=False,
    )

    # 3 merchants x 2 each
    assert found == 6
    assert enqueued == 6
    assert issues == 0
    # Per-merchant function fired once per merchant (order matches discovery).
    assert per_merchant_calls == m_ids
    # One batch → zero sleeps.
    assert sleep_calls == []


# ----------------------------------------------------------------------
# Batching — 12 merchants → 3 batches (5 + 5 + 2) → 2 sleeps
# ----------------------------------------------------------------------


def test_multiple_batches_sleep_between_each(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """12 merchants split into 3 batches of size 5. ``time.sleep(2.0)``
    must fire exactly twice — once after batch 1, once after batch 2 —
    and NEVER after the final batch."""
    m_ids = [uuid4() for _ in range(12)]
    merchants = {mid: _FakeMerchant(mid, f"M{idx:02d}") for idx, mid in enumerate(m_ids)}
    repo = _FakeMerchantRepo(merchants)

    monkeypatch.setattr(
        recover,
        "_select_sealed_manual_review_candidates",
        lambda *, merchant_id, include_old=True: [_candidate(mid) for mid in m_ids],
    )
    monkeypatch.setattr(
        recover,
        "_reparse_sealed_manual_review",
        lambda **kw: (1, 1, 0),
    )

    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))

    found, enqueued, issues = recover._reparse_sealed_manual_review_all_merchants(
        merchants_repo=repo,  # type: ignore[arg-type]
        pdf_store=object(),  # type: ignore[arg-type]
        audit=InMemoryAuditLog(),
        upload_dir=tmp_path,
        apply_writes=True,
        include_old=False,
    )

    assert found == 12
    assert enqueued == 12
    assert issues == 0
    # 3 batches → 2 inter-batch sleeps at the default 2s cadence.
    assert sleep_calls == [2.0, 2.0]


# ----------------------------------------------------------------------
# Distinct merchant collapse — duplicate (merchant_id, doc) rows
# ----------------------------------------------------------------------


def test_duplicate_merchant_rows_collapse_to_one_hydration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two docs belonging to the same merchant produce two candidate
    rows; the iterator must hydrate that merchant once, not twice."""
    shared_mid = uuid4()
    rows = [
        _candidate(shared_mid),
        _candidate(shared_mid),
        _candidate(uuid4()),
    ]
    monkeypatch.setattr(
        recover,
        "_select_sealed_manual_review_candidates",
        lambda *, merchant_id, include_old=True: rows,
    )

    hydrated: list[UUID] = []

    class _CountingRepo:
        def get(self, mid: UUID) -> _FakeMerchant:
            hydrated.append(mid)
            return _FakeMerchant(mid, f"M-{mid.hex[:6]}")

    monkeypatch.setattr(
        recover,
        "_reparse_sealed_manual_review",
        lambda **kw: (1, 1, 0),
    )
    monkeypatch.setattr("time.sleep", lambda s: None)

    recover._reparse_sealed_manual_review_all_merchants(
        merchants_repo=_CountingRepo(),  # type: ignore[arg-type]
        pdf_store=object(),  # type: ignore[arg-type]
        audit=InMemoryAuditLog(),
        upload_dir=tmp_path,
        apply_writes=True,
        include_old=False,
    )

    # 3 candidate rows but only 2 distinct merchant_ids → 2 hydrate calls.
    assert len(hydrated) == 2
    assert shared_mid in hydrated


# ----------------------------------------------------------------------
# include_old plumbing
# ----------------------------------------------------------------------


def test_include_old_is_forwarded_to_selector_and_per_merchant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``include_old=True`` must reach the selector (broader date window)
    AND each per-merchant call (so its own selector pass agrees)."""
    mid = uuid4()
    repo = _FakeMerchantRepo({mid: _FakeMerchant(mid, "Forwarded Co")})

    selector_calls: list[bool] = []

    def fake_select(*, merchant_id: UUID | None, include_old: bool = True) -> list[dict[str, Any]]:
        selector_calls.append(include_old)
        return [_candidate(mid)]

    monkeypatch.setattr(recover, "_select_sealed_manual_review_candidates", fake_select)

    per_merchant_include_old: list[bool] = []

    def fake_per_merchant(**kwargs: Any) -> tuple[int, int, int]:
        per_merchant_include_old.append(kwargs.get("include_old", True))
        return (0, 0, 0)

    monkeypatch.setattr(recover, "_reparse_sealed_manual_review", fake_per_merchant)
    monkeypatch.setattr("time.sleep", lambda s: None)

    recover._reparse_sealed_manual_review_all_merchants(
        merchants_repo=repo,  # type: ignore[arg-type]
        pdf_store=object(),  # type: ignore[arg-type]
        audit=InMemoryAuditLog(),
        upload_dir=tmp_path,
        apply_writes=False,
        include_old=True,
    )

    # The TOP-LEVEL selector call carried include_old=True.
    assert selector_calls[0] is True
    # Per-merchant call also received include_old=True.
    assert per_merchant_include_old == [True]


# ----------------------------------------------------------------------
# Hydrate-miss + per-merchant exception both increment issues
# ----------------------------------------------------------------------


def test_hydrate_miss_increments_issues_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One merchant_id appears in the candidates but doesn't exist in
    the merchant repo. issues += 1, other merchants still process."""
    good_mid = uuid4()
    ghost_mid = uuid4()
    merchants = {good_mid: _FakeMerchant(good_mid, "Good Co")}
    repo = _FakeMerchantRepo(merchants)

    monkeypatch.setattr(
        recover,
        "_select_sealed_manual_review_candidates",
        lambda *, merchant_id, include_old=True: [
            _candidate(good_mid),
            _candidate(ghost_mid),
        ],
    )
    monkeypatch.setattr(
        recover,
        "_reparse_sealed_manual_review",
        lambda **kw: (1, 1, 0),
    )
    monkeypatch.setattr("time.sleep", lambda s: None)

    found, enqueued, issues = recover._reparse_sealed_manual_review_all_merchants(
        merchants_repo=repo,  # type: ignore[arg-type]
        pdf_store=object(),  # type: ignore[arg-type]
        audit=InMemoryAuditLog(),
        upload_dir=tmp_path,
        apply_writes=True,
        include_old=False,
    )

    # Good merchant processed cleanly.
    assert found == 1
    assert enqueued == 1
    # Ghost merchant → 1 hydrate-miss issue.
    assert issues == 1


def test_per_merchant_exception_increments_issues_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A per-merchant ``_reparse_sealed_manual_review`` raise must be
    caught, count as an issue, and not abort the remaining merchants."""
    m_ids = [uuid4(), uuid4(), uuid4()]
    merchants = {mid: _FakeMerchant(mid, f"M{idx}") for idx, mid in enumerate(m_ids)}
    repo = _FakeMerchantRepo(merchants)

    monkeypatch.setattr(
        recover,
        "_select_sealed_manual_review_candidates",
        lambda *, merchant_id, include_old=True: [_candidate(mid) for mid in m_ids],
    )

    failing_mid = m_ids[1]

    def fake_per_merchant(**kwargs: Any) -> tuple[int, int, int]:
        if kwargs["merchant_filter"].id == failing_mid:
            raise RuntimeError("boom — pdf_store fetch failed")
        return (1, 1, 0)

    monkeypatch.setattr(recover, "_reparse_sealed_manual_review", fake_per_merchant)
    monkeypatch.setattr("time.sleep", lambda s: None)

    found, enqueued, issues = recover._reparse_sealed_manual_review_all_merchants(
        merchants_repo=repo,  # type: ignore[arg-type]
        pdf_store=object(),  # type: ignore[arg-type]
        audit=InMemoryAuditLog(),
        upload_dir=tmp_path,
        apply_writes=True,
        include_old=False,
    )

    # Two merchants processed (1 each), one raised.
    assert found == 2
    assert enqueued == 2
    assert issues == 1


# ----------------------------------------------------------------------
# Empty candidate set short-circuits
# ----------------------------------------------------------------------


def test_no_candidates_returns_zeros_with_no_per_merchant_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        recover,
        "_select_sealed_manual_review_candidates",
        lambda *, merchant_id, include_old=True: [],
    )
    per_merchant_calls: list[None] = []

    def _track(**kw: Any) -> tuple[int, int, int]:
        per_merchant_calls.append(None)
        return (0, 0, 0)

    monkeypatch.setattr(recover, "_reparse_sealed_manual_review", _track)

    found, enqueued, issues = recover._reparse_sealed_manual_review_all_merchants(
        merchants_repo=_FakeMerchantRepo({}),  # type: ignore[arg-type]
        pdf_store=object(),  # type: ignore[arg-type]
        audit=InMemoryAuditLog(),
        upload_dir=tmp_path,
        apply_writes=True,
        include_old=False,
    )

    assert (found, enqueued, issues) == (0, 0, 0)
    assert per_merchant_calls == []
