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
from dataclasses import dataclass
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
        state="CO",
        # Socrata SODA API. Canonical working endpoint as of 2026-06-28;
        # paginates cleanly through ~3M historical + active rows. Field
        # name is ``entityname`` (one word) — handled by
        # ``_socrata_row_to_canonical``.
        url="https://data.colorado.gov/resource/4ykn-tg5h.json",
        format="socrata_json",
    ),
    StateSource(
        state="OR",
        # 2026-06-28 — refreshed dataset id ``tckn-sxa6`` ("Active
        # Businesses - ALL") replaces the retired ``bhk3-b7qd``.
        # Confirmed live via data.oregon.gov catalog search +
        # /resource/ probe (returns ``business_name`` field).
        url="https://data.oregon.gov/resource/tckn-sxa6.json",
        format="socrata_json",
    ),
    StateSource(
        state="CT",
        # 2026-06-28 — refreshed dataset id ``n7gp-d28j`` ("Connecticut
        # Business Registry - Business Master") replaces the retired
        # ``w3e7-gxs7`` / ``n3p2-res3``. Returns ``name`` field —
        # canonicalised by ``_socrata_row_to_canonical``'s fallback chain.
        url="https://data.ct.gov/resource/n7gp-d28j.json",
        format="socrata_json",
    ),
    StateSource(
        state="MN",
        # 2026-06-28 — sos.state.mn.us 302-redirects to sos.mn.gov
        # (different subdomain). httpx follows the redirect with
        # ``follow_redirects=True``. The portal returns an HTML index;
        # ``_fetch_mn`` scrapes it for a direct CSV link.
        url="https://mblsportal.sos.mn.gov/Business/DownloadData",
        format="mn_csv",
        notes="MN ships CSV bulk downloads. Build script crawls the index page for CSV links.",
    ),
    StateSource(
        state="WY",
        url="https://wyobiz.wy.gov/Business/FilingSearch.aspx",
        format="wy_scrape",
        notes=(
            "HTML scrape (1 req/sec throttle). Opt-in via --include-wy. "
            "WY does not publish a bulk CSV/JSON export — the "
            "FilingSearch.aspx page is JS-rendered, so a true bulk "
            "ingest needs Playwright (or a paid bulk-data feed). Until "
            "that lands, WY merchant lookups fall through to the "
            "Bedrock web_search fallback."
        ),
    ),
    # ------------------------------------------------------------------
    # States with no working bulk endpoint as of 2026-06-28 — verified
    # individually. They fall through to the Bedrock web_search lookup
    # path in ``business_intel/sos_checker.py`` (which returns
    # ``data_source="bedrock_web_search"`` instead of
    # ``"sos_bulk_<state>"``):
    #
    #   * FL — re-verified 2026-06-28 from the prod box (network egress
    #     works there; localhost was 403'd by Sunbiz's bot manager). The
    #     public HTTPS portal at dos.fl.gov/sunbiz/other-services/
    #     data-downloads/ returns 0 downloadable corp/llc/entity links
    #     under any sensible User-Agent — the index page advertises the
    #     "Quarterly Data" tab but every direct .txt/.zip URL is gated
    #     behind sftp.floridados.gov (which serves an HTTPS login portal
    #     only — no anonymous bulk export). The legacy ftp://
    #     ftp.dos.state.fl.us host resets the TCP connection. A proper FL
    #     ingest needs a SUNBIZ SFTP licensed account; until then FL
    #     falls back to Bedrock web_search per
    #     business_intel/sos_checker.py (data_source=
    #     "bedrock_web_search").
    #   * IA — Iowa shut down their open-data portal's catalog API
    #     (data.iowa.gov/api/catalog/v1 → 404 across every search
    #     term); resource endpoints follow with 404. No Socrata
    #     replacement found.
    #   * NY — data.ny.gov publishes DOS *filing-history* datasets
    #     (e.g. ``63wc-4exh``, ``ekwr-p59j``) but no current
    #     active-entity master list. Filing rows would inflate the
    #     SQLite without giving us cleaner Jaro-Winkler matches
    #     than Bedrock can.
    #   * OH — Ohio SOS HTML pages return 403 for non-browser UAs
    #     (including the curl UA spoof). The bulk-export page is
    #     gated behind their bot-manager.
    # ------------------------------------------------------------------
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


# Batch size for the streaming SQLite writer. Bounds peak memory in
# ``_stream_state_into_conn`` to ``BATCH_SIZE * ~500 bytes`` ≈ 12 MB
# regardless of the source dataset's total row count. Larger values
# trade memory for fewer SQLite commits; 25 000 keeps a 4 GB box well
# clear of OOM (see 2026-06-27 incident: prior collect-then-write
# pattern hit 3.14 GB anon RSS on CO at ~2.85M rows and got killed).
BATCH_SIZE: Final[int] = 25_000


