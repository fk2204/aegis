"""Tests for ``aegis.scoring_v2.narrator.narrate_deal``.

Stub Bedrock — every test injects a ``_StubNarratorClient`` so no
network call fires. The narrator's contract is:

* Build a ``NarratorContext`` from already-loaded scoring objects.
* Hand it to ``narrate_deal`` with a Bedrock-shaped client.
* Receive a ``NarratorSummary`` whose three sections (deal_summary,
  flag_explanations, recommended_action) carry the model's structured
  output, with ``model_id`` + ``generated_at`` populated for audit.

Tests below cover the four action states the dossier surfaces
(``submit_now`` / ``call_first`` / ``request_documents`` /
``do_not_submit``), shape-completeness, and the storage round-trip
through ``InMemoryDocumentRepository.set_narrator_summary``.

The refresh-route test exercises the full FastAPI surface so the
``/narrator/refresh`` POST landed in the merchants router actually
wires.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import (
    get_audit,
    get_llm,
    get_merchant_repository,
    get_repository,
    reset_dependency_caches,
)
from aegis.audit import InMemoryAuditLog
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import InMemoryMerchantRepository
from aegis.scoring.models import ScoreResult
from aegis.scoring_v2.balance_health import BalanceHealthAggregation
from aegis.scoring_v2.mca_stack import MCAStackAggregation
from aegis.scoring_v2.narrator import (
    NarratorContext,
    NarratorError,
    NarratorSummary,
    narrate_deal,
)
from aegis.storage import (
    AnalysisRow,
    DocumentRow,
    InMemoryDocumentRepository,
)

# ---------------------------------------------------------------------------
# Bedrock stub
# ---------------------------------------------------------------------------


_STUB_MODEL_ID = "us.anthropic.claude-sonnet-4-6"


class _StubNarratorClient:
    """Captures the call and returns a caller-supplied tool_input."""

    def __init__(self, tool_input: dict[str, Any]) -> None:
        self.tool_input = tool_input
        self.last_system_prompt: str | None = None
        self.last_user_prompt: str | None = None

    def invoke_tool_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], str]:
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return self.tool_input, _STUB_MODEL_ID


class _RaisingNarratorClient:
    def invoke_tool_json(self, **_: Any) -> tuple[dict[str, Any], str]:
        raise RuntimeError("simulated bedrock failure")


# ---------------------------------------------------------------------------
# Minimal fixtures — every NarratorContext field
# ---------------------------------------------------------------------------


def _merchant() -> MerchantRow:
    return MerchantRow(
        id=uuid4(),
        business_name="Tasty Diner LLC",
        state="MA",
        time_in_business_months=48,
        industry_choice="Food Service",
    )


def _analysis(document_id: UUID) -> AnalysisRow:
    return AnalysisRow(
        id=uuid4(),
        document_id=document_id,
        statement_period_start=datetime(2026, 4, 1, tzinfo=UTC).date(),
        statement_period_end=datetime(2026, 4, 30, tzinfo=UTC).date(),
        statement_days=30,
        beginning_balance=Decimal("10000.00"),
        ending_balance=Decimal("12000.00"),
        avg_daily_balance=Decimal("11000.00"),
        true_revenue=Decimal("52000.00"),
        monthly_revenue=Decimal("52000.00"),
        lowest_balance=Decimal("3000.00"),
        num_nsf=0,
        days_negative=0,
        mca_positions=0,
        mca_daily_total=Decimal("0.00"),
        debt_to_revenue=Decimal("0.00"),
        payroll_detected=False,
        returned_ach_count=0,
    )


def _score(*, recommendation: str = "approve") -> ScoreResult:
    return ScoreResult(
        score=78,
        tier="B",
        recommendation=recommendation,
        soft_concerns=[],
        hard_decline_reasons=[],
        suggested_max_advance=Decimal("50000.00"),
    )


def _mca_stack() -> MCAStackAggregation:
    return MCAStackAggregation(
        active_mca_count=0,
        mca_monthly_load=Decimal("0.00"),
        estimated_combined_holdback_pct=None,
        largest_single_mca_monthly=Decimal("0.00"),
    )


def _balance_health() -> BalanceHealthAggregation:
    return BalanceHealthAggregation(
        avg_daily_balance=Decimal("11000.00"),
        avg_daily_balance_source_ids=(),
        adb_as_pct_of_monthly_deposits=Decimal("0.21"),
        adb_as_pct_of_monthly_deposits_source_ids=(),
        negative_days=0,
        negative_days_source_ids=(),
        negative_days_trailing_3m=0,
        negative_days_trailing_3m_source_ids=(),
        lowest_balance=Decimal("3000.00"),
        lowest_balance_date=None,
        lowest_balance_source_ids=(),
        shadow_triggers=(),
    )


def _build_ctx(
    *,
    merchant: MerchantRow | None = None,
    document_id: UUID | None = None,
    all_flags: tuple[str, ...] = (),
    voided_check_on_file: bool = True,
    drivers_license_on_file: bool = True,
) -> NarratorContext:
    merchant_row = merchant or _merchant()
    doc_id = document_id or uuid4()
    return NarratorContext(
        merchant=merchant_row,
        document_id=doc_id,
        analysis=_analysis(doc_id),
        score_result=_score(),
        track_a_verdict=None,
        track_b_band=None,
        mca_stack=_mca_stack(),
        balance_health=_balance_health(),
        all_flags=all_flags,
        top_funder_name="FunderCo Capital",
        top_funder_factor=Decimal("1.35"),
        top_funder_advance=Decimal("50000.00"),
        top_funder_term_days=180,
        voided_check_on_file=voided_check_on_file,
        drivers_license_on_file=drivers_license_on_file,
        bank_statements_months=4,
    )


# ---------------------------------------------------------------------------
# Tool-input fixtures — one per action state
# ---------------------------------------------------------------------------


def _tool_input_submit_now(monthly_revenue: str = "52,000") -> dict[str, Any]:
    return {
        "deal_summary": (
            f"Tasty Diner LLC is a food-service merchant averaging "
            f"${monthly_revenue}/month in deposits over the last 3 months "
            "with consistent weekly revenue patterns. No current MCA "
            "positions detected. Statement math reconciles cleanly and "
            "no editing tools detected in the PDF metadata."
        ),
        "flag_explanations": [],
        "recommended_action": {
            "action": "submit_now",
            "next_step": (
                "Deal looks clean. Top match is FunderCo Capital "
                "at 1.35 factor, 6 months, $50k advance. Submit the package."
            ),
            "top_funder_match": "FunderCo Capital",
            "estimated_terms": "1.35 factor, 6 months, $50k advance",
        },
    }


def _tool_input_do_not_submit() -> dict[str, Any]:
    return {
        "deal_summary": (
            "Tasty Diner LLC statement carries editor metadata "
            "(iText 7) inconsistent with the bank's normal export "
            "tooling. Statement math otherwise reconciles, but the "
            "tampering signal is conclusive."
        ),
        "flag_explanations": [
            {
                "flag_code": "editor_detected:itext",
                "severity": "decline",
                "explanation": (
                    "PDF /Creator metadata is 'iText 7', which the bank "
                    "does NOT use for statement exports. The statement "
                    "was generated outside the bank's pipeline."
                ),
            }
        ],
        "recommended_action": {
            "action": "do_not_submit",
            "next_step": (
                "Confirmed tampering. Do not submit to any funder. "
                "Statement was modified outside the bank's pipeline."
            ),
            "top_funder_match": None,
            "estimated_terms": None,
        },
    }


def _tool_input_call_first() -> dict[str, Any]:
    return {
        "deal_summary": (
            "Tasty Diner LLC averaging $52,000/month with one outsized "
            "$8,400 deposit in the 14 days before submission. Cashflow "
            "otherwise clean, but the spike is worth a phone call."
        ),
        "flag_explanations": [
            {
                "flag_code": "preloan_spike",
                "severity": "warn",
                "explanation": (
                    "Deposit spike before submission: deposits in the "
                    "14 days before the statement end date were $8,400 "
                    "(43% above the prior 60-day average of $5,870)."
                ),
            }
        ],
        "recommended_action": {
            "action": "call_first",
            "next_step": (
                "Ask the merchant whether they received outside funding in the last two weeks."
            ),
            "top_funder_match": "FunderCo Capital",
            "estimated_terms": "1.35 factor, 6 months, $50k advance",
        },
    }


def _tool_input_request_documents() -> dict[str, Any]:
    return {
        "deal_summary": (
            "Tasty Diner LLC averaging $52,000/month with clean "
            "cashflow. Missing the voided check before this can be "
            "submitted to any funder."
        ),
        "flag_explanations": [],
        "recommended_action": {
            "action": "request_documents",
            "next_step": (
                "Missing voided check. Ask the merchant to upload a "
                "voided check from the same account these statements "
                "belong to."
            ),
            "top_funder_match": "FunderCo Capital",
            "estimated_terms": None,
        },
    }


# ---------------------------------------------------------------------------
# narrate_deal — shape + action-state coverage
# ---------------------------------------------------------------------------


def test_narrator_returns_all_three_sections() -> None:
    ctx = _build_ctx()
    client = _StubNarratorClient(_tool_input_submit_now())

    summary = narrate_deal(ctx, bedrock=client)

    assert isinstance(summary, NarratorSummary)
    assert summary.deal_summary  # Section 1: ALWAYS present
    assert isinstance(summary.flag_explanations, tuple)  # Section 2: may be empty
    assert summary.recommended_action.action == "submit_now"  # Section 3: ALWAYS present
    assert summary.model_id == _STUB_MODEL_ID
    assert summary.version >= 1
    # generated_at is recent (within a few seconds of now).
    assert summary.generated_at <= datetime.now(UTC)
    assert summary.generated_at >= datetime.now(UTC) - timedelta(seconds=30)


def test_narrator_action_submit_now_when_clean() -> None:
    ctx = _build_ctx()  # no flags, clean track A, both docs on file
    summary = narrate_deal(ctx, bedrock=_StubNarratorClient(_tool_input_submit_now()))

    assert summary.recommended_action.action == "submit_now"
    assert summary.flag_explanations == ()
    assert summary.recommended_action.top_funder_match == "FunderCo Capital"
    assert "52,000" in summary.deal_summary  # actual monthly revenue, not placeholder


def test_narrator_action_do_not_submit_when_tampering() -> None:
    ctx = _build_ctx(all_flags=("editor_detected:itext",))
    summary = narrate_deal(ctx, bedrock=_StubNarratorClient(_tool_input_do_not_submit()))

    assert summary.recommended_action.action == "do_not_submit"
    assert len(summary.flag_explanations) == 1
    flag = summary.flag_explanations[0]
    assert flag.flag_code == "editor_detected:itext"
    assert flag.severity == "decline"
    # Cites THIS deal's actual signal, not a generic flag definition.
    assert "iText" in flag.explanation
    assert summary.recommended_action.top_funder_match is None


def test_narrator_action_call_first_when_preloan_spike() -> None:
    ctx = _build_ctx(all_flags=("preloan_spike",))
    summary = narrate_deal(ctx, bedrock=_StubNarratorClient(_tool_input_call_first()))

    assert summary.recommended_action.action == "call_first"
    assert summary.recommended_action.next_step.startswith("Ask the merchant")
    assert any(f.flag_code == "preloan_spike" for f in summary.flag_explanations)


def test_narrator_action_request_documents_when_voided_check_missing() -> None:
    ctx = _build_ctx(voided_check_on_file=False)
    summary = narrate_deal(ctx, bedrock=_StubNarratorClient(_tool_input_request_documents()))

    assert summary.recommended_action.action == "request_documents"
    assert "voided check" in summary.recommended_action.next_step.lower()


def test_narrator_raises_on_bedrock_failure() -> None:
    ctx = _build_ctx()
    with pytest.raises(NarratorError, match="bedrock_call_failed"):
        narrate_deal(ctx, bedrock=_RaisingNarratorClient())


def test_narrator_raises_on_invalid_response_shape() -> None:
    """Tool-input missing the required ``recommended_action`` key surfaces
    as ``NarratorError`` so the caller can leave the persisted column
    untouched."""
    ctx = _build_ctx()
    broken = {"deal_summary": "ok", "flag_explanations": []}  # no recommended_action
    with pytest.raises(NarratorError, match="narrator_response_validation_failed"):
        narrate_deal(ctx, bedrock=_StubNarratorClient(broken))


def test_narrator_accepts_long_explanations_within_800_char_budget() -> None:
    """Regression test for the 2026-06-28 prod outage.

    Bedrock produced 500-700 char ``flag_explanations[].explanation`` and
    ``recommended_action.next_step`` strings when several integrity
    signals fired together (multi-signal decline cases). The old
    ``max_length=400`` cap rejected the whole response as
    ``NarratorError``, leaving the dossier without a summary. The fix
    bumps the Pydantic caps to 800 AND advertises ``maxLength`` to
    Bedrock via the tool schema so the model knows the budget.

    This test exercises the post-fix tolerance: a 700-char explanation
    + a 700-char next_step round-trip cleanly without raising.
    """
    ctx = _build_ctx(all_flags=("editor_detected:itext",))
    long_explanation = "x" * 700
    long_next_step = "y" * 700
    tool_input = {
        "deal_summary": "Merchant statement carries decline-grade integrity signals.",
        "flag_explanations": [
            {
                "flag_code": "editor_detected:itext",
                "severity": "decline",
                "explanation": long_explanation,
            }
        ],
        "recommended_action": {
            "action": "do_not_submit",
            "next_step": long_next_step,
            "top_funder_match": None,
            "estimated_terms": None,
        },
    }
    summary = narrate_deal(ctx, bedrock=_StubNarratorClient(tool_input))
    assert summary.flag_explanations[0].explanation == long_explanation
    assert summary.recommended_action.next_step == long_next_step


def test_narrator_tool_schema_advertises_max_length_to_bedrock() -> None:
    """Bedrock must SEE the length caps in the tool schema so it stays
    within budget. The cap-only-on-Pydantic posture is what blew up
    2026-06-28: the model had no signal about how long it could go.
    """
    from aegis.scoring_v2.narrator import _NARRATOR_TOOL_SCHEMA

    props = _NARRATOR_TOOL_SCHEMA["properties"]
    assert props["deal_summary"]["maxLength"] == 2000
    flag_props = props["flag_explanations"]["items"]["properties"]
    assert flag_props["explanation"]["maxLength"] == 800
    assert flag_props["flag_code"]["maxLength"] == 80
    action_props = props["recommended_action"]["properties"]
    assert action_props["next_step"]["maxLength"] == 800
    assert action_props["top_funder_match"]["maxLength"] == 120
    assert action_props["estimated_terms"]["maxLength"] == 160


# ---------------------------------------------------------------------------
# Storage round-trip via InMemoryDocumentRepository.set_narrator_summary
# ---------------------------------------------------------------------------


def test_narrator_stored_on_analysis_row() -> None:
    repo = InMemoryDocumentRepository()
    doc_id = uuid4()
    merchant_id = uuid4()
    doc = DocumentRow(
        id=doc_id,
        merchant_id=merchant_id,
        file_hash="sha256:" + "0" * 64,
        byte_size=1024,
        original_filename="statement_apr.pdf",
        parse_status="proceed",
        uploaded_at=datetime.now(UTC),
    )
    repo._docs[doc_id] = doc
    repo._analyses[doc_id] = _analysis(doc_id)

    summary = narrate_deal(
        _build_ctx(document_id=doc_id),
        bedrock=_StubNarratorClient(_tool_input_submit_now()),
    )
    payload = summary.model_dump(mode="json")

    repo.set_narrator_summary(doc_id, payload)

    persisted = repo.get_analysis(doc_id)
    assert persisted is not None
    assert persisted.narrator_summary is not None
    assert persisted.narrator_summary["recommended_action"]["action"] == "submit_now"
    assert persisted.narrator_summary["model_id"] == _STUB_MODEL_ID


def test_narrator_storage_is_noop_when_no_analysis_row() -> None:
    """Narrator output for an un-aggregated document is meaningless;
    ``set_narrator_summary`` silently no-ops rather than raising."""
    repo = InMemoryDocumentRepository()
    repo.set_narrator_summary(uuid4(), {"deal_summary": "would never reach a row"})
    # No exception; no row created.


# ---------------------------------------------------------------------------
# Refresh route
# ---------------------------------------------------------------------------


@pytest.fixture
def audit() -> InMemoryAuditLog:
    return InMemoryAuditLog()


@pytest.fixture
def merchants_repo() -> InMemoryMerchantRepository:
    return InMemoryMerchantRepository()


@pytest.fixture
def docs_repo() -> InMemoryDocumentRepository:
    return InMemoryDocumentRepository()


@pytest.fixture
def client(
    audit: InMemoryAuditLog,
    merchants_repo: InMemoryMerchantRepository,
    docs_repo: InMemoryDocumentRepository,
) -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    app.dependency_overrides[get_audit] = lambda: audit
    app.dependency_overrides[get_merchant_repository] = lambda: merchants_repo
    app.dependency_overrides[get_repository] = lambda: docs_repo
    app.dependency_overrides[get_llm] = lambda: _StubNarratorClient(_tool_input_submit_now())
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    reset_dependency_caches()


def test_narrator_refresh_route_returns_404_for_unknown_merchant(
    client: TestClient,
) -> None:
    resp = client.post(
        f"/ui/merchants/{uuid4()}/documents/{uuid4()}/narrator/refresh",
        follow_redirects=False,
    )
    assert resp.status_code == 404
