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
from typing import Final, Protocol

from aegis.logger import get_logger

_log = get_logger(__name__)

_SUMMARY_CAP: int = 1200  # chars
_LIST_CAP: int = 15


# State Secretary-of-State / UCC search portals (all 50 states + DC).
#
# Sourced from public SOS websites. Where a dedicated UCC search exists,
# the dedicated URL is used. Where the SOS exposes only a unified
# business / lien search, that combined URL is used (UCC search is
# accessible from the same page). The operator clicks the verify link
# on the dossier to open these in a new tab — the URL is also
# substituted into the Bedrock prompt so the model knows which portal
# to consult for the state in question.
#
# When a particular state has multiple plausible URLs (e.g. the SOS
# main UCC page vs. a deeper search form), the deeper form is preferred
# so the operator lands on the actual search rather than on marketing
# text.
UCC_STATE_PORTALS: Final[dict[str, str]] = {
    "AL": "https://arc-sos.state.al.us/cgi/corpname.mbr/output",
    "AK": "https://www.commerce.alaska.gov/cbp/main/search/entities",
    "AZ": "https://apps.azsos.gov/apps/ucc/search/",
    "AR": "https://www.sos.arkansas.gov/ucc/online-services/search-ucc-filings",
    "CA": "https://uccconnect.sos.ca.gov/search/common",
    "CO": "https://www.coloradosos.gov/biz/UCCSearchCriteria.do",
    "CT": "https://www.concord-sots.ct.gov/CONCORD/online?sn=PublicInquiry&eid=9743",
    "DE": "https://icis.corp.delaware.gov/ecorp/UCCSearch/UCCSearchOptions.aspx",
    "DC": "https://corp.dcra.dc.gov/Account/Login?ReturnUrl=%2FUCC%2FUCCSearch",
    "FL": "https://search.sunbiz.org/Inquiry/UCCSearch/ByName",
    "GA": "https://www.gsccca.org/search/ucc/namesearch.aspx",
    "HI": "https://hbe.ehawaii.gov/documents/search.html?domain=ucc",
    "ID": "https://www.accessidaho.org/secure/sos/ucc/search.html",
    "IL": "https://www.ilsos.gov/uccsearch/",
    "IN": "https://uccsearch.sos.in.gov/Home/Search",
    "IA": "https://sos.iowa.gov/search/ucc/search.aspx",
    "KS": "https://www.kansas.gov/ucc/index.do",
    "KY": "https://web.sos.ky.gov/UCC/Search",
    "LA": "https://coraweb.sos.la.gov/UCCSearchOnline/UCCSearchOnline.aspx",
    "ME": "https://icrs.informe.org/nei-sos-icrs/ICRS",  # TODO: confirm dedicated UCC search URL
    "MD": "https://egov.maryland.gov/businessexpress/entitysearch",
    "MA": "https://www.sec.state.ma.us/ucc/uccsearch/uccsearch.asp",
    "MI": "https://cofs.lara.state.mi.us/SOSCRA/UCC/UCCSearch",
    "MN": "https://www.sos.state.mn.us/business-liens/business-filings-search/",
    "MS": "https://www.sos.ms.gov/UCC/Pages/default.aspx",  # TODO: confirm direct search URL
    "MO": "https://bsd.sos.mo.gov/ucc/ucc.aspx",
    "MT": "https://sosmt.gov/business/ucc/",  # TODO: confirm direct UCC search URL
    "NE": "https://www.nebraska.gov/sos/ucc/index.cgi",
    "NV": "https://esos.nv.gov/EnterpriseUI/Pages/UCC/UCCFiling.aspx",
    "NH": "https://quickstart.sos.nh.gov/online/UCC/UCCSearch",
    "NJ": "https://www.njportal.com/DOR/BusinessNameSearch/Search/UCC",
    "NM": "https://portal.sos.state.nm.us/UCC/Search",
    "NY": "https://appext20.dos.ny.gov/pls/ucc_public/web_search.main_frame",
    "NC": "https://www.sosnc.gov/online_services/search/by_title/_UCC",
    "ND": "https://firststop.sos.nd.gov/search/ucc",
    # TODO: confirm UCC search URL (OH redirects search through CIS)
    "OH": "https://bizimage.ohiosos.gov/api/image/pdf/",
    "OK": "https://www.sos.ok.gov/business/ucc/default.aspx",
    "OR": "https://sos.oregon.gov/business/Pages/find.aspx",
    "PA": "https://www.corporations.pa.gov/search/UCCsearch",
    "RI": "https://business.sos.ri.gov/CorpWeb/UCCSearch/UCCSearch.aspx",
    "SC": "https://uccfiling.sc.gov/search",
    "SD": "https://sosenterprise.sd.gov/BusinessServices/UCC/UCCSearch.aspx",
    "TN": "https://tnbear.tn.gov/ucc/Home",
    "TX": "https://direct.sos.state.tx.us/ucc/uccnameinquiry.asp",
    "UT": "https://secure.utah.gov/uccsearch/uccs",
    "VT": "https://www.vermontbusinessregistry.com/UccSearch.aspx",
    "VA": "https://cis.scc.virginia.gov/EntitySearch/UCCSearch",
    "WA": "https://www.sos.wa.gov/corps/uccsearch.aspx",
    "WV": "https://apps.sos.wv.gov/business/uccsearch/",
    "WI": "https://www.wdfi.org/UCC/Search/",
    "WY": "https://wyobiz.wy.gov/Business/FilingSearch.aspx",
}


