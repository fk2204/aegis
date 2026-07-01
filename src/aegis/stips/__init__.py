"""Stipulation tracking — deal-level conditions the funder requires.

Migration 104 shipped the ``stips`` table. This module owns the
Pydantic model + repository interface + CRUD helpers the merchants
router calls into.

A "stip" is one of:
  * document      — bank statements, tax returns, voided check
  * verification  — SOS/OFAC/UCC check, background check
  * condition     — landlord letter, COJ, personal guarantee
  * signature     — ISO agreement, merchant application

Each carries a status that moves ``outstanding -> received | waived
| expired`` under operator control. The dossier renders "N
outstanding stips" and the operator collects them as the merchant
provides them.

The module is deliberately narrow — no scoring logic, no calibration
integration yet; those live at the caller layer.
"""

from __future__ import annotations

from aegis.stips.models import STIP_TEMPLATES, StipRow, StipStatus, StipType
from aegis.stips.repository import (
    InMemoryStipRepository,
    StipNotFoundError,
    StipRepository,
    SupabaseStipRepository,
)

__all__ = [
    "STIP_TEMPLATES",
    "InMemoryStipRepository",
    "StipNotFoundError",
    "StipRepository",
    "StipRow",
    "StipStatus",
    "StipType",
    "SupabaseStipRepository",
]
