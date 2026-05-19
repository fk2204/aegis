"""Diff two `run_corpus_bedrock.py` outputs for the verification gate.

Compares a baseline run (page_routing_enabled=False) against a
new-path run (page_routing_enabled=True). Prints both the validation
results and the token-reduction signal.

Gate criteria (post Phase 11 task #6 — image-only synthetic PDFs):

  HARD GATES
    - failed_docs == 0 on baseline
    - failed_docs == 0 on page-routing
    - clean-only TEXT-mode pct >= 80 on the page-routing run
    - image-only token reduction > 0% on the page-routing run
      (vision-routed pages should run cheaper on the page-routing path
      than on the baseline whole-doc path; the page-routing classifier
      lets text-bearing pages skip the expensive vision call. With
      image_only_* PDFs in the corpus this branch is now exercisable.)

  REPORTED SIGNALS (printed, do NOT fail the run on their own)
    - clean-only token reduction (clean_profitable_*) — informational
      because clean docs are 100% text-bearing and the optimization
      has no opportunity to win there; near-zero or slightly negative
      delta from classifier overhead is expected.

History of the token-reduction criterion
----------------------------------------
The token-reduction criterion lived in ``hard_gate_failures`` from
introduction (Stage 2B page-routing) through 2026-05-19, when the
first end-to-end verify-bedrock run on real Bedrock surfaced that the
synthetic corpus was 100% text-readable (56/56 docs, 100% TEXT-mode
pages, -1.18% token delta from per-page classifier overhead). It was
demoted to a soft signal in commit 9487ea2 so the gate would not
falsely fail on a corpus that couldn't exercise the optimization.

Phase 11 task #6 added image-only synthetic PDFs (``image_only_*``
under ``tests/fixtures/corpus/synthetic/``) that route to vision under
the page router. Those PDFs give the optimization a real branch to
exploit, so the criterion is promoted back to a HARD gate against the
``image_only_*`` subset. The clean-only criterion stays soft for the
historical reason above.

Exit codes:
  0 — all hard gates passed (soft signals still reported)
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
IMAGE_ONLY_PREFIX = "image_only_"


def _load(p: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return loaded


def _token_totals(results: list[dict[str, Any]]) -> tuple[int, int]:
    """Sum (input, output) tokens across results."""
    return (
        sum(r["input_tokens"] for r in results),
        sum(r["output_tokens"] for r in results),
    )


def _reduction_pct(base_total: int, new_total: int) -> float:
    if base_total == 0:
        return 0.0
    return round(100 * (base_total - new_total) / base_total, 2)


def _per_doc_avg_reduction(
    base: list[dict[str, Any]], new: list[dict[str, Any]]
) -> float | None:
    """Average per-doc % reduction. Returns None when no overlap."""
    by_name = {r["pdf"]: r for r in new}
    deltas: list[float] = []
    for b in base:
        n = by_name.get(b["pdf"])
        if not n:
            continue
        base_total = b["input_tokens"] + b["output_tokens"]
        new_total = n["input_tokens"] + n["output_tokens"]
        if base_total == 0:
            continue
        deltas.append(100 * (base_total - new_total) / base_total)
    if not deltas:
        return None
    return round(sum(deltas) / len(deltas), 2)


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

    hard_gate_failures: list[str] = []
    soft_signal_warnings: list[str] = []

    if base_fails or new_fails:
        hard_gate_failures.append(
            f"failed_docs (baseline={base_fails}, new={new_fails})"
        )

    # --- Clean-only subset (soft signal — historical, kept for visibility).
    clean_base = [r for r in baseline["results"] if r["pdf"].startswith(CLEAN_PREFIX)]
    clean_new = [r for r in new_path["results"] if r["pdf"].startswith(CLEAN_PREFIX)]
    clean_text_pct: float | None = None
    if clean_base and clean_new:
        base_in, base_out = _token_totals(clean_base)
        new_in, new_out = _token_totals(clean_new)
        total_base = base_in + base_out
        total_new = new_in + new_out
        reduction_pct = _reduction_pct(total_base, total_new)
        print(f"clean-only baseline tokens (in+out): {total_base}")
        print(f"clean-only page-route tokens (in+out): {total_new}")
        print(f"clean-only token reduction:           {reduction_pct}%")
        per_doc = _per_doc_avg_reduction(clean_base, clean_new)
        if per_doc is not None:
            print(f"avg per-doc token reduction (clean): {per_doc}%")

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

        if clean_text_pct < 80:
            hard_gate_failures.append(
                f"clean TEXT-mode pct {clean_text_pct} < 80"
            )

        # Clean-only token reduction stays a SOFT signal. Clean PDFs are
        # 100% text-bearing — the per-page classifier's fixed overhead is
        # the only thing the optimization can spend tokens on there, so a
        # small negative delta is expected and shouldn't fail the gate.
        if total_new >= total_base:
            soft_signal_warnings.append(
                f"clean-only page-routing used >= baseline tokens "
                f"(new={total_new} base={total_base}, "
                f"delta={total_new - total_base:+d}, {reduction_pct}%). "
                "Expected for a 100% text-bearing subset — the per-page "
                "classifier adds overhead with no offsetting vision skip. "
                "The image_only_* subset is what we hard-gate on."
            )

    # --- Image-only subset (HARD gate — Phase 11 task #6).
    img_base = [r for r in baseline["results"] if r["pdf"].startswith(IMAGE_ONLY_PREFIX)]
    img_new = [r for r in new_path["results"] if r["pdf"].startswith(IMAGE_ONLY_PREFIX)]
    if img_base and img_new:
        base_in, base_out = _token_totals(img_base)
        new_in, new_out = _token_totals(img_new)
        total_base = base_in + base_out
        total_new = new_in + new_out
        img_reduction_pct = _reduction_pct(total_base, total_new)
        print(f"image-only baseline tokens (in+out): {total_base}")
        print(f"image-only page-route tokens (in+out): {total_new}")
        print(f"image-only token reduction:           {img_reduction_pct}%")
        per_doc = _per_doc_avg_reduction(img_base, img_new)
        if per_doc is not None:
            print(f"avg per-doc token reduction (image-only): {per_doc}%")

        img_pages_total = sum(len(r["page_strategies"]) for r in img_new)
        img_vision_pages = sum(
            sum(1 for s in r["page_strategies"] if s == "vision") for r in img_new
        )
        img_vision_pct = (
            round(100 * img_vision_pages / img_pages_total, 2)
            if img_pages_total
            else 0.0
        )
        print(f"image-only VISION-mode %: {img_vision_pct}")

        # Hard gate: at least one image-only PDF must route to vision under
        # the page-routing leg. If the page router classifies them all as
        # text (mis-classification), the optimization isn't exercised and
        # the gate result is meaningless.
        if img_vision_pct < 100:
            hard_gate_failures.append(
                f"image-only VISION-mode pct {img_vision_pct} < 100 — "
                "page-router mis-classified an image_only_* PDF. "
                "Verify pymupdf text-layer detection on the failing doc(s)."
            )

        # Hard gate: image-only page-routing leg must reduce tokens vs
        # baseline. The baseline runs every doc through the document-block
        # extractor (vision-equivalent payload size); the page-routing
        # leg should produce identical or smaller token counts because
        # the per-page classifier adds at most a small fixed overhead
        # and identifies vision as the right strategy upfront. A negative
        # delta here means the optimization is actively wasting tokens
        # on image-only docs, which is the regression we must catch.
        if total_new >= total_base:
            hard_gate_failures.append(
                f"image-only page-routing used >= baseline tokens "
                f"(new={total_new} base={total_base}, "
                f"delta={total_new - total_base:+d}, {img_reduction_pct}%). "
                "Page-routing should not regress vision-only docs. "
                "Investigate per-page classifier overhead vs whole-doc."
            )
    else:
        # No image_only_* fixtures present in BOTH legs. We deliberately
        # treat this as a HARD failure — without the mixed-modality
        # subset the gate's vision branch is unverifiable, and silent
        # acceptance is what got us into the soft-signal demotion in
        # the first place. The fix is to regenerate the corpus
        # (``python -m scripts.generate_image_only_corpus``).
        hard_gate_failures.append(
            "image_only_* PDFs missing from at least one leg of the run; "
            "regenerate via `python -m scripts.generate_image_only_corpus`"
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
