"""UCC filings + previous-default web search via Bedrock.

``check_ucc_and_defaults(business_name, state, owner_name)`` invokes
Claude on Bedrock with the ``web_search_20250305`` tool, asks for a
combined sweep of public UCC filings + lawsuit/judgment/MCA-default
mentions, parses the structured JSON into a ``UCCResult``, and
returns. Mirrors ``aegis.web_presence.scanner`` in posture: bounded
search budget, no retry, every failure mode collapses to an empty
result, soft-signal only.

Two parallel red-flag sweeps in one Bedrock call so the operator's
refresh costs one billed invocation rather than two:

* ``ucc_filings`` — secured-party strings the model surfaced from
  state-secretary or public UCC sites. The presence of any filing is
  meaningful (existing collateral commitments overlap MCA holdback);
  individual entries surface to the underwriter.
* ``default_indicators`` — short red-flag strings from lawsuits /
  judgments / MCA-default news / collections actions. Caller decides
  whether to escalate.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from aegis.logger import get_logger

_log = get_logger(__name__)

_SUMMARY_CAP: int = 1200  # chars
_LIST_CAP: int = 15

_PROMPT_TEMPLATE = """\
You have access to a web_search tool. Use AT MOST 5 web searches total
to investigate UCC filings + previous defaults for this business.

Business: {business_name}
State: {state}
Owner: {owner_name}

Suggested searches (use your judgment, no more than 5 total):
  * "{business_name} {state} UCC filing"
  * "{business_name} {owner_name} default judgment merchant cash advance"
  * "{business_name} lawsuit judgment"

Return ONE JSON object and nothing else. No prose, no code fence.

{{
  "ucc_filings": ["<secured-party / lender name 1>", "..."],
  "default_indicators": ["<short tag describing one red flag>", "..."],
  "source_summary": "<one short sentence describing what you found and where>"
}}

Rules:
* ``ucc_filings`` lists secured-party names from public UCC filings.
  Empty list when none found.
* ``default_indicators`` lists short tags like
  ``lawsuit_2024_judgment_50k``, ``mca_default_funder_x``,
  ``collections_action_civil_court``. Empty list when none found.
* ``source_summary`` is a plain-English one-sentence digest naming
  the sources (BBB, state SoS, court records, news). When NOTHING
  is found, set it to "No public UCC filings or default indicators
  located in available web sources." and leave both lists empty."""


@dataclass(frozen=True)
class UCCResult:
    """Output of one check. Empty result = "no data, move on"."""

    ucc_filings: tuple[str, ...] = field(default_factory=tuple)
    default_indicators: tuple[str, ...] = field(default_factory=tuple)
    source_summary: str = ""
    checked_at: datetime | None = None


class _WebSearchClient(Protocol):
    """Minimal protocol the checker needs.

    Production: ``BedrockClient.invoke_with_web_search``. Tests inject
    a stub that returns canned text without hitting the network.
    """

    def invoke_with_web_search(self, prompt: str) -> str: ...


def check_ucc_and_defaults(
    business_name: str,
    state: str | None = None,
    owner_name: str | None = None,
    *,
    client: _WebSearchClient | None = None,
) -> UCCResult:
    """Run one UCC + default check. Returns an empty result on any
    failure.

    ``client`` is injected for testability. When omitted the function
    lazily constructs a ``BedrockClient`` — tests that never call the
    checker shouldn't need Bedrock creds present.
    """
    name = (business_name or "").strip()
    if not name:
        return UCCResult()

    prompt = _PROMPT_TEMPLATE.format(
        business_name=name,
        state=(state or "").strip() or "(not specified)",
        owner_name=(owner_name or "").strip() or "(not specified)",
    )

    if client is None:
        try:
            from aegis.ops.cost_tracking import build_cost_tracking_client

            client = build_cost_tracking_client(call_type="business_intel")
        except Exception:
            _log.warning("ucc_checker.client_init_failed business_name=%s", name, exc_info=True)
            return UCCResult()

    try:
        raw = client.invoke_with_web_search(prompt)
    except Exception:
        _log.warning("ucc_checker.bedrock_invoke_failed business_name=%s", name, exc_info=True)
        return UCCResult()

    try:
        filings, defaults, summary = _parse_response(raw)
    except (ValueError, json.JSONDecodeError):
        _log.warning(
            "ucc_checker.parse_failed business_name=%s raw=%r",
            name,
            raw[:200],
            exc_info=True,
        )
        return UCCResult()

    return UCCResult(
        ucc_filings=filings,
        default_indicators=defaults,
        source_summary=summary,
        checked_at=datetime.now(UTC),
    )


_CODE_FENCE_OPEN = re.compile(r"^```(?:json)?\s*", flags=re.IGNORECASE)
_CODE_FENCE_CLOSE = re.compile(r"\s*```\s*$")


def _parse_response(raw: str) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Coerce the model's text into ``(filings, default_indicators, summary)``."""
    cleaned = (raw or "").strip()
    cleaned = _CODE_FENCE_OPEN.sub("", cleaned)
    cleaned = _CODE_FENCE_CLOSE.sub("", cleaned)

    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")

    filings_raw = data.get("ucc_filings", [])
    defaults_raw = data.get("default_indicators", [])
    summary_raw = data.get("source_summary", "")

    if not isinstance(filings_raw, list):
        raise ValueError("ucc_filings is not a list")
    if not isinstance(defaults_raw, list):
        raise ValueError("default_indicators is not a list")
    if not isinstance(summary_raw, str):
        raise ValueError("source_summary is not a string")

    filings: list[str] = []
    for f in filings_raw[:_LIST_CAP]:
        if not isinstance(f, str):
            raise ValueError("ucc_filings must contain only strings")
        normalized = f.strip()
        if normalized and normalized not in filings:
            filings.append(normalized)

    defaults: list[str] = []
    for d in defaults_raw[:_LIST_CAP]:
        if not isinstance(d, str):
            raise ValueError("default_indicators must contain only strings")
        normalized = d.strip()
        if normalized and normalized not in defaults:
            defaults.append(normalized)

    return tuple(filings), tuple(defaults), summary_raw.strip()[:_SUMMARY_CAP]


__all__ = ["UCCResult", "check_ucc_and_defaults"]
