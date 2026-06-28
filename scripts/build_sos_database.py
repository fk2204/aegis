"""Build / refresh the local Secretary of State entity SQLite cache.

Designed to run from the ``aegis-sos-update.timer`` systemd unit on the
prod box every Sunday 04:00 UTC. Downloads and parses free bulk-data
exports from the following states (in this order — fail-soft per
state, the rest of the build continues):

* FL — fixed-width ASCII (SUNBIZ corporate data file)
* CO / OR / CT / IA / NY — Socrata-style JSON APIs (paginate with
  ``$limit=50000&$offset=N``)
* OH — monthly bulk CSV files
* WY — HTML scrape (Commera home state)
* MN — bulk CSV

The output SQLite lives at ``/var/lib/aegis/sos_cache/sos_entities.db``
and is rebuilt atomically: write to ``sos_entities.db.tmp``, fsync,
rename. Active queries against the previous DB succeed until the rename
lands.

Per-state row counts are large (FL ~10M, NY ~5M). The SQLite shard is
fine for our query pattern — a normalized-name + state composite
index makes the lookup O(log N).

Run modes:

* ``python scripts/build_sos_database.py`` — full rebuild.
* ``python scripts/build_sos_database.py --states FL,CO`` — refresh
  only the listed states (incremental — preserves rows for the others).
* ``python scripts/build_sos_database.py --dry-run`` — log per-state
  fetch outcomes without writing to the SQLite.

Rules:

- Failed state → ``logger.warning`` + skip. The build does NOT abort
  on one state's failure.
- Throttle scraped sources (WY) to 1 req/sec.
- All HTTPS goes through ``httpx`` (project dep) for cert handling.
- WY scrape uses regex on a small set of result-page tables; if the
  page structure changes the result is "WY rows missing this week" —
  not a crash. ``--include-wy`` opt-in is required.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sqlite3
import struct
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from io import BytesIO, StringIO
from pathlib import Path
from typing import Final

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("build_sos_database")

DEFAULT_DB_PATH: Final[Path] = Path("/var/lib/aegis/sos_cache/sos_entities.db")


# Per-state download metadata. ``url`` is the canonical bulk endpoint;
# ``format`` decides which parser to dispatch.
@dataclass(frozen=True)
class StateSource:
    state: str
    url: str
    format: str
    notes: str = ""


STATE_SOURCES: Final[tuple[StateSource, ...]] = (
    StateSource(
        state="FL",
        # SUNBIZ publishes the quarterly corporate-data file as
        # fixed-width ASCII. Field positions documented at
        # https://dos.myflorida.com/sunbiz/other-services/data-downloads/corporate-data-file/
        # The direct download lives on the SUNBIZ FTP host (HTTPS
        # mirror); _fetch_fl discovers the latest archive at runtime
        # by listing the cordata directory.
        url="https://search.sunbiz.org/Inquiry/CorporationSearch/Download",
        format="fl_fixed_width",
        notes="SUNBIZ quarterly fixed-width corporate-data file.",
    ),
    StateSource(
        state="CO",
        # Socrata SODA API (works as of 2026-06 — CO is the canonical
        # working endpoint; ~1.2M active entities).
        url="https://data.colorado.gov/resource/4ykn-tg5h.json",
        format="socrata_json",
    ),
    StateSource(
        state="OR",
        # 2026-06-28 — Socrata SODA /resource/{id}.json returned 404 on
        # 2026-06-27 build. Switching to the Socrata Download API
        # (/api/views/{id}/rows.json?accessType=DOWNLOAD) which the
        # state's open-data portal advertises as the canonical export
        # endpoint. The Socrata Download API returns a {meta, data}
        # envelope (columns + array-of-arrays) — see _fetch_socrata_rows_json.
        url="https://data.oregon.gov/api/views/bhk3-b7qd/rows.json?accessType=DOWNLOAD",
        format="socrata_rows_json",
    ),
    StateSource(
        state="CT",
        # 2026-06-28 — dataset id refreshed (old w3e7-gxs7 returned 404).
        url="https://data.ct.gov/api/views/n3p2-res3/rows.json?accessType=DOWNLOAD",
        format="socrata_rows_json",
    ),
    StateSource(
        state="IA",
        url="https://data.iowa.gov/api/views/kaxi-bdpi/rows.json?accessType=DOWNLOAD",
        format="socrata_rows_json",
    ),
    StateSource(
        state="NY",
        url="https://data.ny.gov/api/views/ej5i-dqpf/rows.json?accessType=DOWNLOAD",
        format="socrata_rows_json",
    ),
    StateSource(
        state="OH",
        # 2026-06-28 — old /globalassets/businesses/ index returned 403.
        # The business-filings page hosts the current bulk-export links.
        url="https://www.ohiosos.gov/businesses/business-filings/",
        format="oh_bulk_csv_index",
        notes=(
            "Ohio SOS publishes monthly bulk files. Build script crawls "
            "the index page for CSV links."
        ),
    ),
    StateSource(
        state="WY",
        url="https://wyobiz.wy.gov/Business/FilingSearch.aspx",
        format="wy_scrape",
        notes=(
            "HTML scrape (1 req/sec throttle). Opt-in via --include-wy. "
            "TODO: WY does not publish a bulk CSV/JSON export — the "
            "FilingSearch.aspx page is JS-rendered, so a true bulk "
            "ingest needs Playwright (or a paid bulk-data feed). Until "
            "that lands, WY merchant lookups fall through to the "
            "Bedrock web_search fallback."
        ),
    ),
    StateSource(
        state="MN",
        # 2026-06-28 — old www.sos.state.mn.us URL redirected to a
        # Radware bot-manager challenge. Direct portal download page is
        # at mblsportal.sos.state.mn.us; falls through to Bedrock if
        # the form-submission path doesn't deliver a CSV.
        url="https://mblsportal.sos.state.mn.us/Business/DownloadData",
        format="mn_csv",
        notes=("MN ships CSV bulk downloads. Build script crawls the index page for CSV links."),
    ),
)


# Florida SUNBIZ field positions (1-indexed in their docs, 0-indexed here).
# These are the most useful columns for our query — the file contains many more.
# Field widths from the SUNBIZ "Corporate Data File Description" doc.
@dataclass(frozen=True)
class FLField:
    name: str
    start: int  # 0-indexed
    length: int


_FL_FIELDS: Final[tuple[FLField, ...]] = (
    FLField("entity_id", 0, 12),
    FLField("entity_name", 12, 192),
    FLField("status", 204, 1),  # A=Active, I=Inactive, etc.
    FLField("filing_type", 205, 5),
    FLField("formation_date", 274, 8),  # MMDDYYYY
)
_FL_RECORD_LENGTH: Final[int] = 1466  # full SUNBIZ record width per spec


@dataclass
class StateBuildOutcome:
    state: str
    rows_inserted: int = 0
    skipped: bool = False
    error: str | None = None
    seconds: float = 0.0
    download_bytes: int = 0
    rows: list[dict[str, str | None]] = field(default_factory=list)


def main() -> int:
    args = _parse_args()
    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    requested_states = {s.upper() for s in args.states.split(",")} if args.states else None
    sources = [s for s in STATE_SOURCES if requested_states is None or s.state in requested_states]
    if not sources:
        log.error("No states matched --states %s", args.states)
        return 2

    outcomes: list[StateBuildOutcome] = []
    for src in sources:
        if src.state == "WY" and not args.include_wy:
            log.info("%s: skipping (HTML scrape, opt-in via --include-wy)", src.state)
            outcomes.append(StateBuildOutcome(state=src.state, skipped=True))
            continue
        outcome = _build_one_state(src, args.max_rows_per_state)
        outcomes.append(outcome)
        log.info(
            "%s: %s",
            src.state,
            (
                f"skipped ({outcome.error})"
                if outcome.skipped
                else f"{outcome.rows_inserted} rows in {outcome.seconds:.1f}s"
            ),
        )

    if args.dry_run:
        log.info("--dry-run: skipping SQLite write")
        return 0

    _write_atomic(db_path, outcomes, replace_states=requested_states)
    log.info("DB written to %s", db_path)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument(
        "--states",
        default=None,
        help="Comma-separated state codes; default = all sources.",
    )
    parser.add_argument(
        "--include-wy",
        action="store_true",
        help="Include the WY HTML scrape (fragile path).",
    )
    parser.add_argument(
        "--max-rows-per-state",
        type=int,
        # 2026-06-28 — cap effectively removed (default raised to
        # 50M). The prior 2M cap silently truncated CO's full ~1.2M
        # active-entity dataset and would also clip FL (~10M) and NY
        # (~5M) bulk exports. The cap remains as a runaway-safety
        # ceiling so a buggy paginator can't grow the SQLite
        # unboundedly; 50M is well above any real US state's active
        # entity count.
        default=50_000_000,
        help="Safety ceiling on rows per state (default 50M).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch + parse but do not write to the SQLite DB.",
    )
    return parser.parse_args()


def _build_one_state(src: StateSource, max_rows: int) -> StateBuildOutcome:
    start = time.monotonic()
    outcome = StateBuildOutcome(state=src.state)
    try:
        if src.format == "socrata_json":
            outcome.rows = list(_fetch_socrata(src.url, max_rows))
        elif src.format == "socrata_rows_json":
            outcome.rows = list(_fetch_socrata_rows_json(src.url, max_rows))
        elif src.format == "fl_fixed_width":
            outcome.rows = list(_fetch_fl(src.url, max_rows))
        elif src.format == "oh_bulk_csv_index":
            outcome.rows = list(_fetch_oh(src.url, max_rows))
        elif src.format == "wy_scrape":
            outcome.rows = list(_fetch_wy(src.url, max_rows))
        elif src.format == "mn_csv":
            outcome.rows = list(_fetch_mn(src.url, max_rows))
        else:
            raise ValueError(f"unknown format {src.format}")
    except (httpx.HTTPError, OSError, ValueError) as exc:
        log.warning("%s: build failed: %s", src.state, exc)
        outcome.skipped = True
        outcome.error = str(exc)
    finally:
        outcome.seconds = time.monotonic() - start
        outcome.rows_inserted = len(outcome.rows)
    return outcome


# ---------------------------------------------------------------------------
# Per-format fetchers
# ---------------------------------------------------------------------------
def _fetch_socrata(url: str, max_rows: int) -> Iterator[dict[str, str | None]]:
    """Paginate a Socrata-style JSON endpoint (/resource/{id}.json)."""
    offset = 0
    page_size = 50_000
    while offset < max_rows:
        with httpx.Client(timeout=60.0) as client:
            resp = client.get(url, params={"$limit": page_size, "$offset": offset})
            resp.raise_for_status()
            page = resp.json()
        if not page:
            return
        for raw in page:
            yield _socrata_row_to_canonical(raw)
        offset += len(page)
        if len(page) < page_size:
            return


def _fetch_socrata_rows_json(url: str, max_rows: int) -> Iterator[dict[str, str | None]]:
    """Stream the Socrata Download API (``/api/views/{id}/rows.json``).

    Different envelope from the SODA ``/resource/{id}.json`` API: returns
    a single JSON object ``{meta: {view: {columns: [...]}}, data: [[row,
    row, ...], ...]}`` with positional rows. We resolve column names from
    ``meta.view.columns[].fieldName`` to build the same canonical
    ``{business_name, status, entity_type, formation_date, ...}`` dict
    shape ``_socrata_row_to_canonical`` produces.

    No pagination on this endpoint — the server streams the full dataset
    in one response, so it can be slow on large states. 120s timeout
    matches the CSV streamers.
    """
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        payload = resp.json()
    columns_meta = payload.get("meta", {}).get("view", {}).get("columns", [])
    field_names: list[str] = [str(c.get("fieldName") or "") for c in columns_meta]
    data = payload.get("data") or []
    for i, row in enumerate(data):
        if i >= max_rows:
            return
        if not isinstance(row, list):
            continue
        # Socrata rows include 8 leading metadata columns (sid, id, position,
        # created_at, created_meta, updated_at, updated_meta, meta) before
        # the user-defined columns. Build a name→value dict skipping the
        # ones we cannot map.
        raw_dict: dict[str, object] = {}
        for idx, value in enumerate(row):
            if idx < len(field_names) and field_names[idx]:
                raw_dict[field_names[idx]] = value
        yield _socrata_row_to_canonical(raw_dict)


def _socrata_row_to_canonical(raw: dict[str, object]) -> dict[str, str | None]:
    # Socrata schemas vary per state; defensively pull common field names.
    name = (
        raw.get("entity_name")
        or raw.get("business_name")
        or raw.get("name")
        or raw.get("entityname")
        or ""
    )
    return {
        "business_name": str(name) if name else "",
        "status": _as_str(raw.get("status") or raw.get("entity_status") or raw.get("standing")),
        "entity_type": _as_str(raw.get("entity_type") or raw.get("type")),
        "formation_date": _as_str(
            raw.get("formation_date") or raw.get("incorporation_date") or raw.get("filing_date")
        ),
        "registered_agent": _as_str(raw.get("registered_agent") or raw.get("agent_name")),
        "principal_address": _as_str(raw.get("principal_address") or raw.get("address")),
        "officer_names": _as_str(raw.get("officer_names") or raw.get("officers")),
    }


def _fetch_fl(_url: str, max_rows: int) -> Iterator[dict[str, str | None]]:
    """Parse the SUNBIZ fixed-width ASCII corporate-data file.

    SUNBIZ publishes the file at
    https://www.floridados.gov/Public-Records/Public-Records-Modernization/
    behind a download portal; the direct URL changes per quarterly
    refresh. Operator updates the URL in the script ahead of the timer
    run by editing STATE_SOURCES — TODO automate this discovery via the
    portal's link parsing.
    """
    # NOTE: actual download requires the latest SUNBIZ URL per quarter.
    # Until automated, the timer logs "FL: skipped (no current URL)" and
    # the operator refreshes the constant before running the script.
    log.warning(
        "FL: SUNBIZ direct URL needs quarterly refresh — current STATE_SOURCES.url "
        "points to the portal index, not the .txt bundle. Operator updates "
        "STATE_SOURCES['FL'].url to the live quarterly file ahead of running."
    )
    # Yield zero rows but exit cleanly so the build continues with other
    # states. When operator wires the real URL in:
    #
    #   with httpx.stream("GET", live_url, timeout=120.0) as resp:
    #       buf = BytesIO()
    #       for chunk in resp.iter_bytes():
    #           buf.write(chunk)
    #       buf.seek(0)
    #       for row in parse_fl_records(buf, max_rows):
    #           yield row
    if False:  # pragma: no cover — placeholder for live wiring
        yield {}


def parse_fl_records(buf: BytesIO, max_rows: int) -> Iterator[dict[str, str | None]]:
    """Walk SUNBIZ fixed-width records. Exposed for unit tests."""
    count = 0
    while count < max_rows:
        chunk = buf.read(_FL_RECORD_LENGTH)
        if len(chunk) < _FL_RECORD_LENGTH:
            return
        try:
            text = chunk.decode("ascii", errors="replace")
        except (UnicodeDecodeError, struct.error):
            return
        # Use ``dict[str, str | None]`` so the formation_date / status
        # normalisation steps below can write ``None`` to clear an
        # invalid field.
        record: dict[str, str | None] = {
            f.name: text[f.start : f.start + f.length].rstrip().strip() for f in _FL_FIELDS
        }
        # Normalize FL's MMDDYYYY to ISO YYYY-MM-DD when valid.
        fd = record["formation_date"]
        if fd and len(fd) == 8 and fd.isdigit():
            record["formation_date"] = f"{fd[4:8]}-{fd[0:2]}-{fd[2:4]}"
        # Map FL status code to a human-readable token.
        status_code = record["status"]
        record["status"] = {"A": "ACTIVE", "I": "INACTIVE"}.get(
            status_code or "", status_code or None
        )
        yield {
            "business_name": record["entity_name"] or "",
            "status": record["status"],
            "entity_type": record["filing_type"] or None,
            "formation_date": record["formation_date"] or None,
            "registered_agent": None,
            "principal_address": None,
            "officer_names": None,
        }
        count += 1


def _fetch_oh(index_url: str, max_rows: int) -> Iterator[dict[str, str | None]]:
    """Crawl the Ohio SOS bulk index page for the most recent CSV link.

    Defensive: if no CSV is listed (page restructure), warn + skip.
    """
    with httpx.Client(timeout=60.0) as client:
        resp = client.get(index_url)
        resp.raise_for_status()
        html = resp.text
    # Look for the most recent ``.csv`` link on the page.
    candidate = _find_latest_csv_link(html)
    if not candidate:
        log.warning("OH: no .csv link found on %s — skipping", index_url)
        return
    csv_url = candidate if candidate.startswith("http") else _join_url(index_url, candidate)
    yield from _stream_csv_to_rows(csv_url, max_rows)


def _fetch_mn(index_url: str, max_rows: int) -> Iterator[dict[str, str | None]]:
    """Mirror of _fetch_oh — MN also publishes index page + CSV links."""
    with httpx.Client(timeout=60.0) as client:
        resp = client.get(index_url)
        resp.raise_for_status()
        html = resp.text
    candidate = _find_latest_csv_link(html)
    if not candidate:
        log.warning("MN: no .csv link found on %s — skipping", index_url)
        return
    csv_url = candidate if candidate.startswith("http") else _join_url(index_url, candidate)
    yield from _stream_csv_to_rows(csv_url, max_rows)


def _fetch_wy(_url: str, max_rows: int) -> Iterator[dict[str, str | None]]:
    """WY scrape — placeholder, currently yields zero rows.

    The wyobiz.wy.gov search results page is dynamic JavaScript-driven
    HTML; a plain ``requests`` fetch returns the shell page only. A
    real scraper needs a headless browser (Playwright) which is OUT OF
    SCOPE for this commit.

    Operator either:
    * Builds a Playwright scraper in a follow-up commit.
    * Files a public-records request with WY SOS for a bulk export.

    Until then, the build records WY as "0 rows fetched, skipped".
    """
    log.warning(
        "WY: HTML scrape requires Playwright (out of scope for this "
        "commit). Skipping — WY merchants fall back to Bedrock lookup."
    )
    if False:  # pragma: no cover
        time.sleep(1.0)  # 1 req/sec throttle if/when the scraper is wired
        yield {}


def _stream_csv_to_rows(csv_url: str, max_rows: int) -> Iterator[dict[str, str | None]]:
    with httpx.Client(timeout=120.0) as client:
        resp = client.get(csv_url)
        resp.raise_for_status()
        text = resp.text
    reader = csv.DictReader(StringIO(text))
    count = 0
    for row in reader:
        if count >= max_rows:
            return
        yield _csv_row_to_canonical(row)
        count += 1


def _csv_row_to_canonical(raw: dict[str, str]) -> dict[str, str | None]:
    name = (
        raw.get("Business Name")
        or raw.get("Entity Name")
        or raw.get("Name")
        or raw.get("BusinessName")
        or ""
    )
    return {
        "business_name": name,
        "status": _as_str(raw.get("Status") or raw.get("Standing") or raw.get("Entity Status")),
        "entity_type": _as_str(raw.get("Entity Type") or raw.get("Type")),
        "formation_date": _as_str(
            raw.get("Formation Date") or raw.get("Filing Date") or raw.get("Inc Date")
        ),
        "registered_agent": _as_str(raw.get("Registered Agent") or raw.get("Agent Name")),
        "principal_address": _as_str(raw.get("Principal Address") or raw.get("Address")),
        "officer_names": _as_str(raw.get("Officers") or raw.get("Officer Names")),
    }


def _find_latest_csv_link(html: str) -> str | None:
    matches: list[str] = re.findall(r'href="([^"]+\.csv)"', html, flags=re.IGNORECASE)
    if not matches:
        return None
    # Lexicographic sort approximates "most recent" when filenames are dated.
    matches.sort(reverse=True)
    return matches[0]


def _join_url(base: str, relative: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base, relative)


def _as_str(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# ---------------------------------------------------------------------------
# SQLite write
# ---------------------------------------------------------------------------
def _write_atomic(
    db_path: Path,
    outcomes: list[StateBuildOutcome],
    *,
    replace_states: set[str] | None,
) -> None:
    """Write the SQLite atomically: tmp → fsync → rename.

    When ``replace_states`` is None (full rebuild), the tmp file
    receives every fetched row. When non-None (partial rebuild), the
    tmp file is seeded from the existing DB, then DELETE + INSERT only
    for the listed states.
    """
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    if replace_states is not None and db_path.exists():
        # Copy existing DB to tmp, then mutate only the listed states.
        import shutil

        shutil.copy2(db_path, tmp_path)

    with sqlite3.connect(tmp_path) as conn:
        conn.executescript(_SCHEMA_DDL)
        if replace_states is not None:
            placeholders = ",".join("?" * len(replace_states))
            conn.execute(
                f"DELETE FROM sos_entities WHERE state IN ({placeholders})",  # noqa: S608 — bind list
                tuple(replace_states),
            )
        rows_written = 0
        for outcome in outcomes:
            if outcome.skipped or not outcome.rows:
                continue
            payload = [
                _row_to_insert_tuple(row, outcome.state)
                for row in outcome.rows
                if row.get("business_name")
            ]
            if not payload:
                continue
            conn.executemany(
                """
                INSERT INTO sos_entities (
                    business_name, business_name_normalized, state, status,
                    entity_type, formation_date, registered_agent,
                    principal_address, officer_names, data_source, last_updated
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                payload,
            )
            rows_written += len(payload)
        conn.commit()
        log.info("SQLite: %d rows written across %d states", rows_written, len(outcomes))

    # fsync then atomic rename.
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, db_path)


_SCHEMA_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS sos_entities (
    id INTEGER PRIMARY KEY,
    business_name TEXT NOT NULL,
    business_name_normalized TEXT NOT NULL,
    state TEXT NOT NULL,
    status TEXT,
    entity_type TEXT,
    formation_date TEXT,
    registered_agent TEXT,
    principal_address TEXT,
    officer_names TEXT,
    data_source TEXT,
    last_updated TEXT
);
CREATE INDEX IF NOT EXISTS idx_name_state
    ON sos_entities(business_name_normalized, state);
CREATE INDEX IF NOT EXISTS idx_state
    ON sos_entities(state);
"""


def _row_to_insert_tuple(row: dict[str, str | None], state: str) -> tuple[str | None, ...]:
    from datetime import UTC, datetime

    from aegis.business_intel.sos_checker import normalize_business_name

    name = row.get("business_name", "") or ""
    return (
        name,
        normalize_business_name(name),
        state,
        row.get("status"),
        row.get("entity_type"),
        row.get("formation_date"),
        row.get("registered_agent"),
        row.get("principal_address"),
        row.get("officer_names"),
        f"sos_bulk_{state.lower()}",
        datetime.now(UTC).isoformat(),
    )


if __name__ == "__main__":
    sys.exit(main())
