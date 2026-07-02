"""Daily OFAC list refresh — downloads SDN + consolidated + OpenSanctions
mirror and merges into a unified local cache.

Run via systemd timer (`deploy/aegis-ofac-update.timer`, 05:00 UTC) or
manually:

    uv run python scripts/update_ofac_list.py [--cache-dir /var/lib/aegis/ofac_cache]

Cache layout
------------
``/var/lib/aegis/ofac_cache/ofac_unified.json`` — single merged file:

```
{
  "fetched_at": "2026-06-28T05:00:00+00:00",
  "lists_checked": ["sdn", "consolidated", "opensanctions_us_ofac_sdn"],
  "entries": [
    {
      "uid": "sdn:12345",
      "name": "CANONICAL NAME",
      "aliases": ["ALIAS1", "ALIAS2"],
      "list": "sdn",
      "type": "individual" | "entity",
      "program": "SDGT,UKRAINE-EO13662",
      "remarks": "<optional>"
    },
    ...
  ],
  "name_index": {
    "normalized name": ["uid1", "uid2", ...]
  }
}
```

The name index keys are ``normalize(name + alias)`` (uppercase, non-
alphanumeric → space, collapsed). The runtime checker uses Jaro-Winkler
against the keys; the keys alone serve only as a hash-bucket prefilter
when the corpus grows past a million names (not needed today — SDN +
consolidated is ~35K entities).

Fail posture
------------
- Each downloader runs independently. A single source failure preserves
  the previous merged cache (the source's entries from the prior fetch
  carry through) so a temporary OFAC outage doesn't blank the screener.
- All-three-fail with NO existing cache → exit non-zero so the systemd
  timer surfaces a failed unit; the runtime ``OFACChecker`` then fails
  closed (cache missing → block).
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx

# Sources (no auth, no rate limits as of 2026-06). Treasury migrated the
# direct XML downloads off ofac.treasury.gov to the Sanctions List
# Service API (2026-Q2); the new endpoints 302-redirect to per-request
# signed S3 URLs, which ``follow_redirects=True`` in ``_fetch`` handles
# transparently.
SDN_URL: Final[str] = (
    "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
)
# 2026-06-28 — Treasury renamed the consolidated export multiple times
# during the 2026-Q2 migration. The path that returns SDN-format XML
# (``<sdnList><sdnEntry>...`` — what ``_parse_ofac_xml`` walks) is
# CONSOLIDATED.XML. Confirmed via prod-box probe:
#   * CONS_ADV.XML        → 200 / 0-byte (Treasury still serves the
#     URL, just empty)
#   * CONS_ADVANCED.XML   → 200 / 4.1 MB but uses the new "Advanced
#     XML" schema (``<Sanctions><DistinctParty>...``) which our parser
#     does NOT understand — returned 0 entries
#   * CONSOLIDATED.XML    → 200 / sdnList format — the parser-compatible
#     one. Selected.
CONSOLIDATED_URL: Final[str] = (
    "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONSOLIDATED.XML"
)

# Hard floor for a successful consolidated fetch. CONS_ADVANCED.XML is
# ~4 MB; anything under 1 KB is a silent breakage (renamed endpoint,
# 200/empty body, etc) and must not overwrite the prior cache entries.
_CONSOLIDATED_MIN_BYTES: Final[int] = 1000
OPENSANCTIONS_URL: Final[str] = (
    "https://data.opensanctions.org/datasets/latest/us_ofac_sdn/entities.ftm.json"
)

# OFAC XML uses a default namespace prefix on every element. Treasury
# renamed the namespace when moving downloads off ofac.treasury.gov to
# the sanctionslistservice.ofac.treas.gov API in 2026-Q2 — the new
# namespace is https://sanctionslistservice.ofac.treas.gov/api/Publication
# Preview/exports/XML. Rather than hardcoding (and breaking again on the
# next rename), strip the namespace off every tag before matching —
# same pattern src/aegis/scoring/ofac.py uses for the runtime fetcher.

DEFAULT_CACHE_DIR: Final[Path] = Path("/var/lib/aegis/ofac_cache")
UNIFIED_CACHE_FILENAME: Final[str] = "ofac_unified.json"

_HTTP_TIMEOUT_SECONDS: Final[float] = 60.0


@dataclass(frozen=True)
class OFACEntry:
    uid: str
    name: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    list_name: str = ""
    entry_type: str = ""
    program: str = ""
    remarks: str = ""
    # Context-aware fields (added 2026-07). ``sdn_type`` mirrors the raw
    # ``<sdnType>`` element ("Individual" / "Entity" / "Vessel" /
    # "Aircraft"); ``programs`` is the list-of-strings shape (the
    # ``program`` field above stays as the comma-joined string for
    # backwards compatibility with old cache readers); ``countries``
    # is the deduped uppercase list of country codes / names from
    # ``<addressList><address><country>``. All three are consumed by
    # ``src/aegis/compliance/ofac.py`` for the context-aware threshold.
    sdn_type: str = ""
    programs: tuple[str, ...] = field(default_factory=tuple)
    countries: tuple[str, ...] = field(default_factory=tuple)


def _normalize_name(s: str) -> str:
    """Uppercase + non-alphanumeric → single space + collapse runs."""
    out: list[str] = []
    prev_space = True
    for ch in s.upper():
        if ch.isalnum():
            out.append(ch)
            prev_space = False
        else:
            if not prev_space:
                out.append(" ")
                prev_space = True
    return "".join(out).strip()


def _fetch(url: str) -> bytes | None:
    """Download a URL with a generous timeout. Returns bytes or None."""
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:
        print(f"FETCH_FAILED url={url} error={exc}", file=sys.stderr)
        return None


def _local_tag(elem: ET.Element) -> str:
    """ElementTree tag with namespace stripped — ``{ns}tag`` → ``tag``."""
    tag = elem.tag or ""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _children(elem: ET.Element, name: str) -> list[ET.Element]:
    """Direct children whose local name matches ``name``."""
    return [c for c in elem if _local_tag(c) == name]


def _child_text(elem: ET.Element, name: str) -> str:
    """Text of the first child whose local name matches ``name``, or ``''``."""
    for c in elem:
        if _local_tag(c) == name and c.text:
            return c.text.strip()
    return ""


def _parse_ofac_xml(xml_bytes: bytes, list_name: str) -> list[OFACEntry]:
    """Parse SDN-format XML into OFACEntry list.

    Both SDN.XML and CONS_ADV.XML share the same schema (sdnList). Tag
    matching strips the default namespace so the parser survives
    Treasury renaming the schema (2026-Q2 migration moved the namespace
    from tempuri.org/sdnList.xsd to sanctionslistservice.ofac.treas.gov).
    """
    entries: list[OFACEntry] = []
    try:
        # nosec B314 — the XML source is Treasury over HTTPS. Not
        # user-controlled; the ET.ParseError catch below covers the
        # malformed-input case. defusedxml is overkill for a known-good
        # government endpoint.
        root = ET.fromstring(xml_bytes)  # noqa: S314
    except ET.ParseError as exc:
        print(f"XML_PARSE_FAILED list={list_name} error={exc}", file=sys.stderr)
        return entries

    for entry in _children(root, "sdnEntry"):
        uid_text = _child_text(entry, "uid")
        if not uid_text:
            continue
        uid = f"{list_name}:{uid_text}"

        # Individual vs entity. Individuals use firstName / lastName;
        # entities use a single "name" pseudo-field via lastName.
        sdn_type = _child_text(entry, "sdnType")
        first = _child_text(entry, "firstName")
        last = _child_text(entry, "lastName")
        if sdn_type.lower() == "individual":
            name = " ".join(p for p in (first, last) if p)
        else:
            name = last or first

        if not name:
            continue

        # Aliases (akaList → aka)
        aliases: list[str] = []
        aka_lists = _children(entry, "akaList")
        for aka_list in aka_lists:
            for aka in _children(aka_list, "aka"):
                f = _child_text(aka, "firstName")
                lt = _child_text(aka, "lastName")
                aka_name = " ".join(p for p in (f, lt) if p) if f else lt
                if aka_name and aka_name not in aliases:
                    aliases.append(aka_name)

        programs: list[str] = []
        prog_lists = _children(entry, "programList")
        for prog_list in prog_lists:
            for p in _children(prog_list, "program"):
                if p.text:
                    programs.append(p.text.strip())

        remarks = _child_text(entry, "remarks")

        # Countries — collected from every <addressList><address><country>
        # element on the entry. Deduped case-insensitively; original
        # casing is preserved on the first occurrence so the runtime
        # can normalize per its own rules.
        countries: list[str] = []
        seen_countries: set[str] = set()
        addr_lists = _children(entry, "addressList")
        for addr_list in addr_lists:
            for addr in _children(addr_list, "address"):
                country = _child_text(addr, "country")
                if country and country.upper() not in seen_countries:
                    countries.append(country)
                    seen_countries.add(country.upper())

        entries.append(
            OFACEntry(
                uid=uid,
                name=name,
                aliases=tuple(aliases),
                list_name=list_name,
                entry_type=sdn_type.lower() or "entity",
                program=",".join(programs),
                remarks=remarks,
                sdn_type=sdn_type or "Entity",
                programs=tuple(programs),
                countries=tuple(countries),
            )
        )
    return entries


def _parse_opensanctions(json_bytes: bytes) -> list[OFACEntry]:
    """Parse OpenSanctions FTM JSON lines into OFACEntry list."""
    entries: list[OFACEntry] = []
    # FTM ships one JSON object per line.
    for line in json_bytes.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        uid = f"opensanctions:{obj.get('id', '')}"
        if uid == "opensanctions:":
            continue
        properties = obj.get("properties") or {}
        names: list[str] = []
        for key in ("name", "alias", "previousName"):
            val = properties.get(key)
            if isinstance(val, list):
                names.extend(str(v) for v in val if v)
            elif isinstance(val, str):
                names.append(val)
        if not names:
            continue
        primary = names[0]
        aliases = tuple(n for n in names[1:] if n and n != primary)
        topics = properties.get("topics") or []
        program = ",".join(str(t) for t in topics if t)
        program_list = tuple(str(t) for t in topics if t)
        # OpenSanctions FTM schema: "Person" / "Company" / "Organization" /
        # "Vessel" / "Airplane". Map to the OFAC-style vocabulary so the
        # runtime type-filter reads the same shape regardless of source.
        schema = str(obj.get("schema", "")).strip()
        sdn_type = _opensanctions_schema_to_sdn_type(schema)
        countries_raw = properties.get("country") or []
        countries: tuple[str, ...] = ()
        if isinstance(countries_raw, list):
            countries = tuple(str(c) for c in countries_raw if c)
        elif isinstance(countries_raw, str) and countries_raw:
            countries = (countries_raw,)
        entries.append(
            OFACEntry(
                uid=uid,
                name=primary,
                aliases=aliases,
                list_name="opensanctions_us_ofac_sdn",
                entry_type=schema.lower() or "entity",
                program=program,
                remarks="",
                sdn_type=sdn_type,
                programs=program_list,
                countries=countries,
            )
        )
    return entries


def _opensanctions_schema_to_sdn_type(schema: str) -> str:
    """Map an OpenSanctions FTM schema name to the OFAC-style sdnType.

    The runtime type-filter compares against the uppercased OFAC
    vocabulary (INDIVIDUAL / ENTITY / VESSEL / AIRCRAFT). OpenSanctions
    ships different labels; normalize here so both sources produce the
    same runtime shape.
    """
    s = schema.strip().lower()
    if s in {"person"}:
        return "Individual"
    if s in {"vessel", "ship"}:
        return "Vessel"
    if s in {"airplane", "aircraft"}:
        return "Aircraft"
    # Company / Organization / LegalEntity / default → Entity.
    return "Entity"


def _build_name_index(entries: list[OFACEntry]) -> dict[str, list[str]]:
    """Map normalized-name → list of UIDs that have that name or alias."""
    index: dict[str, list[str]] = {}
    for entry in entries:
        keys: set[str] = set()
        for raw in (entry.name, *entry.aliases):
            normalized = _normalize_name(raw)
            if normalized:
                keys.add(normalized)
        for key in keys:
            index.setdefault(key, []).append(entry.uid)
    return index


def _existing_entries_by_list(cache_path: Path, list_name: str) -> list[OFACEntry]:
    """Load entries for a single list from the prior cache (for fallback)."""
    if not cache_path.exists():
        return []
    try:
        prior = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        OFACEntry(
            uid=e["uid"],
            name=e["name"],
            aliases=tuple(e.get("aliases") or ()),
            list_name=e.get("list", ""),
            entry_type=e.get("type", ""),
            program=e.get("program", ""),
            remarks=e.get("remarks", ""),
            sdn_type=e.get("sdn_type", ""),
            programs=tuple(e.get("programs") or ()),
            countries=tuple(e.get("countries") or ()),
        )
        for e in (prior.get("entries") or [])
        if e.get("list") == list_name
    ]


def update_cache(cache_dir: Path) -> int:
    """Refresh the unified cache. Returns 0 on success, 1 on hard failure
    (all sources failed AND no prior cache exists)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / UNIFIED_CACHE_FILENAME

    all_entries: list[OFACEntry] = []
    lists_seen: list[str] = []
    any_succeeded = False

    # SDN
    sdn_bytes = _fetch(SDN_URL)
    if sdn_bytes:
        sdn_entries = _parse_ofac_xml(sdn_bytes, "sdn")
        if sdn_entries:
            all_entries.extend(sdn_entries)
            lists_seen.append("sdn")
            any_succeeded = True
        else:
            all_entries.extend(_existing_entries_by_list(cache_path, "sdn"))
    else:
        all_entries.extend(_existing_entries_by_list(cache_path, "sdn"))

    # Consolidated
    cons_bytes = _fetch(CONSOLIDATED_URL)
    if cons_bytes is None or len(cons_bytes) < _CONSOLIDATED_MIN_BYTES:
        # Silent-break guard: Treasury's renamed endpoints (e.g. the
        # 2026-Q2 CONS_ADV→CONS_ADVANCED rename) can return 200/empty
        # instead of 404. Treat a too-small body as failure so the prior
        # cache's consolidated entries carry through instead of being
        # overwritten with nothing. See bytes-floor rationale at
        # _CONSOLIDATED_MIN_BYTES.
        actual = 0 if cons_bytes is None else len(cons_bytes)
        print(
            f"CONS_ADV.too_small_response bytes={actual} url={CONSOLIDATED_URL}",
            file=sys.stderr,
        )
        all_entries.extend(_existing_entries_by_list(cache_path, "consolidated"))
    else:
        cons_entries = _parse_ofac_xml(cons_bytes, "consolidated")
        if cons_entries:
            all_entries.extend(cons_entries)
            lists_seen.append("consolidated")
            any_succeeded = True
        else:
            all_entries.extend(_existing_entries_by_list(cache_path, "consolidated"))

    # OpenSanctions
    os_bytes = _fetch(OPENSANCTIONS_URL)
    if os_bytes:
        os_entries = _parse_opensanctions(os_bytes)
        if os_entries:
            all_entries.extend(os_entries)
            lists_seen.append("opensanctions_us_ofac_sdn")
            any_succeeded = True
        else:
            all_entries.extend(_existing_entries_by_list(cache_path, "opensanctions_us_ofac_sdn"))
    else:
        all_entries.extend(_existing_entries_by_list(cache_path, "opensanctions_us_ofac_sdn"))

    if not any_succeeded and not cache_path.exists():
        print(
            "OFAC_UPDATE_FAILED all three sources failed and no prior cache exists",
            file=sys.stderr,
        )
        return 1

    name_index = _build_name_index(all_entries)

    payload = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "lists_checked": lists_seen,
        "entries": [
            {
                "uid": e.uid,
                "name": e.name,
                "aliases": list(e.aliases),
                "list": e.list_name,
                "type": e.entry_type,
                "program": e.program,
                "remarks": e.remarks,
                # Context-aware fields consumed by
                # ``src/aegis/compliance/ofac.py`` — safe defaults let
                # the runtime keep working when the field is absent
                # (old cache file); the reader falls back to ``ENTITY``
                # / empty program set / empty country set.
                "sdn_type": e.sdn_type,
                "programs": list(e.programs),
                "countries": list(e.countries),
            }
            for e in all_entries
        ],
        "name_index": name_index,
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    print(
        f"OFAC_UPDATE_OK entries={len(all_entries)} index_keys={len(name_index)} "
        f"sources={','.join(lists_seen)}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"Directory for ofac_unified.json (default: {DEFAULT_CACHE_DIR})",
    )
    args = parser.parse_args()
    return update_cache(args.cache_dir)


# Re-export so tests can build synthetic OFACEntry without importing
# private symbols.
__all__ = [
    "DEFAULT_CACHE_DIR",
    "UNIFIED_CACHE_FILENAME",
    "OFACEntry",
    "asdict",
    "main",
    "update_cache",
]


if __name__ == "__main__":
    sys.exit(main())