def build_ucc_prompt(business_name: str, state: str) -> str:
    """Return the state-targeted UCC search prompt.

    Falls back to the generic "state Secretary of State website"
    string when ``state`` isn't in ``UCC_STATE_PORTALS`` (unknown /
    non-US / empty). Empty business_name is the caller's
    responsibility to reject; this helper assumes both are non-empty
    and renders the prompt as-is.
    """
    portal = UCC_STATE_PORTALS.get(
        (state or "").strip().upper(),
        "the state Secretary of State website",
    )
    return f"""Search {portal} for UCC-1 financing statements filed against '{business_name}'.
Report EXACTLY:
1. Number of ACTIVE UCC filings (not terminated/expired)
2. For each active filing: secured party name, collateral description, filing date
3. Whether any filing covers 'all assets' or 'all accounts receivable' (blanket lien)
4. Whether any secured party name contains: Rapid Finance, OnDeck, Kapitus, Fora Financial,
   Yellowstone, World Business Lenders, Expansion Capital, Idea Financial, Libertas,
   Credibly, Forward Financing, National Business Capital, or words: funding, merchant,
   advance, capital group, business capital
5. Lien position — 1st, 2nd, or 3rd+ on all assets
6. State explicitly: 'No UCC filings found' if search returns empty
Report ONLY active filings. Do NOT report terminated or expired filings.

Return ONE JSON object and nothing else. No prose, no code fence.

{{
  "ucc_filings": ["<secured-party / lender name 1>", "..."],
  "default_indicators": ["<short tag describing one red flag>", "..."],
  "blanket_lien": true,
  "mca_funder_detected": true,
  "lien_position": "1st",
  "source_summary": "<one short sentence describing what you found and where>"
}}

Rules:
* ``ucc_filings`` lists secured-party names from ACTIVE filings only.
  Empty list when ``"No UCC filings found"``.
* ``default_indicators`` lists short tags like
  ``blanket_lien_all_assets``, ``mca_funder_secured_party``,
  ``collections_action_civil_court``. Empty list when none surfaced.
* ``blanket_lien`` is true when ANY active filing covers "all assets"
  or "all accounts receivable".
* ``mca_funder_detected`` is true when ANY secured-party string
  matches the watch list in step 4 above.
* ``lien_position`` is one of ``"1st"`` / ``"2nd"`` / ``"3rd+"`` /
  ``"unknown"``.
* ``source_summary`` is a plain-English one-sentence digest naming
  the portal you consulted. When NOTHING is found, set it to
  "No active UCC filings located at {portal} for this business."
  and leave both lists empty."""


