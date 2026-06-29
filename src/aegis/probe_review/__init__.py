"""Probe-review package — operator validation surface for shadow probes.

Owns the ``probe_review_verdicts`` table (migration 091): per-operator
verdicts on shadow-probe disagreements. Today the only probe that emits
into this surface is ``text_layer_probe_v2``
(``parser/metadata.py::_probe_text_layer_v2_shadow``); the schema is
``probe_name``-keyed so future shadow probes can reuse the same table
without a fresh migration.

The shadow probe ships per the CLAUDE.md "Decision-boundary changes —
deliberate + shadow-first" rule: a stricter heuristic that runs
alongside the live probe, appends a
``[SHADOW] text_layer_probe_v2_disagrees: ...`` flag to
``documents.all_flags`` when the two disagree, and changes nothing
about the live routing decision. To flip the probe to live the operator
needs an adjudicated corpus showing the new probe's decisions are
correct — this package is the persistence + read surface that backs the
operator validation UI at ``/ui/admin/text-layer-probe-review``.
"""

from aegis.probe_review.models import (
    PROBE_TEXT_LAYER_V2,
    DisagreementRow,
    ProbeReviewVerdict,
)
from aegis.probe_review.repository import (
    InMemoryProbeReviewRepository,
    ProbeReviewRepository,
    ProbeReviewWriteError,
    SupabaseProbeReviewRepository,
)

__all__ = [
    "PROBE_TEXT_LAYER_V2",
    "DisagreementRow",
    "InMemoryProbeReviewRepository",
    "ProbeReviewRepository",
    "ProbeReviewVerdict",
    "ProbeReviewWriteError",
    "SupabaseProbeReviewRepository",
]
