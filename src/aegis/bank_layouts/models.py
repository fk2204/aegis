"""BankLayoutRow — Pydantic-strict mirror of ``bank_layouts`` (mig 059).

Operator-curated layout-learning metadata: per-bank fingerprint + the
extraction-hints text the pipeline injects into the Bedrock extraction
system prompt. NOT merchant-keyed PII — see migration 059 header for
the fingerprint-content contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

# ``hints_source`` provenance (migration 079). See repository.py for
# threshold-by-source semantics — 'auto' threshold is 1, 'manual' /
# 'mixed' threshold is 3.
HintsSource = Literal["auto", "manual", "mixed"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
    )


class BankLayoutRow(_StrictModel):
    """One bank's accumulated layout fingerprint + operator hints.

    ``layout_fingerprint`` is a free-shape JSONB merged on every
    successful parse (new keys win). Repository code is the only writer
    that mutates this dict — never the operator UI. Keep this in mind
    when reading the model: extra fingerprint keys are expected over
    time and any consumer must tolerate unknown keys.
    """

    id: UUID = Field(default_factory=uuid4)
    bank_name: str = Field(min_length=1)
    # ``dict[str, Any]`` is deliberate: the fingerprint is a free-shape
    # JSONB document whose keys evolve as the parser learns new layout
    # properties (transaction_count, has_running_balance, page_count,
    # currency today; potentially more later). A typed Pydantic submodel
    # would force a schema migration on every new fingerprint key and
    # defeat the merge-and-grow contract documented in migration 059.
    layout_fingerprint: dict[str, Any] = Field(default_factory=dict)
    successful_parses: int = Field(default=0, ge=0)
    extraction_hints: str | None = None
    # 'auto' hints come from src/aegis/bank_layouts/auto_hints.py at the
    # tail of every successful parse; 'manual' hints come from the
    # operator UI or scripts/seed_bank_hints.py; 'mixed' is the upgrade
    # the repository writes when both writers have contributed. Default
    # 'auto' matches the migration column default — empty rows are safe
    # because the gate also checks for non-empty hint text.
    hints_source: HintsSource = "auto"
    last_seen: datetime | None = None
    created_at: datetime | None = None


__all__ = [
    "BankLayoutRow",
    "HintsSource",
]
