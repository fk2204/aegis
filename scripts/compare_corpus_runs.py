"""Diff two `run_corpus_bedrock.py` outputs for the verification gate.

Compares a baseline run (page_routing_enabled=False) against a
new-path run (page_routing_enabled=True). Prints the TEXT-mode
percentage (must be >=80% for the new-path run on clean scenarios)
and the token-reduction average.

Exits non-zero if any of:
  - either run has failed_docs > 0
  - TEXT-mode pct < 80 on the new-path run's clean scenarios
  - new path uses >= baseline tokens on clean scenarios
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

        gate_failures: list[str] = []
        if base_fails or new_fails:
            gate_failures.append(f"failed_docs (baseline={base_fails}, new={new_fails})")
        if clean_text_pct < 80:
            gate_failures.append(f"clean TEXT-mode pct {clean_text_pct} < 80")
        if total_new >= total_base:
            gate_failures.append(
                f"new path used >= baseline tokens (new={total_new} base={total_base})"
            )
        if gate_failures:
            print()
            print("GATE FAILURES:")
            for f in gate_failures:
                print(f"  - {f}")
            return 1
    print()
    print("Gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
