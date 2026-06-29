"""Pydantic models for the probe_review surface.

``ProbeReviewVerdict`` is the row shape persisted into the
``probe_review_verdicts`` table (migration 091). ``DisagreementRow`` is
the read shape returned by
``ProbeReviewRepository.list_unreviewed_disagreements`` — one row per
document carrying a ``[SHADOW] text_layer_probe_v2_disagrees`` flag that
the requesting operator has not yet adjudicated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Single source of truth for the probe identifier emitted into
# ``documents.all_flags`` and stored on ``probe_review_verdicts.probe_name``.
# A future probe (page_layer_probe_v3, ...) lands a new constant here
# and the same repository surfaces the new corpus without a migration.
PROBE_TEXT_LAYER_V2: Final[str] = "text_layer_probe_v2"

# The shadow flag the parser pipeline writes when the v2 probe disagrees
# with the live probe. Format (per parser/pipeline.py::_run_pipeline):
#   [SHADOW] text_layer_probe_v2_disagrees: v2_route_vision=<bool>
#   live_route_vision=<bool> chars_avg=<float-fmt> numeric_lines=<int>
SHADOW_FLAG_CODE: Final[str] = "text_layer_probe_v2_disagrees"


Verdict = Literal["v2_correct", "v1_correct"]


class _StrictModel(BaseModel):
    """Base model that rejects unknown fields — same posture as
    ``aegis.storage._StrictModel`` so a future schema drift surfaces as
    a Pydantic ValidationError instead of a silent field drop."""

    model_config = ConfigDict(extra="forbid")


class ProbeReviewVerdict(_StrictModel):
    """One row in ``probe_review_verdicts``.

    Verdicts are append-only per (document, probe, operator) — a UNIQUE
    constraint at the schema layer prevents duplicates; the repository
    treats a second write from the same operator as a no-op on the
    existing row.
    """

    id: UUID
    document_id: UUID
    probe_name: str
    operator_verdict: Verdict
    operator_email: str
    created_at: datetime


class DisagreementRow(_StrictModel):
    """Read shape for the operator-validation listing.

    Carries the structural fields the operator needs to make the call:
    the bank name, the page count, the v1 and v2 routing decisions
    (parsed from the shadow flag's KV tail), and the original filename
    so a click-through to the PDF surface lands on the right document.

    No transaction descriptions, no holder names, no PII fields — the
    listing is purely structural per the CLAUDE.md PII discipline.
    """

    document_id: UUID
    bank_name: str
    page_count: int
    v1_decision: str
    v2_decision: str
    original_filename: str
    parsed_at: datetime | None = Field(default=None)
    flag_detail: str = Field(
        default="",
        description=(
            "Raw KV tail of the [SHADOW] flag — preserved so the "
            "operator can see chars_avg / numeric_lines values without "
            "re-running the probe."
        ),
    )


__all__ = [
    "PROBE_TEXT_LAYER_V2",
    "SHADOW_FLAG_CODE",
    "DisagreementRow",
    "ProbeReviewVerdict",
    "Verdict",
]
