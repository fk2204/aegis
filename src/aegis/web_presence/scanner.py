"""Reputation lookup via Bedrock + web_search tool.

``scan_web_presence(business_name, city, state)`` calls Claude on
Bedrock with the ``web_search_20250305`` server tool enabled, parses
the structured JSON response into a ``WebPresenceResult``, and returns.
Every failure mode (Bedrock unavailable, malformed response, empty
business name) collapses to an empty ``WebPresenceResult`` — callers
treat that as "no data" and move on. The scan is a soft signal; never
a decline reason.

The scan is bounded:

* ``max_uses=5`` web-search round-trips per invocation (server-side).
* ``max_tokens=2048`` response budget.
* No retry. The retry budget belongs to the operator's explicit refresh
  click, not to the failure path — repeatedly hammering Bedrock from a
  scoring path that may fire on every dossier render would burn quota
  without improving signal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from aegis.logger import get_logger

_log = get_logger(__name__)

# Caps so a runaway model can't blow the dossier or DB row.
_SUMMARY_CAP: int = 800
_FLAGS_CAP: int = 20

_PROMPT_TEMPLATE = """\
You have access to a web_search tool. Use it to investigate this
business's online reputation.

Business: {business_name}
{location_line}

Return ONE JSON object and nothing else. Do not wrap it in a code
fence. Do not write any prose around it. The shape:

{{
  "summary": "<two short sentences describing what the web shows>",
  "risk_flags": ["<lowercase_snake_case_tag>", ...]
}}

risk_flags is a (possibly empty) list of short tags for any red flag
you discover. Use tags like:
  bbb_unresolved_complaints, permanently_closed, recently_closed,
  active_lawsuits, regulatory_action, negative_review_pattern,
  unverified_no_online_presence, name_collision_uncertain.

When the business cannot be confidently identified at all, the summary
should say so plainly and risk_flags should contain
``unverified_no_online_presence``."""


@dataclass(frozen=True)
class WebPresenceResult:
    """Output of one scan. Empty result = "no data, move on"."""

    summary: str = ""
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    scanned_at: datetime | None = None


class _WebSearchClient(Protocol):
    """Minimal protocol the scanner needs.

    Production: ``BedrockClient.invoke_with_web_search``. Tests inject a
    stub that returns canned text without hitting the network.
    """

    def invoke_with_web_search(self, prompt: str) -> str: ...


def scan_web_presence(
    business_name: str,
    city: str | None = None,
    state: str | None = None,
    *,
    client: _WebSearchClient | None = None,
) -> WebPresenceResult:
    """Run one reputation scan. Returns an empty result on any failure.

    ``client`` is injected for testability. When omitted the function
    constructs a fresh ``BedrockClient`` — lazy because tests that never
    call the scanner shouldn't need Bedrock creds present.
    """
    name = (business_name or "").strip()
    if not name:
        return WebPresenceResult()

    parts = [(c or "").strip() for c in (city, state)]
    location = ", ".join(p for p in parts if p)
    location_line = f"Location: {location}" if location else "Location: (not specified)"

    prompt = _PROMPT_TEMPLATE.format(
        business_name=name,
        location_line=location_line,
    )

    if client is None:
        try:
            from aegis.llm import BedrockClient

            client = BedrockClient()
        except Exception:
            _log.warning("web_presence.client_init_failed business_name=%s", name, exc_info=True)
            return WebPresenceResult()

    try:
        raw = client.invoke_with_web_search(prompt)
    except Exception:
        _log.warning("web_presence.bedrock_invoke_failed business_name=%s", name, exc_info=True)
        return WebPresenceResult()

    try:
        summary, risk_flags = _parse_response(raw)
    except (ValueError, json.JSONDecodeError):
        _log.warning(
            "web_presence.parse_failed business_name=%s raw=%r",
            name,
            raw[:200],
            exc_info=True,
        )
        return WebPresenceResult()

    return WebPresenceResult(
        summary=summary,
        risk_flags=risk_flags,
        scanned_at=datetime.now(UTC),
    )


_CODE_FENCE_OPEN = re.compile(r"^```(?:json)?\s*", flags=re.IGNORECASE)
_CODE_FENCE_CLOSE = re.compile(r"\s*```\s*$")


def _parse_response(raw: str) -> tuple[str, tuple[str, ...]]:
    """Coerce the model's text into ``(summary, risk_flags)``.

    Strips optional code fences (Claude sometimes wraps JSON despite the
    prompt's explicit instruction not to). Caps the summary and flag
    count so a runaway response can't blow up the DB row.
    """
    cleaned = (raw or "").strip()
    cleaned = _CODE_FENCE_OPEN.sub("", cleaned)
    cleaned = _CODE_FENCE_CLOSE.sub("", cleaned)

    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")

    summary_raw = data.get("summary", "")
    flags_raw = data.get("risk_flags", [])

    if not isinstance(summary_raw, str):
        raise ValueError("summary is not a string")
    if not isinstance(flags_raw, list):
        raise ValueError("risk_flags is not a list")

    flags_clean: list[str] = []
    for flag in flags_raw[:_FLAGS_CAP]:
        if not isinstance(flag, str):
            raise ValueError("risk_flags must contain only strings")
        normalized = flag.strip().lower()
        if normalized and normalized not in flags_clean:
            flags_clean.append(normalized)

    return summary_raw.strip()[:_SUMMARY_CAP], tuple(flags_clean)


__all__ = ["WebPresenceResult", "scan_web_presence"]
