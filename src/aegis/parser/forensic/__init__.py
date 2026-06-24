"""Forensic integrity detection layer for the parser.

Deterministic per-PDF tampering signals that complement the existing
``aegis.parser.metadata`` checks. No Bedrock calls, no LLM. Each module
exposes a single ``analyze(pdf_path)`` function returning a typed result
dataclass; ``analyze_metadata`` in ``aegis.parser.metadata`` calls them
in turn and folds the verdicts into ``MetadataAnalysis.fraud_score`` +
``MetadataAnalysis.flags``.

The existing page-level helpers (``_font_inconsistency``,
``_page_layer_anomaly``) live in ``metadata.py`` for historical reasons
(master plan §6.4). The detectors here target finer-grained surfaces:
font consistency WITHIN a page, creator-string fingerprinting against a
known-good bank registry, and text-overlay detection on content
streams. See ``docs/REMAINING_WORK.md`` § forensic integrity layer for
build order and rationale.
"""

from __future__ import annotations
