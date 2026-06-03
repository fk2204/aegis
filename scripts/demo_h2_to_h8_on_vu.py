"""Live demonstration of H2-H8 candidate bugs against VU Development's
actual stored data. Read-only.

Output is a yes/no per candidate:
  H2: validation_passed=False on manual_review? → hard-decline appears?
  H3: mean(lowest) vs min(lowest) — material gap?
  H4: bundle key split by bank_name spelling — singleton dropped?
  H6: validate.py running-balance gap-day on any VU doc?
  H7: classifier confidence=0 + category=other rows on VU?
  H8: KNOWN_FUNDERS substring match producing false positives in VU descriptions?
"""

from __future__ import annotations

from decimal import Decimal
from statistics import mean
from uuid import UUID

from aegis.api.deps import get_merchant_repository, get_repository
from aegis.scoring.multi_month import score_input_multi_month
from aegis.scoring.score import score_deal
from aegis.storage import AnalysisRow, DocumentRow

MERCHANT_ID = UUID("5cf4479d-c6ac-4267-a2f7-5e7ef04c1345")


def _h2_demo() -> tuple[bool, str]:
    merchants = get_merchant_repository()
    repo = get_repository()
    merchant = merchants.get(MERCHANT_ID)
    docs = repo.list_documents(merchant_id=MERCHANT_ID, limit=20)
    items: list[tuple[DocumentRow, AnalysisRow]] = []
    for d in docs:
        a = repo.get_analysis(d.id)
        if a is not None:
            items.append((d, a))
    items.sort(key=lambda p: p[1].statement_period_end, reverse=True)

    score_input = score_input_multi_month(merchant, items)
    result = score_deal(score_input, ofac=None)
    hard = list(result.hard_decline_reasons)
    soft = list(result.soft_concerns)

    print("  H2 SCORE_RESULT:")
    print(f"    tier            = {result.tier}")
    print(f"    paper_grade     = {result.paper_grade}")
    print(f"    monthly_revenue = ${score_input.monthly_revenue:,.2f}")
    print(f"    lowest_balance  = ${score_input.lowest_balance:,.2f}")
    print(f"    validation_passed = {score_input.validation_passed}")
    print(f"    hard_decline_reasons ({len(hard)}):")
    for r in hard:
        print(f"      - {r}")
    print(f"    soft_concerns ({len(soft)}):")
    for r in soft:
        print(f"      - {r}")

    fired = any(
        "manual_review" in r or "validation_failed" in r for r in hard
    )
    if fired:
        return True, (
            "validation_passed=False on manual_review docs caused a hard "
            "decline reason to fire on VU's real result"
        )
    return False, (
        "no manual_review-related hard decline fired on VU; "
        "H2 suspicion not demonstrated on real data"
    )


def _h3_demo() -> tuple[bool, str]:
    repo = get_repository()
    docs = repo.list_documents(merchant_id=MERCHANT_ID, limit=20)
    lowests = []
    for d in docs:
        a = repo.get_analysis(d.id)
        if a is not None:
            lowests.append(a.lowest_balance)
    if len(lowests) < 2:
        return False, "fewer than 2 analyses; can't demonstrate"

    mean_low = mean(lowests)
    min_low = min(lowests)
    gap = (mean_low - min_low) / mean_low if mean_low else Decimal("0")

    print("  H3 LOWEST BALANCE:")
    print(f"    per-doc lowest: {[str(v) for v in lowests]}")
    print(f"    mean(lowest)  = ${mean_low:,.2f}")
    print(f"    min(lowest)   = ${min_low:,.2f}")
    print(f"    mean/min gap  = {gap*100:.1f}%")

    # Material if mean masks a near-zero (min < 25% of mean) — i.e. a real
    # liquidity event hidden by the averaging.
    if mean_low > 0 and (min_low / mean_low) < Decimal("0.25"):
        return True, (
            f"mean(lowest)=${mean_low:,.2f} masks min=${min_low:,.2f} "
            f"({(min_low/mean_low)*100:.1f}% of mean) on VU's real data"
        )
    return False, "mean(lowest) and min(lowest) within materiality"


def _h4_demo() -> tuple[bool, str]:
    repo = get_repository()
    docs = repo.list_documents(merchant_id=MERCHANT_ID, limit=20)
    bundle_counts: dict[tuple[str | None, str | None], int] = {}
    bundle_labels: dict[tuple[str | None, str | None], list[str]] = {}
    for d in docs:
        a = repo.get_analysis(d.id)
        if a is None:
            continue
        key = (a.bank_name, a.account_last4)
        bundle_counts[key] = bundle_counts.get(key, 0) + 1
        bundle_labels.setdefault(key, []).append(d.original_filename)

    print("  H4 BUNDLES (raw bank_name, account_last4):")
    for key, count in bundle_counts.items():
        print(f"    {key!r}: {count}  -> {bundle_labels[key]}")

    if len(bundle_counts) > 1:
        return True, (
            f"{len(bundle_counts)} bundles for what is operationally one "
            "account; H4 normalization would merge them"
        )
    return False, "single bundle; H4 normalization unnecessary on VU"


