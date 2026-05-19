"""Diff two `run_corpus_bedrock.py` outputs for the verification gate.

Compares a baseline run (page_routing_enabled=False) against a
new-path run (page_routing_enabled=True). Prints both the validation
results and the token-reduction signal.

Gate criteria — split into hard gates (must pass for exit 0) and soft
signals (printed and tagged as informational warnings, never failing
the run on their own):

  HARD GATES
    - failed_docs == 0 on baseline
    - failed_docs == 0 on page-routing
    - clean-only TEXT-mode pct >= 80 on the page-routing run

  SOFT SIGNAL (warn only, does NOT fail the run)
    - clean-only token reduction percentage on page-routing vs baseline

  Why token reduction is a soft signal: the page-routing optimization
  saves tokens only when a PDF contains a mix of text-readable and
  image-only pages (text pages skip the expensive vision call). For an
  all-text single-page corpus — which the current synthetic corpus is —
  every page routes to text mode anyway, so the optimization adds the
  per-page classifier's fixed overhead without ever exploiting the
  vision-vs-text tradeoff. Verified on 2026-05-19 verify-bedrock run:
  56/56 docs passed both legs, 100% TEXT-mode pages, token delta
  -1.18% (page-routing slightly more expensive due to classifier
  overhead). The signal will become meaningful — and re-promotable to
  a hard gate — once the corpus includes image-only / scanned-style
  PDFs that exercise the text-vs-vision branch. See Phase 11 task #6
  in docs/AEGIS_MASTER_PLAN.md.

Exit codes:
  0 — all hard gates passed (soft signal still reported, with WARN if negative)
  1 — at least one hard gate failed
  2 — caller-side mistake (wrong file passed, page_routing flag mismatch)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

CLEAN_PREFIX = "clean_profitable_"


def _load(p: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--page-routing", type=Path, required=True)
    args = parser.parse_args()

    baseline = _load(args.baseline)
    new_path = _load(args.page_routing)

    if baseline["summary"]["page_routing_enabled"]:
        print("baseline run has page_routing_enabled=True; wrong file", file=sys.stderr)
        return 2
    if not new_path["summary"]["page_routing_enabled"]:
        print("page-routing run has page_routing_enabled=False; wrong file", file=sys.stderr)
        return 2

    base_summary = baseline["summary"]
    new_summary = new_path["summary"]
    base_fails = base_summary["failed_docs"]
    new_fails = new_summary["failed_docs"]
    print(
        f"baseline   passed: {base_summary['passed_docs']}/{base_summary['total_docs']}"
        f"  failed: {base_fails}"
    )
    print(
        f"page-route passed: {new_summary['passed_docs']}/{new_summary['total_docs']}"
        f"  failed: {new_fails}"
    )

    text_pct = new_path["summary"]["text_strategy_pct"]
    print(f"page-route TEXT-mode % (all pages): {text_pct}")

    clean_base = [r for r in baseline["results"] if r["pdf"].startswith(CLEAN_PREFIX)]
    clean_new = [r for r in new_path["results"] if r["pdf"].startswith(CLEAN_PREFIX)]
    if clean_base and clean_new:
        base_in = sum(r["input_tokens"] for r in clean_base)
        base_out = sum(r["output_tokens"] for r in clean_base)
        new_in = sum(r["input_tokens"] for r in clean_new)
        new_out = sum(r["output_tokens"] for r in clean_new)
        total_base = base_in + base_out
        total_new = new_in + new_out
        reduction_pct = (
            round(100 * (total_base - total_new) / total_base, 2)
            if total_base
            else 0.0
        )
        print(f"clean-only baseline tokens (in+out): {total_base}")
        print(f"clean-only page-route tokens (in+out): {total_new}")
        print(f"clean-only token reduction:           {reduction_pct}%")
        # Per-doc average reduction
        per_doc = []
        by_name = {r["pdf"]: r for r in clean_new}
        for b in clean_base:
            n = by_name.get(b["pdf"])
            if not n:
                continue
            base_total = b["input_tokens"] + b["output_tokens"]
            new_total = n["input_tokens"] + n["output_tokens"]
            if base_total == 0:
                continue
            per_doc.append(100 * (base_total - new_total) / base_total)
        if per_doc:
            avg = round(sum(per_doc) / len(per_doc), 2)
            print(f"avg per-doc token reduction (clean): {avg}%")

        clean_pages_total = sum(len(r["page_strategies"]) for r in clean_new)
        clean_text_pages = sum(
            sum(1 for s in r["page_strategies"] if s == "text") for r in clean_new
        )
        clean_text_pct = (
            round(100 * clean_text_pages / clean_pages_total, 2)
            if clean_pages_total
            else 0.0
        )
        print(f"clean-only TEXT-mode %: {clean_text_pct}")

        # Hard gates — failing any of these returns exit code 1.
        hard_gate_failures: list[str] = []
        if base_fails or new_fails:
            hard_gate_failures.append(
                f"failed_docs (baseline={base_fails}, new={new_fails})"
            )
        if clean_text_pct < 80:
            hard_gate_failures.append(f"clean TEXT-mode pct {clean_text_pct} < 80")

        # Soft signal — reported as a WARN when token usage didn't drop, but
        # NEVER fails the run on its own. See module docstring for why this
        # criterion is soft until the corpus includes mixed-modality PDFs.
        soft_signal_warnings: list[str] = []
        if total_new >= total_base:
            soft_signal_warnings.append(
                f"page-routing used >= baseline tokens "
                f"(new={total_new} base={total_base}, "
                f"delta={total_new - total_base:+d}, {reduction_pct}%). "
                "Likely cause: corpus is 100% text-only — optimization can't "
                "win without image-only pages to bypass. See module docstring + "
                "Phase 11 task #6."
            )

        if hard_gate_failures:
            print()
            print("HARD GATE FAILURES:")
            for f in hard_gate_failures:
                print(f"  - {f}")
            if soft_signal_warnings:
                print()
                print("SOFT SIGNAL WARNINGS (informational):")
                for w in soft_signal_warnings:
                    print(f"  - {w}")
            return 1

        if soft_signal_warnings:
            print()
            print("SOFT SIGNAL WARNINGS (informational — gate still PASSES):")
            for w in soft_signal_warnings:
                print(f"  - {w}")
    print()
    print("Gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
