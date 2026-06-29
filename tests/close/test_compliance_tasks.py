"""Tests for ``aegis.close.compliance_tasks`` — build-plan 7.3.

Covers the five required behaviors:

(a) OFAC gate triggers task creation via mocked Close client.
(b) Bankruptcy gate triggers task creation.
(c) Licensing gate triggers task creation.
(d) Merchant without ``close_lead_id`` → skipped + audit row.
(e) Close API raises → gate decision still completes + failure audit row.

The Close client is exercised through a captured-shape fake. The
``create_task`` response shape mirrors what
``POST /api/v1/task/`` actually returns (an ``id`` field + the full
task body); the ``get_lead`` response mirrors the ``assigned_to``
top-level field that gates assignee resolution.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.close.client import CloseAuthError, CloseError
from aegis.close.compliance_tasks import (
    BankruptcyGateDetails,
    ComplianceGateType,
    LicenseGateDetails,
    OFACGateDetails,
    create_compliance_gate_task,
    has_open_gate_task,
)
from aegis.merchants.models import MerchantRow

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _merchant(
    *,
    business_name: str | None = "Acme Diner LLC",
    close_lead_id: str | None = "lead_abc",
    state: str | None = "FL",
    industry_naics: str | None = None,
) -> MerchantRow:
    """Build a merchant with the smallest valid shape for these tests."""
    return MerchantRow(
        id=uuid4(),
        status="finalized",
        business_name=business_name,
        owner_name="Sam Owner",
        state=state,
        industry_naics=industry_naics,
        close_lead_id=close_lead_id,
    )


class _FakeCloseClient:
    """Drop-in for :class:`CloseClient` — capture POST bodies, return canned responses.

    Implements the exact methods :func:`create_compliance_gate_task`
    calls. Not a Pydantic substitute for the real client; we use it to
    pin the audit + payload contract without spinning up httpx.

    The ``get_lead`` shape mirrors what Close actually returns —
    ``assigned_to`` at the top level (verified against AEGIS's existing
    ``aegis.close.sync.push_decision_to_close`` consumer).
    """

    def __init__(
        self,
        *,
        lead_assigned_to: str | None = "user_xyz",
        task_id: str = "task_001",
        raise_on_create: Exception | None = None,
        raise_on_get_lead: Exception | None = None,
    ) -> None:
        self._lead_assigned_to = lead_assigned_to
        self._task_id = task_id
        self._raise_on_create = raise_on_create
        self._raise_on_get_lead = raise_on_get_lead
        self.create_task_calls: list[dict[str, Any]] = []
        self.get_lead_calls: list[str] = []

    def get_lead(self, lead_id: str) -> dict[str, Any]:
        self.get_lead_calls.append(lead_id)
        if self._raise_on_get_lead is not None:
            raise self._raise_on_get_lead
        return {"id": lead_id, "assigned_to": self._lead_assigned_to}

    def create_task(
        self,
        lead_id: str,
        text: str,
        due_date: date | None = None,
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
        if self._raise_on_create is not None:
            raise self._raise_on_create
        return {
            "id": self._task_id,
            "_type": "lead",
            "lead_id": lead_id,
            "text": text,
            "assigned_to": assigned_to,
            "date": due_date.isoformat() if due_date else None,
        }


def _audit_actions(audit: InMemoryAuditLog) -> list[str]:
    return [e["action"] for e in audit.entries]


def _audit_details(audit: InMemoryAuditLog, action: str) -> dict[str, Any]:
    for entry in audit.entries:
        if entry["action"] == action:
            details = entry.get("details") or {}
            assert isinstance(details, dict)
            return details
    raise AssertionError(f"audit action {action!r} not found; saw {_audit_actions(audit)}")


# ---------------------------------------------------------------------------
# (a) OFAC gate triggers task creation
# ---------------------------------------------------------------------------


def test_ofac_gate_creates_close_task() -> None:
    merchant = _merchant()
    audit = InMemoryAuditLog()
    client = _FakeCloseClient(lead_assigned_to="user_xyz", task_id="task_ofac_1")
    now = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)

    task_id = create_compliance_gate_task(
        merchant=merchant,
        gate_type=ComplianceGateType.OFAC_BLOCK,
        details=OFACGateDetails(sdn_name="BLOCKED ENTITY HOLDINGS LLC"),
        client=client,  # type: ignore[arg-type]  # Fake is structurally compatible
        audit=audit,
        now=now,
    )

    assert task_id == "task_ofac_1"
    assert len(client.create_task_calls) == 1
    call = client.create_task_calls[0]
    assert call["lead_id"] == "lead_abc"
    assert "OFAC block on Acme Diner LLC" in call["text"]
    assert "BLOCKED ENTITY HOLDINGS LLC" in call["text"]
    assert call["assigned_to"] == "user_xyz"
    # Default due date is +1 day.
    assert call["due_date"] == date(2026, 6, 29)

    details = _audit_details(audit, "close.task.compliance_gate_created")
    assert details["gate_type"] == "ofac_block"
    assert details["close_lead_id"] == "lead_abc"
    assert details["task_id_from_close"] == "task_ofac_1"
    assert details["assigned_to"] == "user_xyz"


# ---------------------------------------------------------------------------
# (b) Bankruptcy gate triggers task creation
# ---------------------------------------------------------------------------


def test_bankruptcy_gate_creates_close_task() -> None:
    merchant = _merchant(business_name="Bankrupt Bros LLC")
    audit = InMemoryAuditLog()
    client = _FakeCloseClient(task_id="task_bk_1")

    task_id = create_compliance_gate_task(
        merchant=merchant,
        gate_type=ComplianceGateType.BANKRUPTCY_BLOCK,
        details=BankruptcyGateDetails(chapter="7", case_count=2, court="flsb"),
        client=client,  # type: ignore[arg-type]
        audit=audit,
    )

    assert task_id == "task_bk_1"
    text = client.create_task_calls[0]["text"]
    assert "Bankruptcy block on Bankrupt Bros LLC" in text
    assert "chapter 7" in text
    assert "flsb" in text
    assert "2 cases" in text

    details = _audit_details(audit, "close.task.compliance_gate_created")
    assert details["gate_type"] == "bankruptcy_block"


# ---------------------------------------------------------------------------
# (c) Licensing gate triggers task creation
# ---------------------------------------------------------------------------


def test_license_gate_creates_close_task() -> None:
    merchant = _merchant(business_name="HVAC Heroes Inc", state="FL")
    audit = InMemoryAuditLog()
    client = _FakeCloseClient(task_id="task_lic_1")

    task_id = create_compliance_gate_task(
        merchant=merchant,
        gate_type=ComplianceGateType.LICENSE_REQUIRED,
        details=LicenseGateDetails(
            state="FL",
            license_type="HVAC / Plumbing Contractor",
            portal_url="https://www.myfloridalicense.com/wl11.asp",
        ),
        client=client,  # type: ignore[arg-type]
        audit=audit,
    )

    assert task_id == "task_lic_1"
    text = client.create_task_calls[0]["text"]
    assert "License verification required" in text
    assert "FL HVAC / Plumbing Contractor" in text
    assert "myfloridalicense.com" in text

    details = _audit_details(audit, "close.task.compliance_gate_created")
    assert details["gate_type"] == "license_required"


# ---------------------------------------------------------------------------
# (d) Merchant without close_lead_id → skipped + audit
# ---------------------------------------------------------------------------


def test_merchant_without_close_lead_id_is_skipped() -> None:
    merchant = _merchant(close_lead_id=None)
    audit = InMemoryAuditLog()
    client = _FakeCloseClient()

    task_id = create_compliance_gate_task(
        merchant=merchant,
        gate_type=ComplianceGateType.OFAC_BLOCK,
        details=OFACGateDetails(sdn_name="ANY ENTITY"),
        client=client,  # type: ignore[arg-type]
        audit=audit,
    )

    assert task_id is None
    assert client.create_task_calls == []
    assert client.get_lead_calls == []

    actions = _audit_actions(audit)
    assert actions == ["close.task.skipped_no_lead"]
    details = _audit_details(audit, "close.task.skipped_no_lead")
    assert details["gate_type"] == "ofac_block"
    assert "merchant.close_lead_id is None" in details["reason"]


# ---------------------------------------------------------------------------
# (e) Close API raises → gate decision still completes + audit on failure
# ---------------------------------------------------------------------------


def test_close_create_task_failure_is_audited_not_raised() -> None:
    merchant = _merchant()
    audit = InMemoryAuditLog()
    client = _FakeCloseClient(
        raise_on_create=CloseError(
            "close 500: upstream",
            status_code=500,
            body="upstream timeout",
        )
    )

    task_id = create_compliance_gate_task(
        merchant=merchant,
        gate_type=ComplianceGateType.OFAC_BLOCK,
        details=OFACGateDetails(sdn_name="ENTITY X"),
        client=client,  # type: ignore[arg-type]
        audit=audit,
    )

    # No raise — caller's gate decision is independent.
    assert task_id is None
    # POST was attempted (get_lead succeeded; create_task failed).
    assert len(client.create_task_calls) == 1
    actions = _audit_actions(audit)
    assert "close.task.create_failed" in actions
    details = _audit_details(audit, "close.task.create_failed")
    assert details["gate_type"] == "ofac_block"
    assert details["close_lead_id"] == "lead_abc"
    assert details["status_code"] == 500
    assert details["error"] == "CloseError"


def test_get_lead_auth_failure_falls_back_to_no_assignee() -> None:
    """A 401 on the assignee lookup MUST NOT block task creation.

    Auth failures on the assignee lookup are best-effort — we audit and
    fall through to a task POST with ``assigned_to=None``, letting Close
    default the assignee to the API key's owning user.
    """
    merchant = _merchant()
    audit = InMemoryAuditLog()
    client = _FakeCloseClient(
        raise_on_get_lead=CloseAuthError("close 401", status_code=401),
        task_id="task_fallback",
    )

    task_id = create_compliance_gate_task(
        merchant=merchant,
        gate_type=ComplianceGateType.OFAC_BLOCK,
        details=OFACGateDetails(sdn_name="ENTITY Y"),
        client=client,  # type: ignore[arg-type]
        audit=audit,
    )

    assert task_id == "task_fallback"
    actions = _audit_actions(audit)
    assert "close.task.assignee_lookup_failed" in actions
    assert "close.task.compliance_gate_created" in actions
    # No assignee on the task POST when the lookup failed.
    assert client.create_task_calls[0]["assigned_to"] is None


# ---------------------------------------------------------------------------
# has_open_gate_task — idempotency helper
# ---------------------------------------------------------------------------


def test_has_open_gate_task_detects_prior_row() -> None:
    merchant = _merchant()
    audit = InMemoryAuditLog()
    client = _FakeCloseClient()

    # First create → audit row lands.
    create_compliance_gate_task(
        merchant=merchant,
        gate_type=ComplianceGateType.LICENSE_REQUIRED,
        details=LicenseGateDetails(
            state="FL",
            license_type="General Contractor",
            portal_url="https://example/portal",
        ),
        client=client,  # type: ignore[arg-type]
        audit=audit,
    )

    assert (
        has_open_gate_task(
            audit=audit,
            merchant_id=merchant.id,
            gate_type=ComplianceGateType.LICENSE_REQUIRED,
        )
        is True
    )
    # A different gate_type is still considered "open task absent" for
    # that gate — gates are independent.
    assert (
        has_open_gate_task(
            audit=audit,
            merchant_id=merchant.id,
            gate_type=ComplianceGateType.OFAC_BLOCK,
        )
        is False
    )


def test_has_open_gate_task_empty_for_fresh_merchant() -> None:
    audit = InMemoryAuditLog()
    assert (
        has_open_gate_task(
            audit=audit,
            merchant_id=uuid4(),
            gate_type=ComplianceGateType.OFAC_BLOCK,
        )
        is False
    )


# ---------------------------------------------------------------------------
# Detail-payload typing — wrong gate_type vs details combo raises TypeError
# ---------------------------------------------------------------------------


def test_mismatched_details_payload_raises_type_error() -> None:
    merchant = _merchant()
    audit = InMemoryAuditLog()
    client = _FakeCloseClient()

    with pytest.raises(TypeError):
        create_compliance_gate_task(
            merchant=merchant,
            gate_type=ComplianceGateType.OFAC_BLOCK,
            details=BankruptcyGateDetails(  # wrong payload for OFAC gate
                chapter="7", case_count=1, court=None
            ),
            client=client,  # type: ignore[arg-type]
            audit=audit,
        )
