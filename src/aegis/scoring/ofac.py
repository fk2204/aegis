"""OFAC SDN sanctions screening.

Hard-decline rule: if the merchant's `business_name` or `owner_name`
matches an entry on the U.S. Treasury OFAC SDN list, scoring returns
`recommendation="decline"` with `hard_decline_reasons=["ofac_sanctions_match"]`.

Cache policy (fail-closed)
--------------------------
- Cache file lives at `cache_path` (configurable; default
  `<state-dir>/ofac/sdn.json`).
- Refresh window: 24 hours. If the cache is < 24h old, use it unchanged.
- Stale-while-revalidate: if the cache is between 24h and 7 days old,
  try to refresh; on fetch failure, fall back to the stale copy and
  surface a warning.
- Hard cutoff: if the cache is >= 7 days old AND a refresh fails (or no
  cache exists at all), raise `OFACStaleError`. Scoring callers MUST
  treat this as a system fault — never silently allow a sanctioned name
  through because the list couldn't be refreshed.

Cache format
------------
JSON: `{"entries": [{"primary_name": str, "aliases": [str]}, ...],
"refreshed_at": "<ISO timestamp>"}`. The bundled `_treasury_fetch_xml`
helper (production) converts Treasury SDN.XML to this shape.

Match semantics
---------------
Conservative: an OFAC false-positive sends a deal to manual review,
which is better than a false-negative funding a sanctioned merchant.
Names normalize via lowercase + alphanumeric + token-set; we match if
every token of an SDN primary name (or alias) appears as a whole-token
substring of the input. So input "Acme Co owned by Vladimir Putin"
matches an SDN entry "Putin, Vladimir."
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

REFRESH_WINDOW = timedelta(hours=24)
HARD_CUTOFF = timedelta(days=7)
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")


class OFACError(RuntimeError):
    """Base class for OFAC client errors."""


class OFACStaleError(OFACError):
    """Cache is older than HARD_CUTOFF and refresh failed. Fail-closed."""


class OFACFetchError(OFACError):
    """Refresh attempt failed (network, parse, etc.)."""


@dataclass(frozen=True)
class SDNEntry:
    primary_name: str
    aliases: tuple[str, ...]

    def all_names(self) -> tuple[str, ...]:
        return (self.primary_name, *self.aliases)


Fetcher = Callable[[], bytes]
"""Returns raw cache-file bytes (JSON-encoded SDN list)."""


class OFACClient:
    """SDN screening with cache-aware refresh.

    Construct with `cache_path` (file location) and an optional `fetcher`
    (defaulting to `_treasury_fetch_json` in production). Tests inject a
    fake fetcher and a controllable `now` callable.
    """

    def __init__(
        self,
        cache_path: Path,
        fetcher: Fetcher | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._cache_path = cache_path
        self._fetcher = fetcher or _treasury_fetch_json
        self._now = now or (lambda: datetime.now(UTC))
        self._index: list[SDNEntry] | None = None
        self._loaded_mtime: float | None = None

    def is_match(self, name: str) -> bool:
        """Return True if `name` matches any SDN entry. Raises on stale cache."""
        if not name or not name.strip():
            return False
        self._ensure_fresh()
        norm_input = _normalize_tokens(name)
        if not norm_input:
            return False
        for entry in self._index or []:
            for candidate in entry.all_names():
                cand_tokens = _normalize_tokens(candidate)
                if not cand_tokens:
                    continue
                if cand_tokens.issubset(norm_input):
                    logger.warning(
                        "ofac_match: input matched SDN candidate (count=%d tokens)",
                        len(cand_tokens),
                    )
                    return True
        return False

    # -- cache lifecycle -----------------------------------------------------

    def _ensure_fresh(self) -> None:
        now = self._now()
        mtime = self._cache_mtime()
        age = (now - mtime) if mtime is not None else None

        # Case 1: no cache yet -> must fetch.
        if mtime is None:
            self._refresh_or_die(allow_stale=False, reason="no_cache")
            self._load_index_if_changed()
            return

        # Case 2: cache fresh -> use as-is.
        if age is not None and age < REFRESH_WINDOW:
            self._load_index_if_changed()
            return

        # Case 3: cache stale-but-within-cutoff -> try refresh, fall back.
        if age is not None and age < HARD_CUTOFF:
            try:
                self._refresh_or_die(allow_stale=False, reason="stale_under_cutoff")
            except OFACFetchError as exc:
                logger.warning(
                    "ofac_refresh_failed_using_stale: age=%s err=%s", age, exc
                )
            self._load_index_if_changed()
            return

        # Case 4: cache too old -> refresh required, fail-closed if it can't.
        self._refresh_or_die(allow_stale=False, reason="hard_cutoff_exceeded")
        self._load_index_if_changed()

    def _cache_mtime(self) -> datetime | None:
        if not self._cache_path.exists():
            return None
        ts = self._cache_path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=UTC)

    def _refresh_or_die(self, *, allow_stale: bool, reason: str) -> None:
        """Fetch + persist. Raises OFACStaleError on hard-cutoff failure."""
        _ = allow_stale  # reserved
        try:
            payload = self._fetcher()
        except Exception as exc:
            mtime = self._cache_mtime()
            now = self._now()
            age = (now - mtime) if mtime is not None else None
            if mtime is None or (age is not None and age >= HARD_CUTOFF):
                raise OFACStaleError(
                    f"OFAC SDN cache {'missing' if mtime is None else f'older than {HARD_CUTOFF}'} "
                    f"and refresh failed (reason={reason}): {exc}"
                ) from exc
            raise OFACFetchError(f"OFAC refresh failed (reason={reason}): {exc}") from exc

        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_bytes(payload)
        self._loaded_mtime = None  # force reload

    def _load_index_if_changed(self) -> None:
        mtime = self._cache_path.stat().st_mtime
        if self._index is not None and self._loaded_mtime == mtime:
            return
        try:
            payload = json.loads(self._cache_path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise OFACError(f"OFAC cache unreadable at {self._cache_path}: {exc}") from exc
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise OFACError(f"OFAC cache missing 'entries' list at {self._cache_path}")
        self._index = [
            SDNEntry(
                primary_name=str(e["primary_name"]),
                aliases=tuple(str(a) for a in e.get("aliases", [])),
            )
            for e in entries
            if isinstance(e, dict) and "primary_name" in e
        ]
        self._loaded_mtime = mtime


def _normalize_tokens(name: str) -> set[str]:
    """Lowercase, alnum tokens. Empty input -> empty set."""
    return set(_TOKEN_RE.findall(name.lower()))


SDN_XML_URL = (
    "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
)


def _treasury_fetch_json() -> bytes:
    """Production fetcher: download SDN.XML, convert to our JSON shape.

    Network failures, malformed XML, or empty entry lists all raise — the
    caller (`OFACClient._refresh_or_die`) translates that into a stale-cache
    fallback or a hard `OFACStaleError` per the cache age.
    """
    import httpx

    response = httpx.get(SDN_XML_URL, timeout=60.0, follow_redirects=True)
    response.raise_for_status()
    entries = parse_sdn_xml(response.content)
    if not entries:
        raise OFACFetchError("Treasury SDN.XML returned no entries")
    payload = {
        "entries": [
            {"primary_name": e.primary_name, "aliases": list(e.aliases)} for e in entries
        ],
        "refreshed_at": datetime.now(UTC).isoformat(),
        "source": SDN_XML_URL,
    }
    return json.dumps(payload).encode("utf-8")


def parse_sdn_xml(xml_bytes: bytes) -> list[SDNEntry]:
    """Parse Treasury SDN.XML bytes into our SDNEntry list.

    Schema (abbreviated):
      <sdnList xmlns="...">
        <sdnEntry>
          <uid>...</uid>
          <firstName>Vladimir</firstName>      <- individuals only
          <lastName>Putin</lastName>           <- entity name lives here too
          <sdnType>Individual|Entity|Vessel|Aircraft</sdnType>
          <akaList>
            <aka>
              <category>strong|weak</category>
              <firstName>...</firstName>
              <lastName>...</lastName>
            </aka>
          </akaList>
        </sdnEntry>
      </sdnList>

    Treasury's namespace varies between exports; we strip namespaces before
    matching tag names so the parser doesn't break on namespace renames.
    """
    # Treasury SDN.XML is published bytes-for-bytes by Treasury; this is not
    # untrusted user input. defusedxml would be belt-and-braces; the stdlib
    # parser is sufficient for a known-source feed.
    import xml.etree.ElementTree as ElementTree

    try:
        root = ElementTree.fromstring(xml_bytes)  # noqa: S314
    except ElementTree.ParseError as exc:
        raise OFACFetchError(f"SDN.XML parse failed: {exc}") from exc

    entries: list[SDNEntry] = []
    for entry in _iterfind(root, "sdnEntry"):
        primary = _full_name(entry)
        if not primary:
            continue
        aliases: list[str] = []
        for aka in _iterfind(entry, "aka"):
            alias = _full_name(aka)
            if alias and alias != primary:
                aliases.append(alias)
        entries.append(SDNEntry(primary_name=primary, aliases=tuple(aliases)))
    return entries


def _local_tag(elem: object) -> str:
    """ElementTree tag with namespace stripped."""
    tag = getattr(elem, "tag", "") or ""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _iterfind(parent: object, tag_name: str) -> Iterator[object]:
    """Yield direct children whose local tag matches `tag_name` (any descendant depth)."""
    iter_fn = getattr(parent, "iter", None)
    if iter_fn is None:
        return
    for child in iter_fn():
        if _local_tag(child) == tag_name:
            yield child


def _child_text(parent: object, tag_name: str) -> str:
    for child in _iterfind(parent, tag_name):
        text = getattr(child, "text", None)
        if text:
            return str(text).strip()
        return ""
    return ""


def _full_name(entry: object) -> str:
    first = _child_text(entry, "firstName")
    last = _child_text(entry, "lastName")
    if first and last:
        # SDN canonical form. Token-set matching makes order irrelevant
        # but storing this way matches Treasury's printed exports.
        return f"{last}, {first}"
    return last or first


__all__ = [
    "HARD_CUTOFF",
    "REFRESH_WINDOW",
    "OFACClient",
    "OFACError",
    "OFACFetchError",
    "OFACStaleError",
    "SDNEntry",
]
