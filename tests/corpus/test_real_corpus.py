"""Real-statement corpus runner.

Walks every ``*.expected.json`` under ``tests/corpus/real/`` and
runs the full parser pipeline against the sibling sanitized PDF.
Asserts the parsed output matches the expected ranges + exact counts.

Lifecycle
=========

The corpus directory is .gitignored — CI never sees fixtures, the
test silently parametrises to zero cases there. The operator builds
the corpus locally via ``scripts/build_real_corpus.py --apply`` from
sealed prod ``pdf_store`` blobs (PII-sanitized + canary-gated). The
test then becomes a pre-push gate the operator runs against parser
changes: ``REAL_CORPUS=1 pytest tests/corpus/test_real_corpus.py``.

Why opt-in?
-----------

* Calls Bedrock — costs money + needs AWS creds in env.
* Reads from network (Supabase / Bedrock).

Without ``REAL_CORPUS=1`` set, every test in this module is skipped
so unattended ``pytest`` runs (CI + local make-check) stay free.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

REAL_CORPUS_ROOT = Path(__file__).parent.parent.parent / "tests" / "corpus" / "real"
ENABLED = os.environ.get("REAL_CORPUS", "").strip() in {"1", "true", "yes"}


@dataclass(frozen=True)
class RealCorpusCase:
    expected_json_path: Path
    pdf_path: Path
    expected: dict[str, Any]

    @property
    def stem(self) -> str:
        return self.pdf_path.stem


def _discover() -> list[RealCorpusCase]:
    cases: list[RealCorpusCase] = []
    if not REAL_CORPUS_ROOT.exists():
        return cases
    for expected_json in sorted(REAL_CORPUS_ROOT.glob("*.expected.json")):
        pdf = expected_json.with_name(expected_json.stem.replace(".expected", "") + ".pdf")
        if not pdf.exists():
            continue
        try:
            expected = json.loads(expected_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        cases.append(
            RealCorpusCase(
                expected_json_path=expected_json,
                pdf_path=pdf,
                expected=expected,
            )
        )
    return cases


_CASES = _discover()
_IDS = [c.stem for c in _CASES]


def _iter_param() -> Iterator[Any]:
    yield from _CASES


def _period_days_for_result(result: Any) -> int:
    """Statement period span used by the monthly-revenue normaliser.

    Falls back to 30 (1 month) when the extraction or summary dates
    aren't available — keeps the assertion meaningful rather than
    crashing on a sparse result.
    """
    if result.extraction is None:
        return 30
    summary = result.extraction.statement.summary
    start = summary.period_start
    end = summary.period_end
    if start is None or end is None:
        return 30
    span: int = (end - start).days + 1
    return max(1, span)


@pytest.mark.skipif(
    not ENABLED,
    reason="real-corpus runner opt-in only — set REAL_CORPUS=1 to enable.",
)
@pytest.mark.skipif(
    not _CASES,
    reason="no real-corpus fixtures present (run scripts/build_real_corpus.py --apply).",
)
@pytest.mark.parametrize("case", list(_iter_param()), ids=_IDS)
def test_real_statement_parses_within_expected_ranges(case: RealCorpusCase, tmp_path: Any) -> None:
    """Full pipeline against the sanitized real PDF; assertions are:

    * parse_status matches ``expected_parse_status`` exactly,
    * monthly revenue (true_revenue normalized to 30 days) lies in
      [``expected_revenue_min``, ``expected_revenue_max``],
    * num_nsf == ``expected_nsf_count``,
    * confirmed MCA positions == ``expected_mca_positions_confirmed``,
    * bank_name matches ``expected.bank_name``.

    Pipeline crashes (uncaught exceptions) fail the test directly —
    that's the catch-all for parser regressions on real layouts.
    """
    # Local imports so the module remains importable even when the
    # parser package can't initialise (e.g. missing Bedrock env).
    # Tests are skipped under those conditions via REAL_CORPUS=1 + the
    # fixture-presence guard above; the import error only surfaces
    # when the operator actually invokes the gate.
    from aegis.api.deps import get_llm
    from aegis.parser.pipeline import run_pipeline

    # ``run_pipeline`` takes a path on disk — copy into the tmp dir so
    # the (sanitized) corpus file isn't touched by the parser.
    pdf_target = tmp_path / case.pdf_path.name
    pdf_target.write_bytes(case.pdf_path.read_bytes())

    result = run_pipeline(str(pdf_target), llm=get_llm())

    assert result.parse_status == case.expected["expected_parse_status"], (
        f"parse_status drift on {case.stem}: got {result.parse_status!r}, "
        f"expected {case.expected['expected_parse_status']!r}"
    )

    assert result.aggregates is not None, (
        f"{case.stem}: aggregates missing — pipeline produced no parsed numbers"
    )
    true_rev = Decimal(str(result.aggregates.true_revenue.value))
    # Statement-period normalisation matches Track B's
    # ``compute_monthly_revenue``: true_revenue * 30 / period_days.
    period_days = _period_days_for_result(result)
    monthly = (true_rev * Decimal(30) / Decimal(period_days)).quantize(Decimal("1"))
    low = Decimal(str(case.expected["expected_revenue_min"]))
    high = Decimal(str(case.expected["expected_revenue_max"]))
    assert low <= monthly <= high, (
        f"monthly revenue drift on {case.stem}: got {monthly} "
        f"(true_revenue {true_rev} over {period_days}d), "
        f"expected in [{low}, {high}]"
    )

    assert result.aggregates.num_nsf.value == case.expected["expected_nsf_count"], (
        f"nsf_count drift on {case.stem}: got {result.aggregates.num_nsf.value}, "
        f"expected {case.expected['expected_nsf_count']}"
    )

    pa = result.patterns
    confirmed = (
        sum(
            1
            for p in pa.mca_positions
            if getattr(p, "match_source", "known_funder") == "known_funder"
        )
        if pa is not None
        else 0
    )
    assert confirmed == case.expected["expected_mca_positions_confirmed"], (
        f"confirmed MCA positions drift on {case.stem}: got {confirmed}, "
        f"expected {case.expected['expected_mca_positions_confirmed']}"
    )

    bank_name = (
        result.extraction.statement.summary.bank_name if result.extraction is not None else None
    )
    assert bank_name == case.expected["bank_name"], (
        f"bank_name drift on {case.stem}: got {bank_name!r}, "
        f"expected {case.expected['bank_name']!r}"
    )
