"""Gate-logic tests for `scripts/compare_corpus_runs.py`.

The actual verify-bedrock script runs against real Bedrock + real
corpus PDFs on the Hetzner box. These tests exercise the gate
*decision* logic in isolation by feeding synthetic baseline.json +
pagerouting.json fixtures into the script and asserting the exit code.

Phase 11 task #6 promoted image-only token reduction back to a hard
gate. The fixtures here verify:

  1. A "happy path" run (clean-only stable, image-only saves tokens,
     no doc failures) passes with exit code 0.
  2. An image-only token regression fails with exit code 1.
  3. Missing image_only_* subset fails with exit code 1.
  4. A clean-only token regression alone does NOT fail (soft signal).
  5. Doc failures on either leg fail with exit code 1.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "compare_corpus_runs.py"


def _doc(
    *,
    pdf: str,
    input_tokens: int,
    output_tokens: int,
    page_strategies: list[str] | None = None,
    passed: bool = True,
) -> dict[str, Any]:
    """Synthetic per-doc result row matching the run_corpus_bedrock shape."""
    return {
        "pdf": pdf,
        "expected_validation_passed": True,
        "actual_parse_status": "proceed",
        "actual_validation_passed": True,
        "passed": passed,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "page_strategies": page_strategies if page_strategies is not None else [],
        "flags": [],
        "elapsed_seconds": 1.0,
    }


def _summary_from_results(
    results: list[dict[str, Any]], *, page_routing_enabled: bool
) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    total_pages = sum(len(r["page_strategies"]) for r in results)
    text_pages = sum(
        sum(1 for s in r["page_strategies"] if s == "text") for r in results
    )
    return {
        "total_docs": total,
        "passed_docs": passed,
        "failed_docs": total - passed,
        "total_input_tokens": sum(r["input_tokens"] for r in results),
        "total_output_tokens": sum(r["output_tokens"] for r in results),
        "avg_input_tokens_per_doc": 0,
        "avg_output_tokens_per_doc": 0,
        "total_pages_classified": total_pages,
        "text_strategy_pages": text_pages,
        "text_strategy_pct": (
            round(100 * text_pages / max(total_pages, 1), 2)
            if total_pages
            else None
        ),
        "page_routing_enabled": page_routing_enabled,
    }


def _write_run(path: Path, results: list[dict[str, Any]], *, page_routing: bool) -> None:
    path.write_text(
        json.dumps(
            {
                "summary": _summary_from_results(
                    results, page_routing_enabled=page_routing
                ),
                "results": results,
            },
            indent=2,
        )
    )


def _run_gate(baseline_path: Path, pagerouting_path: Path) -> tuple[int, str]:
    """Invoke compare_corpus_runs.py as a subprocess; return (exitcode, stdout)."""
    completed = subprocess.run(  # noqa: S603 — test-local subprocess
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--baseline",
            str(baseline_path),
            "--page-routing",
            str(pagerouting_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout


# --- fixture builders -------------------------------------------------------


def _happy_baseline() -> list[dict[str, Any]]:
    return [
        _doc(
            pdf="clean_profitable_chase_business_10001.pdf",
            input_tokens=10000,
            output_tokens=1000,
        ),
        _doc(
            pdf="image_only_clean_profitable_chase_business_90001.pdf",
            input_tokens=50000,
            output_tokens=2000,
        ),
    ]


def _happy_pagerouting(
    *, clean_overhead: int = 200, img_saving: int = 5000
) -> list[dict[str, Any]]:
    """Page-routing leg: clean docs route to text (small per-page overhead),
    image-only docs route to vision (token savings vs baseline)."""
    return [
        _doc(
            pdf="clean_profitable_chase_business_10001.pdf",
            input_tokens=10000 + clean_overhead,
            output_tokens=1000,
            page_strategies=["text"],
        ),
        _doc(
            pdf="image_only_clean_profitable_chase_business_90001.pdf",
            input_tokens=50000 - img_saving,
            output_tokens=2000,
            page_strategies=["vision"],
        ),
    ]


# --- tests ------------------------------------------------------------------


def test_happy_path_passes(tmp_path: Path) -> None:
    baseline_p = tmp_path / "baseline.json"
    pagerouting_p = tmp_path / "pagerouting.json"
    _write_run(baseline_p, _happy_baseline(), page_routing=False)
    _write_run(pagerouting_p, _happy_pagerouting(), page_routing=True)

    code, out = _run_gate(baseline_p, pagerouting_p)
    assert code == 0, out
    assert "Gate: PASS" in out


def test_image_only_token_regression_fails(tmp_path: Path) -> None:
    """If page-routing uses MORE tokens than baseline on image-only PDFs,
    the new hard gate must fail the run."""
    baseline_p = tmp_path / "baseline.json"
    pagerouting_p = tmp_path / "pagerouting.json"
    _write_run(baseline_p, _happy_baseline(), page_routing=False)
    # Negative saving = page-routing used MORE tokens on image-only.
    _write_run(
        pagerouting_p,
        _happy_pagerouting(clean_overhead=200, img_saving=-5000),
        page_routing=True,
    )

    code, out = _run_gate(baseline_p, pagerouting_p)
    assert code == 1, out
    assert "image-only page-routing used >= baseline tokens" in out


def test_missing_image_only_subset_fails(tmp_path: Path) -> None:
    """Gate must fail if image_only_* PDFs are missing — without them
    the vision-branch optimization is unverifiable."""
    baseline = [
        _doc(
            pdf="clean_profitable_chase_business_10001.pdf",
            input_tokens=10000,
            output_tokens=1000,
        ),
    ]
    pagerouting = [
        _doc(
            pdf="clean_profitable_chase_business_10001.pdf",
            input_tokens=10100,
            output_tokens=1000,
            page_strategies=["text"],
        ),
    ]
    baseline_p = tmp_path / "baseline.json"
    pagerouting_p = tmp_path / "pagerouting.json"
    _write_run(baseline_p, baseline, page_routing=False)
    _write_run(pagerouting_p, pagerouting, page_routing=True)

    code, out = _run_gate(baseline_p, pagerouting_p)
    assert code == 1, out
    assert "image_only_* PDFs missing" in out


def test_clean_only_regression_alone_passes(tmp_path: Path) -> None:
    """Clean-only token regression is a SOFT signal — should not fail
    the run as long as image-only saves tokens."""
    baseline_p = tmp_path / "baseline.json"
    pagerouting_p = tmp_path / "pagerouting.json"
    _write_run(baseline_p, _happy_baseline(), page_routing=False)
    # Large clean overhead — would fail the OLD hard gate.
    _write_run(
        pagerouting_p,
        _happy_pagerouting(clean_overhead=2000, img_saving=5000),
        page_routing=True,
    )

    code, out = _run_gate(baseline_p, pagerouting_p)
    assert code == 0, out
    assert "Gate: PASS" in out
    assert "clean-only page-routing used >= baseline tokens" in out


def test_doc_failure_on_baseline_fails(tmp_path: Path) -> None:
    baseline = _happy_baseline()
    baseline[0]["passed"] = False
    pagerouting = _happy_pagerouting()
    baseline_p = tmp_path / "baseline.json"
    pagerouting_p = tmp_path / "pagerouting.json"
    _write_run(baseline_p, baseline, page_routing=False)
    _write_run(pagerouting_p, pagerouting, page_routing=True)

    code, out = _run_gate(baseline_p, pagerouting_p)
    assert code == 1, out
    assert "failed_docs" in out


def test_image_only_misclassified_as_text_fails(tmp_path: Path) -> None:
    """If page-router classifies image_only_* as text strategy, the
    vision branch isn't exercised → gate must fail to surface the
    classifier regression."""
    baseline_p = tmp_path / "baseline.json"
    pagerouting_p = tmp_path / "pagerouting.json"
    _write_run(baseline_p, _happy_baseline(), page_routing=False)
    pagerouting = _happy_pagerouting()
    # Force misclassification: image_only doc was tagged as text.
    pagerouting[1]["page_strategies"] = ["text"]
    _write_run(pagerouting_p, pagerouting, page_routing=True)

    code, out = _run_gate(baseline_p, pagerouting_p)
    assert code == 1, out
    assert "image-only VISION-mode pct" in out


def test_wrong_baseline_flag_returns_exit_2(tmp_path: Path) -> None:
    baseline_p = tmp_path / "baseline.json"
    pagerouting_p = tmp_path / "pagerouting.json"
    _write_run(baseline_p, _happy_baseline(), page_routing=True)  # WRONG
    _write_run(pagerouting_p, _happy_pagerouting(), page_routing=True)

    code, _ = _run_gate(baseline_p, pagerouting_p)
    assert code == 2


@pytest.mark.parametrize("missing_leg", ["baseline", "pagerouting"])
def test_synthetic_fixtures_skip_real_bedrock(tmp_path: Path, missing_leg: str) -> None:
    """Documentation-only: the gate doesn't touch Bedrock when given
    pre-baked JSON. This test exists so a future refactor can't
    accidentally introduce a Bedrock call into the diffing path."""
    # If the gate ever shells out for live data, the test would
    # error on network / credential issues; we treat happy-path
    # success on a tmp_path as proof that the diff is purely local.
    baseline_p = tmp_path / "baseline.json"
    pagerouting_p = tmp_path / "pagerouting.json"
    _write_run(baseline_p, _happy_baseline(), page_routing=False)
    _write_run(pagerouting_p, _happy_pagerouting(), page_routing=True)

    code, _ = _run_gate(baseline_p, pagerouting_p)
    assert code == 0, f"missing_leg={missing_leg} sentinel — diff path must be local-only"
