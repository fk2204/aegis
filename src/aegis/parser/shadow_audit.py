"""Translate shadow-mode parser warnings into structured audit payloads.

The deterministic validation gate (``aegis.parser.validate``) emits its
shadow-mode flags as colon-separated strings on
``ValidationResult.warnings``. The shadow-first discipline in CLAUDE.md
requires audit-log telemetry for every shadow check before the live-flip
decision; this module owns the warning-string → audit-payload
translation so ``validate.py`` can stay DB-free and the worker can write
one ``audit_log`` row per match.

Scoped to the TD withdrawal-total coercion shadow today (the rule whose
live-flip decision is pending, per CORPUS_FINDINGS 2026-06-17). Other
shadow flags (``daily_balance_continuity_break``,
``transaction_id_sequence_gap``) pass through silently — wire them
through the same pattern when their flip decisions come up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# Format emitted by `_shadow_check_td_withdrawal_coercion` in
# `aegis.parser.validate` (round-trip tested below). Values are unsigned
# dollar amounts except `residual`, which is signed.
_TD_WITHDRAWAL_PATTERN = re.compile(
    r"^shadow_td_withdrawal_(?P<outcome>coercion_would_clear|drift_unattributed):"
    r"listed_(?P<listed>-?\d+(?:\.\d+)?)"
    r"_printed_(?P<printed>-?\d+(?:\.\d+)?)"
    r"_service_charges_(?P<service_charges>-?\d+(?:\.\d+)?)"
    r"_residual_(?P<residual>-?\d+(?:\.\d+)?)$"
)

_TD_WITHDRAWAL_ACTION = "parser.shadow.td_withdrawal_coercion"


@dataclass(frozen=True)
class ShadowAuditPayload:
    """One audit row to be written by the worker."""

    action: str
    details: dict[str, Any]


def shadow_audit_payloads(warnings: list[str]) -> list[ShadowAuditPayload]:
    """Translate ``ValidationResult.warnings`` into audit-row payloads.

    Today only ``shadow_td_withdrawal_*`` warnings are translated;
    unmatched strings are dropped silently. Single fixed action name
    (``parser.shadow.td_withdrawal_coercion``) so a single
    ``WHERE action=...`` query surfaces every shadow case during the
    live-flip review window; outcome routing lives in ``details.outcome``.
    """
    out: list[ShadowAuditPayload] = []
    for warning in warnings:
        match = _TD_WITHDRAWAL_PATTERN.match(warning)
        if match is None:
            continue
        out.append(
            ShadowAuditPayload(
                action=_TD_WITHDRAWAL_ACTION,
                details={
                    "outcome": match.group("outcome"),
                    "listed": str(Decimal(match.group("listed"))),
                    "printed": str(Decimal(match.group("printed"))),
                    "service_charges": str(Decimal(match.group("service_charges"))),
                    "residual": str(Decimal(match.group("residual"))),
                    "raw": warning,
                },
            )
        )
    return out


__all__ = ["ShadowAuditPayload", "shadow_audit_payloads"]
