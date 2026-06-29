"""Equipment invoice / quote parser (Bedrock vision).

Detects equipment-purchase documents (quotes, invoices, vehicle bills of
sale) and extracts the structured fields the dossier surfaces on
``product_type == "equipment"`` merchants: description, make, model,
year, condition, serial number, VIN, vendor, total cost.

Routing entrypoint
------------------
``detect_equipment_document(filename)`` returns ``True`` when the
filename should route here. Precedence vs. the A/R aging parser is
documented in ``aegis.parser.pipeline`` next to the routing helpers —
the equipment rule fires when the filename contains one of
``quote / equipment / vehicle / machinery`` OR ``invoice`` AND NOT
``aging / receivable / ar_``.

Extraction entrypoint
---------------------
``extract_equipment_details(file_path, llm_client=...)`` runs the
Bedrock-vision forced tool-use call and returns a validated
``EquipmentInvoiceResult`` (or ``None`` when extraction is
non-recoverable).
"""

from __future__ import annotations

from aegis.parser.equipment.extract import (
    encode_pdf_for_bedrock,
    extract_equipment_details,
)
from aegis.parser.equipment.models import (
    EquipmentCondition,
    EquipmentInvoiceResult,
)


def detect_equipment_document(filename: str) -> bool:
    """Return ``True`` if the filename should route to the equipment parser.

    Precedence rules (must stay in lockstep with the comment block in
    ``aegis.parser.pipeline`` that ties this to Agent 2's A/R aging
    detector):

      * Filename contains any of ``quote``, ``equipment``, ``vehicle``,
        ``machinery`` → equipment.
      * Filename contains ``invoice`` AND NOT ``aging`` / ``receivable``
        / ``ar_`` / ``ar-`` → equipment (the A/R aging detector wins on
        anything that mentions aging or receivables explicitly).
      * Anything else → not equipment.

    Pure string match on the lowercased filename. The caller is
    responsible for passing only the basename (not the full path) and
    for handling the "neither equipment nor A/R aging" fall-through
    case — this helper returns a bool, not a routing decision.
    """
    if not filename:
        return False
    name = filename.lower()
    # Aging / A/R receivables wins outright — bail before the invoice
    # token check so an "AR Aging Invoice Detail.pdf" routes to the
    # A/R aging parser, not here.
    if "aging" in name or "receivable" in name or "ar_" in name or "ar-" in name:
        return False
    if any(token in name for token in ("quote", "equipment", "vehicle", "machinery")):
        return True
    if "invoice" in name:
        return True
    return False


__all__ = [
    "EquipmentCondition",
    "EquipmentInvoiceResult",
    "detect_equipment_document",
    "encode_pdf_for_bedrock",
    "extract_equipment_details",
]