# Legacy prompt kept inline as a comment for historical reference only:
# the previous generic prompt suggested ad-hoc search queries; the
# replacement above narrows the model to the specific state portal so
# the result is reproducible across runs.
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
    """Output of one check. Empty result = "no data, move on".

    ``blanket_lien``, ``mca_funder_detected``, and ``lien_position`` are
    optional structured-signal fields populated by the state-targeted
    prompt (``build_ucc_prompt``). Older response shapes from the
    legacy prompt leave them at their dataclass defaults, which the
    consumers (dossier, scorer) treat as "unknown" rather than "false".
    """

    ucc_filings: tuple[str, ...] = field(default_factory=tuple)
    default_indicators: tuple[str, ...] = field(default_factory=tuple)
    source_summary: str = ""
    checked_at: datetime | None = None
    blanket_lien: bool | None = None
    mca_funder_detected: bool | None = None
    lien_position: str | None = None


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

    # State-targeted prompt — uses the dedicated SOS / UCC portal URL
    # for the merchant's state. When state is missing or unknown the
    # helper falls back to a generic "state Secretary of State website"
    # string so the model still has something to anchor on.
    prompt = build_ucc_prompt(business_name=name, state=(state or "").strip())

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
        parsed = _parse_response(raw)
    except (ValueError, json.JSONDecodeError):
        _log.warning(
            "ucc_checker.parse_failed business_name=%s raw=%r",
            name,
            raw[:200],
            exc_info=True,
        )
        return UCCResult()

    return UCCResult(
        ucc_filings=parsed.filings,
        default_indicators=parsed.defaults,
        source_summary=parsed.summary,
        checked_at=datetime.now(UTC),
        blanket_lien=parsed.blanket_lien,
        mca_funder_detected=parsed.mca_funder_detected,
        lien_position=parsed.lien_position,
    )


_CODE_FENCE_OPEN = re.compile(r"^```(?:json)?\s*", flags=re.IGNORECASE)
_CODE_FENCE_CLOSE = re.compile(r"\s*```\s*$")

_VALID_LIEN_POSITIONS: Final[frozenset[str]] = frozenset({"1st", "2nd", "3rd+", "unknown"})


@dataclass(frozen=True)
class _ParsedResponse:
    """Internal shape returned by ``_parse_response``.

    Carries both the legacy-shape fields and the new structured signals
    so the public ``check_ucc_and_defaults`` callsite stays a single
    constructor call rather than a tuple-unpack-with-optional-trailer.
    """

    filings: tuple[str, ...]
    defaults: tuple[str, ...]
    summary: str
    blanket_lien: bool | None
    mca_funder_detected: bool | None
    lien_position: str | None


def _parse_response(raw: str) -> _ParsedResponse:
    """Coerce the model's text into ``_ParsedResponse``.

    Accepts both the legacy response shape (filings + defaults +
    summary) and the new structured shape (legacy fields + blanket_lien
    + mca_funder_detected + lien_position). Missing structured fields
    collapse to ``None`` rather than raising — the new prompt is
    forward-compatible with existing tests / callers.
    """
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

    blanket_raw = data.get("blanket_lien")
    blanket: bool | None = blanket_raw if isinstance(blanket_raw, bool) else None

    mca_raw = data.get("mca_funder_detected")
    mca: bool | None = mca_raw if isinstance(mca_raw, bool) else None

    lien_pos_raw = data.get("lien_position")
    lien_pos: str | None
    if isinstance(lien_pos_raw, str) and lien_pos_raw.strip() in _VALID_LIEN_POSITIONS:
        lien_pos = lien_pos_raw.strip()
    else:
        lien_pos = None

    return _ParsedResponse(
        filings=tuple(filings),
        defaults=tuple(defaults),
        summary=summary_raw.strip()[:_SUMMARY_CAP],
        blanket_lien=blanket,
        mca_funder_detected=mca,
        lien_position=lien_pos,
    )


__all__ = ["UCC_STATE_PORTALS", "UCCResult", "build_ucc_prompt", "check_ucc_and_defaults"]