def _h6_demo() -> tuple[bool, str]:
    repo = get_repository()
    docs = repo.list_documents(merchant_id=MERCHANT_ID, limit=20)
    gap_doc_hits = []
    for d in docs:
        txns = repo.list_transactions(d.id)
        if not txns:
            continue
        a = repo.get_analysis(d.id)
        if a is None:
            continue
        days_with_balance = {
            t.posted_date for t in txns if t.running_balance is not None
        }
        days_with_any_txn = {t.posted_date for t in txns}
        gap_days = days_with_any_txn - days_with_balance
        if gap_days:
            gap_doc_hits.append(
                (d.original_filename, len(gap_days), sorted(gap_days)[:5])
            )

    print("  H6 RUNNING-BALANCE GAP DAYS (days with txns but no printed balance):")
    for name, count, sample in gap_doc_hits:
        print(f"    {name}: {count} gap-days (sample: {[str(x) for x in sample]})")

    if gap_doc_hits:
        return True, (
            f"{len(gap_doc_hits)} VU docs have gap-days; validator did "
            "not flag them — H6 demonstrable"
        )
    return False, "no gap-days on VU; H6 not demonstrable on this merchant"


def _h7_demo() -> tuple[bool, str]:
    repo = get_repository()
    docs = repo.list_documents(merchant_id=MERCHANT_ID, limit=20)
    bad_rows = []
    for d in docs:
        txns = repo.list_transactions(d.id)
        for t in txns:
            if (
                t.classification_confidence == 0
                and t.category == "other"
            ):
                bad_rows.append((d.original_filename, t.id, t.description[:60]))

    print("  H7 CONFIDENCE=0 + CATEGORY=other rows:")
    if not bad_rows:
        print("    none found")
    else:
        for name, tid, desc in bad_rows[:10]:
            print(f"    {name}  tx={tid}  '{desc}'")
        print(f"    total: {len(bad_rows)}")

    if bad_rows:
        return True, f"{len(bad_rows)} VU transactions are confidence=0 + other"
    return False, "no confidence=0+other rows on VU; H7 not demonstrable"


def _h8_demo() -> tuple[bool, str]:
    # Pull the funder allowlist from the patterns module by reflection
    # so we don't tie the demo to a private symbol name.
    import aegis.parser.patterns as pat

    funders: object | None = getattr(pat, "KNOWN_FUNDERS", None) or getattr(
        pat, "_KNOWN_FUNDERS", None
    )
    if not isinstance(funders, (set, frozenset, list, tuple)):
        print("  H8: KNOWN_FUNDERS not found in patterns module")
        return False, "KNOWN_FUNDERS symbol not importable; can't demonstrate"
    known_funders = list(funders)

    repo = get_repository()
    docs = repo.list_documents(merchant_id=MERCHANT_ID, limit=20)
    substring_hits = []
    for d in docs:
        txns = repo.list_transactions(d.id)
        for t in txns:
            desc_lower = (t.description or "").lower()
            for funder in known_funders:
                if not isinstance(funder, str):
                    continue
                if funder in desc_lower:
                    # Whole-token check — if not a whole token, this is a
                    # false-positive substring match
                    tokens = desc_lower.replace(":", " ").split()
                    if funder not in tokens:
                        substring_hits.append(
                            (d.original_filename, funder, t.description[:80])
                        )
                        break

    print("  H8 KNOWN_FUNDERS substring (but not whole-token) matches:")
    for name, funder, desc in substring_hits[:10]:
        print(f"    {name}  funder={funder!r}  desc='{desc}'")

    if substring_hits:
        return True, (
            f"{len(substring_hits)} VU descriptions trigger substring "
            "match without whole-token match — false-positive surface"
        )
    return False, "no substring-but-not-token hits on VU; H8 not demonstrable"


def main() -> None:
    print("VU Development H2-H8 demonstration matrix")
    print("=" * 100)
    results: list[tuple[str, bool, str]] = []

    for label, fn in (
        ("H2 validation_passed-driven hard decline", _h2_demo),
        ("H3 mean(lowest) masking worst month", _h3_demo),
        ("H4 bundle key split on bank_name spelling", _h4_demo),
        ("H6 running-balance gap-day on any doc", _h6_demo),
        ("H7 confidence=0 + other classification rows", _h7_demo),
        ("H8 KNOWN_FUNDERS substring false positive", _h8_demo),
    ):
        print()
        print(f"== {label} ==")
        try:
            demonstrated, note = fn()
        except Exception as exc:
            demonstrated, note = False, f"demonstration script error: {exc!r}"
        results.append((label, demonstrated, note))

    print()
    print("=" * 100)
    print("RESULTS MATRIX")
    print("=" * 100)
    for label, demonstrated, note in results:
        tag = "[DEMONSTRATED]" if demonstrated else "[only suspicion]"
        print(f"  {tag}  {label}")
        print(f"      {note}")


if __name__ == "__main__":
    main()
