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

# Sources (no auth, no rate limits as of 2026-06).
SDN_URL: Final[str] = "https://ofac.treasury.gov/SDN.XML"
CONSOLIDATED_URL: Final[str] = "https://ofac.treasury.gov/consolidated/consolidated.xml"
OPENSANCTIONS_URL: Final[str] = (
    "https://data.opensanctions.org/datasets/latest/us_ofac_sdn/entities.ftm.json"
)

# OFAC XML uses a default namespace prefix on every element.
_SDN_NS: Final[dict[str, str]] = {"s": "http://tempuri.org/sdnList.xsd"}

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


def _parse_ofac_xml(xml_bytes: bytes, list_name: str) -> list[OFACEntry]:
    """Parse SDN-format XML into OFACEntry list.

    Both SDN.XML and consolidated.xml share the same schema (sdnList).
    """
    entries: list[OFACEntry] = []
    try:
        # nosec B314 — the XML source is ofac.treasury.gov over HTTPS.
        # Not user-controlled; the ET.ParseError catch below covers the
        # malformed-input case. defusedxml is overkill for a known-good
        # government endpoint.
        root = ET.fromstring(xml_bytes)  # noqa: S314
    except ET.ParseError as exc:
        print(f"XML_PARSE_FAILED list={list_name} error={exc}", file=sys.stderr)
        return entries

    # Tags carry the namespace prefix; iterate findall with the ns map.
    for entry in root.findall("s:sdnEntry", _SDN_NS):
        uid_elem = entry.find("s:uid", _SDN_NS)
        if uid_elem is None or not uid_elem.text:
            continue
        uid = f"{list_name}:{uid_elem.text.strip()}"

        # Individual vs entity. Individuals use firstName / lastName;
        # entities use a single "name" pseudo-field via lastName.
        sdn_type = entry.findtext("s:sdnType", default="", namespaces=_SDN_NS).strip()
        first = entry.findtext("s:firstName", default="", namespaces=_SDN_NS).strip()
        last = entry.findtext("s:lastName", default="", namespaces=_SDN_NS).strip()
        if sdn_type.lower() == "individual":
            name = " ".join(p for p in (first, last) if p)
        else:
            name = last or first

        if not name:
            continue

        # Aliases (akaList → aka)
        aliases: list[str] = []
        aka_list = entry.find("s:akaList", _SDN_NS)
        if aka_list is not None:
            for aka in aka_list.findall("s:aka", _SDN_NS):
                f = aka.findtext("s:firstName", default="", namespaces=_SDN_NS).strip()
                lt = aka.findtext("s:lastName", default="", namespaces=_SDN_NS).strip()
                aka_name = " ".join(p for p in (f, lt) if p) if f else lt
                if aka_name and aka_name not in aliases:
                    aliases.append(aka_name)

        programs: list[str] = []
        prog_list = entry.find("s:programList", _SDN_NS)
        if prog_list is not None:
            for p in prog_list.findall("s:program", _SDN_NS):
                if p.text:
                    programs.append(p.text.strip())

        remarks = entry.findtext("s:remarks", default="", namespaces=_SDN_NS).strip()

        entries.append(
            OFACEntry(
                uid=uid,
                name=name,
                aliases=tuple(aliases),
                list_name=list_name,
                entry_type=sdn_type.lower() or "entity",
                program=",".join(programs),
                remarks=remarks,
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
        entries.append(
            OFACEntry(
                uid=uid,
                name=primary,
                aliases=aliases,
                list_name="opensanctions_us_ofac_sdn",
                entry_type=str(obj.get("schema", "")).lower() or "entity",
                program=program,
                remarks="",
            )
        )
    return entries


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
    if cons_bytes:
        cons_entries = _parse_ofac_xml(cons_bytes, "consolidated")
        if cons_entries:
            all_entries.extend(cons_entries)
            lists_seen.append("consolidated")
            any_succeeded = True
        else:
            all_entries.extend(_existing_entries_by_list(cache_path, "consolidated"))
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