def main() -> int:
    args = _parse_args()
    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    requested_states = {s.upper() for s in args.states.split(",")} if args.states else None
    sources = [s for s in STATE_SOURCES if requested_states is None or s.state in requested_states]
    if not sources:
        log.error("No states matched --states %s", args.states)
        return 2

    if args.dry_run:
        outcomes: list[StateBuildOutcome] = []
        for src in sources:
            if src.state == "WY" and not args.include_wy:
                log.info("%s: skipping (HTML scrape, opt-in via --include-wy)", src.state)
                outcomes.append(StateBuildOutcome(state=src.state, skipped=True))
                continue
            outcome = _dry_run_count(src, args.max_rows_per_state)
            outcomes.append(outcome)
            log.info(
                "%s: %s",
                src.state,
                (
                    f"skipped ({outcome.error})"
                    if outcome.skipped
                    else f"dry-run {outcome.rows_inserted} rows in {outcome.seconds:.1f}s"
                ),
            )
        log.info("--dry-run: skipping SQLite write")
        return 0

    # Live build: open the tmp DB, stream every state in, then atomic
    # rename. Memory stays bounded by BATCH_SIZE rows.
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    if requested_states is not None and db_path.exists():
        import shutil

        shutil.copy2(db_path, tmp_path)

    outcomes = []
    with sqlite3.connect(tmp_path) as conn:
        conn.executescript(_SCHEMA_DDL)
        if requested_states is not None:
            placeholders = ",".join("?" * len(requested_states))
            conn.execute(
                f"DELETE FROM sos_entities WHERE state IN ({placeholders})",  # noqa: S608
                tuple(requested_states),
            )
            conn.commit()
        for src in sources:
            if src.state == "WY" and not args.include_wy:
                log.info("%s: skipping (HTML scrape, opt-in via --include-wy)", src.state)
                outcomes.append(StateBuildOutcome(state=src.state, skipped=True))
                continue
            outcome = _stream_state_into_conn(src, conn, args.max_rows_per_state)
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

    # fsync the tmp file then atomic rename so a crash mid-build never
    # leaves a half-populated SQLite at the canonical path.
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, db_path)
    total = sum(o.rows_inserted for o in outcomes)
    log.info("SQLite: %d rows written across %d states", total, len(outcomes))
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


def _iter_rows(src: StateSource, max_rows: int) -> Iterator[dict[str, str | None]]:
    """Dispatch to the right per-format fetcher. Each fetcher is a
    generator that yields canonical-shape dicts; callers stream the
    rows without buffering them into a list (see _stream_state_into_conn)."""
    if src.format == "socrata_json":
        return _fetch_socrata(src.url, max_rows)
    if src.format == "socrata_rows_json":
        return _fetch_socrata_rows_json(src.url, max_rows)
    if src.format == "fl_fixed_width":
        return _fetch_fl(src.url, max_rows)
    if src.format == "oh_bulk_csv_index":
        return _fetch_oh(src.url, max_rows)
    if src.format == "wy_scrape":
        return _fetch_wy(src.url, max_rows)
    if src.format == "mn_csv":
        return _fetch_mn(src.url, max_rows)
    raise ValueError(f"unknown format {src.format}")


def _stream_state_into_conn(
    src: StateSource,
    conn: sqlite3.Connection,
    max_rows: int,
    *,
    batch_size: int = BATCH_SIZE,
) -> StateBuildOutcome:
    """Stream rows from the per-state fetcher into the SQLite connection
    in fixed-size batches.

    Peak in-memory footprint is bounded by ``batch_size`` rows
    regardless of the source dataset's total row count — fixes the OOM
    that killed the 2026-06-27 CO build at offset 2.85M rows / 3.14 GB
    anon RSS on a 4 GB box (root cause: the prior collect-then-write
    pattern buffered every fetched row into ``outcome.rows`` before the
    write step).

    Commits per batch so even if the build crashes mid-state the rows
    persisted so far stay durable in the tmp file (the atomic rename at
    the end of main() decides whether the tmp file lands as the
    canonical DB).
    """
    start = time.monotonic()
    outcome = StateBuildOutcome(state=src.state)
    batch: list[tuple[str | None, ...]] = []
    try:
        for row in _iter_rows(src, max_rows):
            if not row.get("business_name"):
                continue
            batch.append(_row_to_insert_tuple(row, src.state))
            if len(batch) >= batch_size:
                conn.executemany(_INSERT_SQL, batch)
                conn.commit()
                outcome.rows_inserted += len(batch)
                batch.clear()
        if batch:
            conn.executemany(_INSERT_SQL, batch)
            conn.commit()
            outcome.rows_inserted += len(batch)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        log.warning("%s: build failed: %s", src.state, exc)
        outcome.skipped = True
        outcome.error = str(exc)
    finally:
        outcome.seconds = time.monotonic() - start
    return outcome


def _dry_run_count(src: StateSource, max_rows: int) -> StateBuildOutcome:
    """``--dry-run`` mode: walk the fetcher, count rows, never touch SQLite.
    Same fail-soft handling as the live path."""
    start = time.monotonic()
    outcome = StateBuildOutcome(state=src.state)
    try:
        for row in _iter_rows(src, max_rows):
            if row.get("business_name"):
                outcome.rows_inserted += 1
    except (httpx.HTTPError, OSError, ValueError) as exc:
        log.warning("%s: dry-run failed: %s", src.state, exc)
        outcome.skipped = True
        outcome.error = str(exc)
    outcome.seconds = time.monotonic() - start
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
# SQLite write — schema + per-batch INSERT, used by _stream_state_into_conn.
# The atomic-rename ceremony (tmp → fsync → rename) lives in ``main`` now
# so a single connection spans every state and per-batch commits keep
# memory bounded; ``_write_atomic`` was retired in the 2026-06-28 streaming
# rewrite (collect-then-write hit OOM on CO at 3 GB anon RSS).
# ---------------------------------------------------------------------------

_INSERT_SQL: Final[str] = """
INSERT INTO sos_entities (
    business_name, business_name_normalized, state, status,
    entity_type, formation_date, registered_agent,
    principal_address, officer_names, data_source, last_updated
) VALUES (?,?,?,?,?,?,?,?,?,?,?)
"""


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
