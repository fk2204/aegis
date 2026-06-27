"""Deterministic auto-hint generation from a successful parse.

Companion to operator-authored hints in
``aegis.bank_layouts.repository.set_hints(..., source="manual")`` —
``generate_hints_from_parse_result`` runs at the tail of every
``parse_status in ('proceed', 'review')`` pipeline call and extracts
layout characteristics from the first page text + parse result:

  * **Period format** — date strings near "statement period", "from",
    "through", "to" labels in the first-page text. Capture the format
    string seen ("Month DD, YYYY through Month DD, YYYY",
    "MM/DD/YYYY - MM/DD/YYYY", etc.) so the next parse's prompt nudges
    Bedrock toward the same recognition.
  * **Running balance** — whether transactions carried a per-row
    ``running_balance``. Boolean signal that mirrors the existing
    fingerprint key but lives in the hint text so the prompt sees it.
  * **Summary block labels** — what the bank actually calls Total
    Deposits / Total Withdrawals / Beginning / Ending balance. Regex on
    first_page_text; case-preserving (operators care about the exact
    label for downstream reconciliation).
  * **Transaction section header** — the column-header row ("Date /
    Description / Amount / Balance").
  * **Bank identifier** — first non-empty line of meaningful text near
    the top of page 1, useful as a sanity check for downstream
    consumers.

``merge_hints`` appends new observations to an existing hint string
without duplicating: if the existing hint already mentions period
format, the new period-format sentence is skipped. Idempotent —
``merge_hints(merge_hints(None, x), x) == merge_hints(None, x)``.

The generator is intentionally conservative: when the first-page text
is too short, too noisy, or matches none of the expected patterns the
function returns ``""`` and the caller skips the ``set_hints`` write.
A bad hint is worse than no hint — better to wait for a second parse
to clarify than to confidently mislead Bedrock.

NO PII. The hint text describes structural layout ("Period formatted
as Month DD, YYYY through Month DD, YYYY") not content. Account
holder names, transaction descriptions, and any merchant identifier
MUST NOT appear in the generated hint. The label extractions below
deliberately capture canonical bank-side strings ("Total Deposits"),
not the merchant-specific values they describe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ``str | list[str]`` parameter typing is sufficient at the boundary;
# ``Any`` would defeat the strict-typing check the rest of the parser
# observes. The parse_result is consumed via attribute access on
# ``transactions`` only (we don't reach into the full PipelineResult
# shape — that would couple the auto-hint generator to a moving
# target).


@dataclass(frozen=True)
class _HintFragments:
    """Internal collector — one optional sentence per observation."""

    period_format: str | None
    running_balance: str | None
    summary_labels: str | None
    transaction_header: str | None
    bank_identifier: str | None

    def joined(self) -> str:
        """Return the non-empty sentences joined with single spaces."""
        sentences = [
            self.period_format,
            self.running_balance,
            self.summary_labels,
            self.transaction_header,
            self.bank_identifier,
        ]
        return " ".join(s for s in sentences if s)


_HINT_PREFIX = "Auto-derived layout hints from a prior successful parse: "

# Period-label regexes. Order matters — the first match wins, so the
# more specific patterns (with explicit separators) come first.
_PERIOD_LABEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "'Month DD, YYYY through Month DD, YYYY' (full English month, "
        "literal 'through' as separator)",
        re.compile(
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+\d{1,2},?\s+\d{4}\s+through\s+"
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+\d{1,2},?\s+\d{4}",
            re.IGNORECASE,
        ),
    ),
    (
        "'Month DD, YYYY to Month DD, YYYY' (full English month, literal 'to' as separator)",
        re.compile(
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+\d{1,2},?\s+\d{4}\s+to\s+"
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
            r"\s+\d{1,2},?\s+\d{4}",
            re.IGNORECASE,
        ),
    ),
    (
        "'MMM DD YYYY-MMM DD YYYY' (3-letter month, single hyphen, no spaces)",
        re.compile(
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+\d{1,2}\s+\d{4}-"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+\d{1,2}\s+\d{4}",
        ),
    ),
    (
        "'MMM DD, YYYY thru MMM DD, YYYY' (3-letter month, literal 'thru' as separator)",
        re.compile(
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+\d{1,2},?\s+\d{4}\s+thru\s+"
            r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
            r"\s+\d{1,2},?\s+\d{4}",
        ),
    ),
    (
        "'MM/DD/YYYY - MM/DD/YYYY' (numeric date with spaced hyphen)",
        re.compile(r"\d{2}/\d{2}/\d{4}\s+-\s+\d{2}/\d{2}/\d{4}"),
    ),
    (
        "'MM/DD/YY-MM/DD/YY' (numeric short-year, hyphen, no spaces)",
        re.compile(r"\d{2}/\d{2}/\d{2}-\d{2}/\d{2}/\d{2}"),
    ),
)

# Canonical summary-block labels. We probe for the operator-facing
# label substrings; presence is the signal. The hint surfaces the
# specific subset the bank uses so the prompt steers Bedrock to label-
# match instead of label-guess.
_SUMMARY_LABEL_CANDIDATES: tuple[str, ...] = (
    "Total Deposits",
    "Total Withdrawals",
    "Beginning Balance",
    "Ending Balance",
    "Deposits and Additions",
    "Checks Paid",
    "Electronic Withdrawals",
    "Service Charges",
    "Account Summary",
    "Account summary",
    "CHECKING SUMMARY",
    "ACCOUNT SUMMARY",
    "Balance Summary",
    "Starting Balance",
)

# Header rows for the transaction section. Several variants because the
# column ordering + delimiter differ across banks; we report verbatim
# the one that appears.
_TRANSACTION_HEADER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bDate\s+Description\s+Amount(?:\s+Balance)?\b", re.IGNORECASE),
    re.compile(
        r"\bPost\s*Date\s+Description\s+Debits?\s+Credits?(?:\s+Balance)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bDate\s+Description\s+Deposits?\s+Withdrawals?(?:\s+Balance)?\b",
        re.IGNORECASE,
    ),
)


def _extract_period_format(first_page_text: str) -> str | None:
    """Return the period-format hint sentence or None.

    Walks the regex table in order; first match wins. Returns a
    sentence describing the format observed, e.g.
    ``"Period formatted as 'Month DD, YYYY through Month DD, YYYY'."``.
    """
    for description, pattern in _PERIOD_LABEL_PATTERNS:
        if pattern.search(first_page_text):
            return f"Period formatted as {description}."
    return None


def _extract_running_balance(parse_result: object) -> str | None:
    """Return the running-balance hint sentence or None.

    ``parse_result`` is typed ``object`` to keep this module decoupled
    from ``PipelineResult``; the attribute path
    ``parse_result.classified.transactions[i].running_balance`` is the
    expected duck-typed shape (matches what ``pipeline.py`` produces).
    Falls back through the simpler ``extraction.statement.transactions``
    path used by older test inputs. Returns None when neither path
    yields any transactions.
    """
    transactions = _resolve_transactions(parse_result)
    if not transactions:
        return None
    has_running = any(getattr(t, "running_balance", None) is not None for t in transactions)
    if has_running:
        return (
            "Transactions include a per-row running-balance column — use it "
            "for transaction-by-transaction reconciliation."
        )
    return (
        "Transactions do NOT include a per-row running-balance column; "
        "daily / period balances appear in a separate summary block."
    )


def _resolve_transactions(parse_result: object) -> list[object]:
    """Pull the transactions iterable out of a parse_result-shaped object.

    Two duck-typed paths: the ``classified.transactions`` shape produced
    by the live pipeline, and the older ``extraction.statement.transactions``
    shape used in some tests. Returns ``[]`` when neither path exists.
    """
    classified = getattr(parse_result, "classified", None)
    txs = getattr(classified, "transactions", None) if classified else None
    if txs is not None:
        return list(txs)
    extraction = getattr(parse_result, "extraction", None)
    statement = getattr(extraction, "statement", None) if extraction else None
    txs = getattr(statement, "transactions", None) if statement else None
    if txs is not None:
        return list(txs)
    return []


def _extract_summary_labels(first_page_text: str) -> str | None:
    """Return the summary-label hint sentence or None.

    Reports the canonical labels that actually appear in the first-page
    text. Bedrock benefits more from "the bank uses these specific
    labels" than from a generic "the bank has summary labels".
    """
    hits = [label for label in _SUMMARY_LABEL_CANDIDATES if label in first_page_text]
    if not hits:
        return None
    # Dedup case-insensitive while preserving the first-seen casing.
    seen_lower: set[str] = set()
    unique: list[str] = []
    for label in hits:
        if label.lower() not in seen_lower:
            seen_lower.add(label.lower())
            unique.append(label)
    quoted = ", ".join(f"'{label}'" for label in unique)
    return f"Summary block uses labels: {quoted}."


def _extract_transaction_header(first_page_text: str) -> str | None:
    """Return the transaction-header hint sentence or None.

    Probes a small table of common header shapes; reports the first
    match verbatim so the prompt sees the exact column ordering.
    """
    for pattern in _TRANSACTION_HEADER_PATTERNS:
        match = pattern.search(first_page_text)
        if match:
            verbatim = " ".join(match.group(0).split())
            return f"Transaction section header: {verbatim}."
    return None


def _extract_bank_identifier(first_page_text: str, bank_name: str) -> str | None:
    """Return the bank-identifier hint sentence or None.

    Confirms the bank-name string appears at the top of page 1; useful
    sanity check for downstream consumers. Returns None when the bank
    name string is absent (the caller already trusts the parser's
    bank-name extraction; this just affirms the layout location).
    """
    # Look in the first 800 characters — that's typically the top
    # banner + header block before any transaction tables start.
    top_block = first_page_text[:800]
    if bank_name and bank_name in top_block:
        return f"Bank identifier '{bank_name}' appears in the top banner of page 1."
    return None


def generate_hints_from_parse_result(
    bank_name: str,
    first_page_text: str,
    parse_result: object,
) -> str:
    """Return a hint string derived from the parse result.

    Returns ``""`` when the input is too sparse to produce any useful
    observation (caller skips the write — a bad hint is worse than no
    hint). The returned string is prefixed with ``_HINT_PREFIX`` so
    ``merge_hints`` can deduplicate by sentence.

    Pure function — no side effects, no I/O. Tests can call this with
    a hand-rolled ``parse_result`` shape that satisfies the duck-typed
    attribute access.
    """
    if not first_page_text or not first_page_text.strip():
        return ""
    fragments = _HintFragments(
        period_format=_extract_period_format(first_page_text),
        running_balance=_extract_running_balance(parse_result),
        summary_labels=_extract_summary_labels(first_page_text),
        transaction_header=_extract_transaction_header(first_page_text),
        bank_identifier=_extract_bank_identifier(first_page_text, bank_name),
    )
    body = fragments.joined()
    if not body:
        return ""
    return _HINT_PREFIX + body


def merge_hints(existing: str | None, new: str) -> str:
    """Append ``new`` observations to ``existing`` without duplicating.

    Splits on the sentence-terminator '.' (the auto-hint generator
    produces canonical sentences) and de-duplicates case-insensitively.
    Preserves the existing prefix line ('Auto-derived layout hints
    from a prior successful parse:' or whatever the manual writer
    chose) by treating any leading non-sentence preamble as a single
    sentence. Idempotent — re-running on the same input yields the
    same output.

    Returns ``new`` verbatim when ``existing`` is None / empty.
    Returns ``existing`` verbatim when ``new`` is empty (a no-op merge).
    """
    if not existing or not existing.strip():
        return new
    if not new or not new.strip():
        return existing
    existing_sentences = _split_sentences(existing)
    new_sentences = _split_sentences(new)
    seen_lower = {s.lower() for s in existing_sentences}
    merged = list(existing_sentences)
    for s in new_sentences:
        if s.lower() not in seen_lower:
            merged.append(s)
            seen_lower.add(s.lower())
    return " ".join(merged)


def _split_sentences(text: str) -> list[str]:
    """Split hint text into normalized sentences ending in '.'.

    The auto-hint generator emits canonical sentences with single-space
    separators and trailing periods; this splitter preserves those and
    is tolerant of operator-authored manual hints that may not strictly
    follow the same pattern (collapses whitespace, preserves trailing
    period if absent only on the last fragment).
    """
    # Collapse runs of whitespace to a single space, then split on '. '
    # while keeping the period attached.
    normalized = re.sub(r"\s+", " ", text.strip())
    # Split, then re-attach the period to every fragment except the
    # last when it already had one.
    raw = [s.strip() for s in normalized.split(". ") if s.strip()]
    out: list[str] = []
    for fragment in raw:
        if fragment.endswith("."):
            out.append(fragment)
        else:
            out.append(fragment + ".")
    return out


__all__ = [
    "generate_hints_from_parse_result",
    "merge_hints",
]
