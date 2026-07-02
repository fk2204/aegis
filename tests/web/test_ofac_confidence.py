"""Tests for ``aegis.web.routers.merchants._parse_ofac_confidence``.

The dossier surfaces a tiered confidence label next to each OFAC match
so the underwriter can spot obvious false positives without leaving
the page. Thresholds:

  * jw >= 0.96 → HIGH — investigate immediately
  * jw >= 0.92 → MEDIUM — review carefully
  * jw <  0.92 → LOW ({jw:.2f}) — likely false positive, ...
  * no parseable jw → "unknown"

Match strings come from ``aegis.compliance.ofac`` and look like
``"sdn:9999 :: BLOCKED ENTITY HOLDINGS LLC (jw=0.97 ts=0.94)"``.
"""

from __future__ import annotations

from aegis.web.routers.merchants import _parse_ofac_confidence


def test_ofac_confidence_high_at_threshold() -> None:
    result = _parse_ofac_confidence("sdn:1 :: ACME LLC (jw=0.96 ts=0.90)")
    assert result == "HIGH — investigate immediately"


def test_ofac_confidence_high_above_threshold() -> None:
    result = _parse_ofac_confidence("sdn:1 :: ACME LLC (jw=0.99 ts=0.95)")
    assert result == "HIGH — investigate immediately"


def test_ofac_confidence_medium_at_threshold() -> None:
    result = _parse_ofac_confidence("sdn:1 :: ACME LLC (jw=0.92 ts=0.80)")
    assert result == "MEDIUM — review carefully"


def test_ofac_confidence_medium_below_high_threshold() -> None:
    result = _parse_ofac_confidence("sdn:1 :: ACME LLC (jw=0.94 ts=0.80)")
    assert result == "MEDIUM — review carefully"


def test_ofac_confidence_low_below_medium_threshold() -> None:
    result = _parse_ofac_confidence("sdn:1 :: ACME LLC (jw=0.85 ts=0.70)")
    assert result == "LOW (0.85) — likely false positive, review before declining"


def test_ofac_confidence_low_reports_two_decimal_score() -> None:
    """The LOW label carries the actual jw value formatted to two
    decimals so the operator sees exactly how weak the match is."""
    result = _parse_ofac_confidence("sdn:1 :: ACME LLC (jw=0.712 ts=0.50)")
    assert result == "LOW (0.71) — likely false positive, review before declining"


def test_ofac_confidence_unknown_when_jw_missing() -> None:
    """A match line lacking the ``(jw=...)`` annotation collapses to
    ``"unknown"`` — never a fabricated number."""
    result = _parse_ofac_confidence("sdn:1 :: ACME LLC")
    assert result == "unknown"


def test_ofac_confidence_unknown_when_jw_unparseable() -> None:
    result = _parse_ofac_confidence("sdn:1 :: ACME LLC (jw=abc ts=0.50)")
    assert result == "unknown"


def test_ofac_confidence_unknown_when_string_empty() -> None:
    assert _parse_ofac_confidence("") == "unknown"
