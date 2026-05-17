---
paths:
  - "src/aegis/parser/**"
  - "src/aegis/core.py"
  - "tests/parser/**"
---

# AEGIS Parser Architecture

This rule auto-loads when editing the parser, the conductor (`core.py`), or parser tests. Different from the TS version — read carefully before making changes.

---

## Two-pass parser

The parser does NOT ask Claude for aggregates. Aggregates are derived from transaction rows in pure Python.

Flow:

1. **Metadata layer** — `pikepdf` inspects PDF tampering signals.
2. **Extraction (Claude pass 1)** — Claude returns the raw transaction list (every row, with date, description, amount, page number, line number, running balance if present) AND the printed statement summary (beginning balance, ending balance, deposit total as printed, withdrawal total as printed, statement period). NO classification yet, NO derived aggregates.
3. **Validation (deterministic)** — Python code verifies:
   - Sum of extracted deposits matches printed deposit total within $1
   - Sum of extracted withdrawals matches printed withdrawal total within $1
   - Beginning + sum(deposits) − sum(withdrawals) = ending, within $1
   - Daily running balance reconciles for every day with transactions
   - Number of extracted transactions matches printed count if available
   If ANY check fails, `parse_status = "manual_review"`. No retry, no second AI chance. The validation gate is the firewall against AI hallucination.
4. **Classification (Claude pass 2)** — Each transaction is classified into one of: `deposit`, `payroll`, `ach_credit`, `mca_debit`, `nsf_fee`, `wire_in`, `wire_out`, `transfer`, `fee`, `chargeback`, `refund`, `other`. Misclassifying one row is recoverable; misclassifying many shows up as low confidence and triggers review.
5. **Patterns (deterministic)** — MCA stacking, kiting, preloan spike, wash pairs, paydown detection. Pure Python over the classified rows.
6. **Aggregation (deterministic)** — Computes `avg_daily_balance`, `true_revenue` (deposits net of transfers and chargebacks), `num_nsf`, `days_negative`, `debt_to_revenue`, etc. Every aggregate stores the list of `transaction_ids` that produced it.

---

## Audit trail

Every aggregate field in the final report MUST be traceable to specific source transactions. The `analyses` table stores not just `total_deposits` but also `total_deposits_source_ids: list[uuid]` referencing the `transactions` table, where each transaction has `source_page` and `source_line` from the original PDF.

When a funder asks "where did this come from?" the answer is "page 7 lines 14, 22, 31, ..." — clickable.

---

## Storage shape

- `documents` — one row per PDF (file_hash, metadata flags, parse status)
- `transactions` — one row per extracted line item
- `analyses` — derived aggregates with `_source_ids` arrays back to `transactions`
- `audit_log` — every action with actor, timestamp, details

---

## Rules specific to parser code

- Pass 1 returns transactions only, never aggregates.
- Pass 2 returns classifications only, never aggregates.
- Aggregates are deterministic Python — NEVER from Claude.
- Validation failure = `manual_review`. Never retry the LLM after a failed validation; that defeats the firewall.
- Every transaction MUST have non-null `source_page` and `source_line` after extraction. Validator enforces this.
- Every aggregate MUST have a non-empty `_source_ids` array. Aggregator enforces this.
- Float comparisons use `abs(a - b) < Decimal("0.01")`, never `==`.