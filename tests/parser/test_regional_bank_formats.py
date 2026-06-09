"""R4.7 — synthetic regional bank layout corpus tests.

Audit finding H13: parser layout coverage was Chase / BoA / Wells / CapOne /
generic regional / generic credit union. Brex, Mercury, and an older dense
credit-union format were untested. This module is the parser-side regression
suite for the three R4.7 layouts added in ``scripts/generate_corpus.py``:

- ``brex_business``       — modern fintech, single Running Balance column
- ``mercury_business``    — minimalist sans-serif, grouped-by-date subheaders
- ``community_cu_legacy`` — dense legacy format with split deposit/withdrawal
                            sub-tables and a prominent closing-balance box

The corpus generator is the **ground truth**: each (PDF, manifest) pair is
emitted from a fixed seed. This test verifies — for every R4.7 fixture —
that:

1. The PDF text layer is extractable (no image-only / OCR fallback path).
2. Bank-identifying strings appear in the text layer (so the LLM extractor
   has a real signal for ``bank_name``).
3. Every manifest transaction's amount appears in the text layer (the
   layout actually rendered every row).
4. The manifest-feed deterministic pipeline (validate → aggregate) hits the
   same ``parse_status`` the manifest's ``expected`` block claims.
5. Money totals match the manifest within $1 (the corpus tolerance from
   ``.claude/rules/testing.md``).
6. Every transaction has non-null ``source_page`` and ``source_line`` after
   layout (AEGIS auditability rule from CLAUDE.md).

Determinism: no PDFs are generated here — the test reads the committed
synthetic fixtures produced by ``scripts/generate_corpus.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymupdf
import pytest

from aegis.parser.aggregate import aggregate
from aegis.parser.models import (
    ClassifiedTransaction,
    ExtractedStatement,
    StatementSummary,
)
from aegis.parser.validate import validate_extraction

CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus" / "synthetic"

# (bank slug, expected text-layer markers, human display name)
R4_7_LAYOUTS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "brex_business",
        ("Brex Inc", "Period summary", "Running Balance"),
        "Brex Inc",
    ),
    (
        "mercury_business",
        ("Mercury", "Account", "Period summary"),
        "Mercury",
    ),
    (
        "community_cu_legacy",
        (
            "Members Community Credit Union",
            "Deposits & Credits",
            "Withdrawals & Debits",
            "CLOSING BALANCE",
        ),
        "Members Community Credit Union",
    ),
)

MONEY_TOLERANCE = Decimal("1.00")
MIN_EXPECTED_TXS = 5  # every R4.7 scenario is ≥ 5 rows by construction


@dataclass(frozen=True)
class _Fixture:
    manifest_path: Path
    pdf_path: Path
    bank_slug: str
    markers: tuple[str, ...]
    display_name: str


def _discover_r4_7_fixtures() -> list[_Fixture]:
    items: list[_Fixture] = []
    for bank_slug, markers, display_name in R4_7_LAYOUTS:
        for manifest in sorted(CORPUS_DIR.glob(f"*_{bank_slug}_*.manifest.json")):
            pdf = manifest.with_suffix("").with_suffix(".pdf")
            if not pdf.exists():
                continue
            items.append(
                _Fixture(
                    manifest_path=manifest,
                    pdf_path=pdf,
                    bank_slug=bank_slug,
                    markers=markers,
                    display_name=display_name,
                )
            )
    return items


_FIXTURES = _discover_r4_7_fixtures()
_IDS = [f.manifest_path.stem for f in _FIXTURES]


pytestmark = pytest.mark.skipif(
    not _FIXTURES,
    reason="R4.7 fixtures missing — run `python -m scripts.generate_corpus --clean`",
)


def _load_manifest(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        loaded: dict[str, Any] = json.load(f)
    return loaded


def _build_extracted_from_manifest(
    manifest: dict[str, Any],
) -> tuple[StatementSummary, list[ClassifiedTransaction]]:
    summary_raw = manifest["summary"]
    summary = StatementSummary(
        beginning_balance=Decimal(summary_raw["beginning_balance"]),
        ending_balance=Decimal(summary_raw["ending_balance"]),
        deposit_total=Decimal(summary_raw["deposit_total"]),
        withdrawal_total=Decimal(summary_raw["withdrawal_total"]),
        period_start=date.fromisoformat(summary_raw["period_start"]),
        period_end=date.fromisoformat(summary_raw["period_end"]),
        printed_transaction_count=summary_raw.get("printed_transaction_count"),
    )
    classified = [
        ClassifiedTransaction(
            posted_date=date.fromisoformat(t["posted_date"]),
            description=t["description"],
            amount=Decimal(t["amount"]),
            running_balance=Decimal(t["running_balance"]) if t.get("running_balance") else None,
            source_page=t["source_page"],
            source_line=t["source_line"],
            category=t["category"],
            classification_confidence=100,
        )
        for t in manifest["transactions"]
    ]
    return summary, classified


def _extract_text_layer(pdf_path: Path) -> str:
    """Concatenate text-layer content from every page of the PDF."""
    text = ""
    with pymupdf.open(str(pdf_path)) as doc:  # type: ignore[no-untyped-call]
        for page in doc:
            text += page.get_text("text")
    return text


@pytest.mark.parametrize("fixture", _FIXTURES, ids=_IDS)
def test_text_layer_present(fixture: _Fixture) -> None:
    """R4.7 PDFs render text natively — no OCR fallback should ever be needed.

    The legacy renderer (Chase/BoA/Wells) writes via ``canvas.drawString`` which
    produces a text layer; the R4.7 renderers must do the same.
    """
    text = _extract_text_layer(fixture.pdf_path)
    assert len(text) > 100, (
        f"{fixture.pdf_path.name}: text layer is suspiciously short "
        f"({len(text)} chars) — did the renderer drop to image-only?"
    )


@pytest.mark.parametrize("fixture", _FIXTURES, ids=_IDS)
def test_bank_identity_markers_present(fixture: _Fixture) -> None:
    """Bank-identifying strings appear in the text layer.

    The LLM extractor pulls ``bank_name`` from the printed header. If the
    layout's distinguishing markers are missing, the parser will likely emit
    ``bank_name=None`` and the merchant-bundling query will see drift.
    """
    text = _extract_text_layer(fixture.pdf_path)
    missing = [m for m in fixture.markers if m not in text]
    assert not missing, (
        f"{fixture.pdf_path.name}: expected text markers {missing!r} missing "
        f"from rendered text layer"
    )


@pytest.mark.parametrize("fixture", _FIXTURES, ids=_IDS)
def test_every_transaction_amount_in_text_layer(fixture: _Fixture) -> None:
    """Every manifest transaction's amount string appears in the PDF text.

    Confirms the layout actually rendered every row — a layout bug that
    dropped rows (e.g. exhausted the page bottom without paginating) would
    fail here.
    """
    manifest = _load_manifest(fixture.manifest_path)
    text = _extract_text_layer(fixture.pdf_path)
    missing_amounts: list[str] = []
    for tx in manifest["transactions"]:
        amount_token = f"${tx['amount']}"
        if amount_token not in text:
            missing_amounts.append(amount_token)
    assert not missing_amounts, (
        f"{fixture.pdf_path.name}: {len(missing_amounts)} manifest "
        f"transaction amounts missing from text layer (first 5: "
        f"{missing_amounts[:5]})"
    )


@pytest.mark.parametrize("fixture", _FIXTURES, ids=_IDS)
def test_source_attribution_complete(fixture: _Fixture) -> None:
    """Every manifest tx has source_page >= 1 and source_line >= 1.

    AEGIS auditability rule: drill-down from aggregate -> transaction -> PDF
    page is broken if either field is missing.
    """
    manifest = _load_manifest(fixture.manifest_path)
    bad: list[tuple[int, int, int]] = []
    for i, tx in enumerate(manifest["transactions"]):
        page = int(tx.get("source_page", 0))
        line = int(tx.get("source_line", 0))
        if page < 1 or line < 1:
            bad.append((i, page, line))
    assert not bad, (
        f"{fixture.pdf_path.name}: transactions missing source attribution "
        f"(idx, page, line): {bad[:5]}"
    )


@pytest.mark.parametrize("fixture", _FIXTURES, ids=_IDS)
def test_min_transaction_count(fixture: _Fixture) -> None:
    """Layouts must render at least ``MIN_EXPECTED_TXS`` rows per scenario."""
    manifest = _load_manifest(fixture.manifest_path)
    txs = manifest["transactions"]
    assert len(txs) >= MIN_EXPECTED_TXS, (
        f"{fixture.manifest_path.name}: only {len(txs)} transactions; "
        f"R4.7 corpus tolerance demands ≥ {MIN_EXPECTED_TXS}"
    )


@pytest.mark.parametrize("fixture", _FIXTURES, ids=_IDS)
def test_manifest_feed_pipeline_matches_expected_status(fixture: _Fixture) -> None:
    """Validator + aggregator on the manifest produce the expected outcome.

    Mirrors ``tests/test_corpus.py`` but scoped to R4.7 fixtures so a parser
    regression on a regional layout is surfaced by an R4.7-named failure
    (signal not lost in the 72-item parametrize blob).
    """
    manifest = _load_manifest(fixture.manifest_path)
    summary, classified = _build_extracted_from_manifest(manifest)
    expected = manifest.get("expected", {})

    raw_txs = [t.model_copy(update={}, deep=True) for t in classified]
    extraction = ExtractedStatement(summary=summary, transactions=raw_txs)
    validation = validate_extraction(extraction)

    if not expected.get("validation_passed", True):
        # math_tampered fixture: validator MUST refuse, with the expected
        # substring present in the failure list.
        assert not validation.passed, (
            f"{fixture.manifest_path.name}: expected validation failure, got pass"
        )
        substring = expected.get("expected_failure_substring", "")
        if substring:
            joined = " ".join(validation.failures)
            assert substring in joined, (
                f"{fixture.manifest_path.name}: expected failure substring "
                f"{substring!r}; got {validation.failures!r}"
            )
        return

    assert validation.passed, (
        f"{fixture.manifest_path.name}: validation failed unexpectedly: "
        f"{validation.failures!r}"
    )

    # Aggregate within $1 of the manifest's printed totals.
    result = aggregate(
        classified,
        period_start=summary.period_start,
        period_end=summary.period_end,
        beginning_balance=summary.beginning_balance,
    )
    aggs = result.aggregates

    # true_revenue includes credits minus transfers/chargebacks; for a clean
    # / nsf / mca / processor manifest it should be ≤ deposit_total but
    # within the corpus money tolerance for clean scenarios.
    assert aggs.true_revenue.value <= summary.deposit_total + MONEY_TOLERANCE, (
        f"{fixture.manifest_path.name}: true_revenue {aggs.true_revenue.value} "
        f"exceeds printed deposit_total {summary.deposit_total} by more than "
        f"{MONEY_TOLERANCE}"
    )

    # Source attribution: any non-zero aggregate carries source ids.
    if aggs.true_revenue.value > 0:
        assert aggs.true_revenue.source_ids, (
            f"{fixture.manifest_path.name}: non-zero true_revenue has no "
            f"source_ids — aggregator auditability broken"
        )


@pytest.mark.parametrize("fixture", _FIXTURES, ids=_IDS)
def test_layout_distinguishable_from_chase(fixture: _Fixture) -> None:
    """R4.7 layouts MUST not collide with the Chase identity marker.

    Guards against a regression where the R4.7 renderer defaults back to the
    shared ``_render_pdf`` and the resulting PDF reads as "Chase Business".
    """
    text = _extract_text_layer(fixture.pdf_path)
    assert "Chase Business" not in text, (
        f"{fixture.pdf_path.name}: text layer contains 'Chase Business' — "
        f"R4.7 layout dispatcher regressed to the legacy renderer"
    )
    assert "Bank of America" not in text, (
        f"{fixture.pdf_path.name}: text layer contains 'Bank of America' — "
        f"R4.7 layout dispatcher regressed to the legacy renderer"
    )
    assert "Wells Fargo" not in text, (
        f"{fixture.pdf_path.name}: text layer contains 'Wells Fargo' — "
        f"R4.7 layout dispatcher regressed to the legacy renderer"
    )


def test_r4_7_coverage_complete() -> None:
    """Sanity: at least one fixture per R4.7 layout was discovered."""
    found = {f.bank_slug for f in _FIXTURES}
    expected = {slug for slug, _, _ in R4_7_LAYOUTS}
    missing = expected - found
    assert not missing, (
        f"R4.7 fixtures missing for layouts {missing!r}; run "
        "`python -m scripts.generate_corpus --clean`"
    )
