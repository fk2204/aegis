"""A/R aging parser — Excel / CSV / PDF.

Used for asset-based-lending and factoring product screens where the
operator uploads an accounts-receivable aging report instead of (or
alongside) bank statements. Output is one ``ARAgingResult`` per file
with total outstanding, aging buckets (current / 30-60 / 60-90 / 90+),
top debtors by concentration, and the per-debtor breakdown the dossier
surfaces.

Detection in ``aegis.parser.pipeline`` is filename-driven (case-
insensitive ``aging|receivable|ar_|a_r`` substring match). Routing on
upload follows in a separate commit — this module is callable today
and is exercised end-to-end by ``scripts/`` ingestion paths.
"""

from aegis.parser.ar_aging.extract import (
    ARAgingResult,
    detect_ar_aging_filename,
    extract_ar_aging_csv,
    extract_ar_aging_excel,
    extract_ar_aging_pdf,
)

__all__ = [
    "ARAgingResult",
    "detect_ar_aging_filename",
    "extract_ar_aging_csv",
    "extract_ar_aging_excel",
    "extract_ar_aging_pdf",
]
