"""Track A — Document Integrity.

Surfaces the existing per-document integrity signals (PDF metadata
forensics, math reconciliation, running-balance drift) as the
near-binary verdict the design doc specified:
``integrity_verdict ∈ {clean, review, fail}``.

This is NOT a new detector. The integrity signals already exist in
``aegis.parser.metadata`` (metadata_score + flags), ``aegis.parser.validate``
(reconciliation failures + future-dated checks), and
``aegis.parser.tampering`` (the strong/medium composition rule).
Track A reads those signals and projects them onto the verdict
taxonomy so the dossier and Step-2 auto-decline logic have one
consistent surface to consume.

Per the design doc (Q2 decided):

* **fail** — strong metadata tampering (metadata_score ≥ 50: hard
  editor / forged author / structural anomaly). Auto-decline-eligible
  in Step 2; ADDITIVE today.
* **review** — medium metadata (25-49) + corroborating math/structural
  signal (reconciliation failure, future-dated). Behavioural patterns
  (concentration, payroll, etc.) do NOT corroborate — that's the
  VU-shaped false-positive guard preserved from
  ``aegis.parser.tampering``.
* **clean** — nothing fired.

* **Running-balance-drift placement** (the canonical
  competent-fabrication signature):
  - drift alone → ``review`` (could be genuine OCR or parser miss).
  - drift + editor metadata → ``fail`` (two integrity signals
    corroborate; same evidence pattern A&R KM's Lili statements
    showed: iText 2.1.7 + 4-of-4 months reconciliation drift).

THIS COMMIT SHIPS PURELY ADDITIVE — same structural guard as Track B
and Track C. Track A produces a verdict; it does NOT wire into the
live decline path. The legacy ``fraud_score`` retains control of
production until Step 2 of the redesign deliberately replaces it,
and the tampering rule stays in shadow mode until live audit-row
review (per ``aegis.parser.tampering``'s ``aegis_tampering_decline_mode``).
"""

from aegis.scoring_v2.track_a.compute import compute_integrity_verdict
from aegis.scoring_v2.track_a.models import (
    DocumentIntegritySignals,
    EvidenceItem,
    IntegrityBranch,
    IntegrityVerdict,
    VerdictLevel,
)

__all__ = [
    "DocumentIntegritySignals",
    "EvidenceItem",
    "IntegrityBranch",
    "IntegrityVerdict",
    "VerdictLevel",
    "compute_integrity_verdict",
]
