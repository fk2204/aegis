"""Tests for ``aegis.compliance.ofac`` — local-cache SDN screener.

Fixtures use a synthetic-but-faithful unified cache shape (the same
JSON layout ``scripts/update_ofac_list.py`` produces). Real OFAC SDN
entries are public data; the test fixture names are clearly synthetic
to avoid even the appearance of capturing real PII.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.compliance.ofac import (
    CACHE_STALE_THRESHOLD,
    OFACResult,
    ensure_ofac_check,
    refresh_ofac_for_merchant,
    screen_merchant,
)
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _write_cache(
    tmp_path: Path,
    *,
    entries: list[dict[str, Any]] | None = None,
    fetched_at: datetime | None = None,
    lists_checked: list[str] | None = None,
) -> Path:
    cache_file = tmp_path / "ofac_unified.json"
    payload = {
        "fetched_at": (fetched_at or datetime.now(UTC)).isoformat(),
        "lists_checked": lists_checked
        if lists_checked is not None
        else ["sdn", "consolidated", "opensanctions_us_ofac_sdn"],
        "entries": entries or [],
        "name_index": {},
    }
    cache_file.write_text(json.dumps(payload), encoding="utf-8")
    return cache_file


def _sdn_entry(
    *,
    uid: str = "sdn:9999",
    name: str = "BLOCKED ENTITY HOLDINGS LLC",
    aliases: list[str] | None = None,
    list_name: str = "sdn",
) -> dict[str, Any]:
    return {
        "uid": uid,
        "name": name,
        "aliases": aliases or [],
        "list": list_name,
        "type": "entity",
        "program": "SDGT",
        "remarks": "",
    }


def _merchant(
    *,
    business_name: str = "Clean Coffee Roasters LLC",
    owner_name: str = "Alice Generic-Owner",
) -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        business_name=business_name,
        owner_name=owner_name,
        state="MA",
    )


# ---------------------------------------------------------------------------
# screen_merchant — pure function
# ---------------------------------------------------------------------------


def test_clear_merchant_returns_is_clear_true(tmp_path: Path) -> None:
    cache_path = _write_cache(
        tmp_path,
        entries=[_sdn_entry(name="EXAMPLE BLOCKED ENTITY HOLDINGS LLC")],
    )
    result = screen_merchant(
        business_name="Clean Coffee Roasters LLC",
        owner_name="Alice Generic-Owner",
        cache_path=cache_path,
    )
    assert result.is_clear is True
    assert result.match_detail == ()
    assert result.error is None


def test_sdn_name_match_blocks(tmp_path: Path) -> None:
    cache_path = _write_cache(
        tmp_path,
        entries=[_sdn_entry(uid="sdn:1234", name="BLOCKED ENTITY HOLDINGS LLC")],
    )
    result = screen_merchant(
        business_name="Blocked Entity Holdings LLC",
        owner_name=None,
        cache_path=cache_path,
    )
    assert result.is_clear is False
    assert len(result.match_detail) == 1
    assert "sdn:1234" in result.match_detail[0]
    assert "BLOCKED ENTITY HOLDINGS LLC" in result.match_detail[0]
    assert result.error is None


def test_missing_cache_file_blocks(tmp_path: Path) -> None:
    nowhere = tmp_path / "does_not_exist.json"
    result = screen_merchant(
        business_name="Clean Coffee",
        owner_name=None,
        cache_path=nowhere,
    )
    assert result.is_clear is False
    assert result.error == "cache_missing"


def test_stale_cache_blocks(tmp_path: Path) -> None:
    too_old = datetime.now(UTC) - CACHE_STALE_THRESHOLD - timedelta(hours=1)
    cache_path = _write_cache(tmp_path, entries=[], fetched_at=too_old)
    result = screen_merchant(
        business_name="Clean Coffee",
        owner_name=None,
        cache_path=cache_path,
    )
    assert result.is_clear is False
    assert result.error == "cache_stale"


def test_both_lists_are_recorded(tmp_path: Path) -> None:
    cache_path = _write_cache(
        tmp_path,
        entries=[
            _sdn_entry(uid="sdn:1", name="ENTITY ONE", list_name="sdn"),
            _sdn_entry(uid="cons:2", name="ENTITY TWO", list_name="consolidated"),
        ],
    )
    result = screen_merchant(
        business_name="Clean",
        owner_name=None,
        cache_path=cache_path,
    )
    assert "sdn" in result.lists_checked
    assert "consolidated" in result.lists_checked
    assert "opensanctions_us_ofac_sdn" in result.lists_checked


def test_jaro_winkler_threshold_respected_below_cutoff(tmp_path: Path) -> None:
    # "Jonathan Carmichael" vs "John Doe" should NOT trigger.
    cache_path = _write_cache(
        tmp_path,
        entries=[_sdn_entry(uid="sdn:1", name="John Doe")],
    )
    result = screen_merchant(
        business_name="Jonathan Carmichael Industries",
        owner_name=None,
        cache_path=cache_path,
    )
    assert result.is_clear is True


def test_jaro_winkler_threshold_respected_above_cutoff(tmp_path: Path) -> None:
    # "Blocked Entity Holdings LLC" vs "BLOCKED ENTITY HOLDINGS LLC"
    # (case + identical-token) is well above 0.88.
    cache_path = _write_cache(
        tmp_path,
        entries=[_sdn_entry(uid="sdn:1", name="BLOCKED ENTITY HOLDINGS LLC")],
    )
    result = screen_merchant(
        business_name="Blocked Entity Holdings LLC",
        owner_name=None,
        cache_path=cache_path,
    )
    assert result.is_clear is False


def test_owner_name_alone_can_trigger_match(tmp_path: Path) -> None:
    cache_path = _write_cache(
        tmp_path,
        entries=[_sdn_entry(uid="sdn:99", name="SDN INDIVIDUAL NAME")],
    )
    result = screen_merchant(
        business_name="Clean Coffee LLC",
        owner_name="Sdn Individual Name",
        cache_path=cache_path,
    )
    assert result.is_clear is False


def test_alias_match_triggers(tmp_path: Path) -> None:
    cache_path = _write_cache(
        tmp_path,
        entries=[
            _sdn_entry(
                uid="sdn:7",
                name="PRIMARY NAME ONE",
                aliases=["BLOCKED ENTITY ALIAS"],
            )
        ],
    )
    result = screen_merchant(
        business_name="Blocked Entity Alias",
        owner_name=None,
        cache_path=cache_path,
    )
    assert result.is_clear is False


# ---------------------------------------------------------------------------
# refresh_ofac_for_merchant — persistence + audit
# ---------------------------------------------------------------------------


def test_refresh_persists_clear_result_and_audits(tmp_path: Path) -> None:
    cache_path = _write_cache(tmp_path, entries=[])
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _merchant()
    repo.upsert(merchant)

    result = refresh_ofac_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        cache_path=cache_path,
    )
    assert result.is_clear is True

    refreshed = repo.get(merchant.id)
    assert refreshed.ofac_is_clear is True
    assert refreshed.ofac_match_detail == []
    assert refreshed.ofac_checked_at is not None
    assert refreshed.ofac_cache_date is not None

    screened_rows = [e for e in audit.entries if e["action"] == "compliance.ofac_screened"]
    assert len(screened_rows) == 1
    assert screened_rows[0]["details"]["is_clear"] is True

    block_rows = [e for e in audit.entries if e["action"] == "compliance.ofac_block"]
    assert block_rows == []


def test_refresh_writes_block_audit_when_blocked(tmp_path: Path) -> None:
    cache_path = _write_cache(
        tmp_path,
        entries=[_sdn_entry(uid="sdn:42", name="SANCTIONED FAKE LLC")],
    )
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _merchant(business_name="Sanctioned Fake LLC")
    repo.upsert(merchant)

    result = refresh_ofac_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        cache_path=cache_path,
    )
    assert result.is_clear is False

    refreshed = repo.get(merchant.id)
    assert refreshed.ofac_is_clear is False
    assert refreshed.ofac_match_detail  # non-empty

    block_rows = [e for e in audit.entries if e["action"] == "compliance.ofac_block"]
    assert len(block_rows) == 1
    assert block_rows[0]["details"]["match_count"] == 1


def test_refresh_writes_block_audit_when_cache_missing(tmp_path: Path) -> None:
    nowhere = tmp_path / "missing.json"
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _merchant()
    repo.upsert(merchant)

    refresh_ofac_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        cache_path=nowhere,
    )
    refreshed = repo.get(merchant.id)
    assert refreshed.ofac_is_clear is False
    block_rows = [e for e in audit.entries if e["action"] == "compliance.ofac_block"]
    assert len(block_rows) == 1
    assert block_rows[0]["details"]["error"] == "cache_missing"


def test_refresh_audit_does_not_leak_business_name(tmp_path: Path) -> None:
    """PII rule — the audit details carry counts + sanctioned-side
    name only (the SDN list is public). The merchant's business_name
    + owner_name MUST NOT appear in the details serialisation."""
    cache_path = _write_cache(
        tmp_path,
        entries=[_sdn_entry(uid="sdn:42", name="SANCTIONED FAKE LLC")],
    )
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _merchant(business_name="Sanctioned Fake LLC")
    repo.upsert(merchant)
    refresh_ofac_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        cache_path=cache_path,
    )
    rows = audit.entries
    serialised = json.dumps(rows, default=str)
    # The merchant's own business_name should not appear in audit detail
    # (the screener only writes the sanctioned SDN name and a UID).
    assert "Sanctioned Fake LLC" not in serialised  # would be the merchant name
    # The sanctioned-list name IS allowed (it's public OFAC data).
    assert "SANCTIONED FAKE LLC" in serialised


# ---------------------------------------------------------------------------
# ensure_ofac_check — lazy hook
# ---------------------------------------------------------------------------


def test_ensure_skips_when_already_checked(tmp_path: Path) -> None:
    cache_path = _write_cache(tmp_path, entries=[])
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    earlier = datetime(2026, 6, 1, tzinfo=UTC)
    merchant = _merchant().model_copy(
        update={
            "ofac_checked_at": earlier,
            "ofac_is_clear": True,
        }
    )
    repo.upsert(merchant)

    result_row = ensure_ofac_check(
        merchant,
        merchants_repo=repo,
        audit=audit,
        cache_path=cache_path,
    )
    assert result_row.ofac_checked_at == earlier
    assert audit.entries == []


def test_ensure_runs_when_never_checked(tmp_path: Path) -> None:
    cache_path = _write_cache(tmp_path, entries=[])
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _merchant()
    repo.upsert(merchant)

    refreshed = ensure_ofac_check(
        merchant,
        merchants_repo=repo,
        audit=audit,
        cache_path=cache_path,
    )
    assert refreshed.ofac_checked_at is not None
    assert refreshed.ofac_is_clear is True
    assert any(e["action"] == "compliance.ofac_screened" for e in audit.entries)


def test_screen_merchant_returns_ofac_result_instance(tmp_path: Path) -> None:
    """Regression guard — refresh path returns the dataclass shape the
    dossier route expects (boolean fields, tuple fields, datetime)."""
    cache_path = _write_cache(tmp_path, entries=[])
    result = screen_merchant(
        business_name="Anything",
        owner_name="Anyone",
        cache_path=cache_path,
    )
    assert isinstance(result, OFACResult)
    assert isinstance(result.is_clear, bool)
    assert isinstance(result.lists_checked, tuple)
    assert isinstance(result.match_detail, tuple)


@pytest.mark.parametrize(
    "candidate,sanctioned,should_match",
    [
        # Identical with case + punctuation diff → match
        ("Doe, John A.", "JOHN A DOE", True),
        # Very close common-prefix → match
        ("ACME RIYAD HOLDINGS", "ACME RIYAD HOLDINGS LLC", True),
        # Completely different → no match
        ("Boston Bagels Inc", "Tehran Trading Co", False),
        # One-letter typo in a short name → JW > 0.88 (jellyfish JW
        # is prefix-weighted, so the shared prefix bumps the score)
        ("Cohen", "Cohan", True),
    ],
)
def test_fuzzy_match_table(
    tmp_path: Path,
    candidate: str,
    sanctioned: str,
    should_match: bool,
) -> None:
    cache_path = _write_cache(
        tmp_path,
        entries=[_sdn_entry(uid="sdn:1", name=sanctioned)],
    )
    result = screen_merchant(
        business_name=candidate,
        owner_name=None,
        cache_path=cache_path,
    )
    assert (result.is_clear is False) == should_match


# ---------------------------------------------------------------------------
# refresh_ofac_for_merchant + Close-task auto-creation (build-plan 7.3)
# ---------------------------------------------------------------------------


class _FakeCloseClientForOFAC:
    """Minimal Close client stub for the OFAC → Close-task integration.

    Mirrors the shape consumed by ``compliance_tasks.create_compliance_gate_task``:
    ``get_lead`` returns ``assigned_to`` on the response dict;
    ``create_task`` records the call and returns a Close-shaped task
    body. No raises — these tests exercise the success path on the
    refresh integration. Failure-path coverage lives in
    ``tests/close/test_compliance_tasks.py``.
    """

    def __init__(self) -> None:
        self.create_task_calls: list[dict[str, Any]] = []

    def get_lead(self, lead_id: str) -> dict[str, Any]:
        return {"id": lead_id, "assigned_to": "user_xyz"}

    def create_task(
        self,
        lead_id: str,
        text: str,
        due_date: Any = None,  # datetime.date in production
        assigned_to: str | None = None,
    ) -> dict[str, Any]:
        self.create_task_calls.append(
            {
                "lead_id": lead_id,
                "text": text,
                "due_date": due_date,
                "assigned_to": assigned_to,
            }
        )
        return {"id": "task_from_ofac", "lead_id": lead_id, "text": text}


def test_refresh_blocked_with_close_client_creates_task(tmp_path: Path) -> None:
    """End-to-end — an OFAC block on a merchant with a close_lead_id
    triggers a Close task via the optional ``close_client`` kwarg."""
    cache_path = _write_cache(
        tmp_path,
        entries=[_sdn_entry(uid="sdn:42", name="SANCTIONED FAKE LLC")],
    )
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _merchant(business_name="Sanctioned Fake LLC")
    merchant = merchant.model_copy(update={"close_lead_id": "lead_xyz"})
    repo.upsert(merchant)
    fake_close = _FakeCloseClientForOFAC()

    refresh_ofac_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        cache_path=cache_path,
        close_client=fake_close,  # type: ignore[arg-type]
    )

    # Task POSTed exactly once with the block details.
    assert len(fake_close.create_task_calls) == 1
    call = fake_close.create_task_calls[0]
    assert call["lead_id"] == "lead_xyz"
    assert "OFAC block on Sanctioned Fake LLC" in call["text"]
    assert call["assigned_to"] == "user_xyz"

    # Audit row pinned for the gate-created task.
    created_rows = [e for e in audit.entries if e["action"] == "close.task.compliance_gate_created"]
    assert len(created_rows) == 1
    assert created_rows[0]["details"]["gate_type"] == "ofac_block"


def test_refresh_blocked_without_close_client_does_not_create_task(tmp_path: Path) -> None:
    """Backwards compatibility — callers that don't pass ``close_client``
    keep the original audit shape (no task rows added)."""
    cache_path = _write_cache(
        tmp_path,
        entries=[_sdn_entry(uid="sdn:42", name="SANCTIONED FAKE LLC")],
    )
    repo = InMemoryMerchantRepository()
    audit = InMemoryAuditLog()
    merchant = _merchant(business_name="Sanctioned Fake LLC")
    merchant = merchant.model_copy(update={"close_lead_id": "lead_xyz"})
    repo.upsert(merchant)

    refresh_ofac_for_merchant(
        merchant.id,
        merchants_repo=repo,
        audit=audit,
        cache_path=cache_path,
    )
    actions = [e["action"] for e in audit.entries]
    assert "compliance.ofac_block" in actions
    assert "close.task.compliance_gate_created" not in actions
