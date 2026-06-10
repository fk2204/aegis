"""POD (probability-of-default) calibration scaffold — R4.5 cohort backtest.

Purpose
-------
Pull funded-deal outcomes from Close webhook history, bucket them by AEGIS
tier-at-funding, and produce a ``Tier → default_rate`` table. This is
RESEARCH SCAFFOLDING ONLY — per CLAUDE.md, no scoring-decision changes ship
today. The output is read-only insight that the operator inspects to
understand whether tier A really does default less than tier C in
practice. Once a year's worth of mature deals accumulates, the operator
re-runs this and decides whether to re-tune scoring (a separate,
shadow-first decision).

How outcome is resolved
-----------------------
Close does NOT expose a dedicated ``find_opportunities`` endpoint in the
local wrapper (``src/aegis/close/client.py`` only ships ``get_lead`` and
``get_opportunity``). The Lead object returned by ``GET /api/v1/lead/<id>/``
already embeds an ``opportunities`` array with each opportunity's current
``status_id``, ``status_label`` and ``status_type``. We resolve outcome by
walking that embedded array — no extra round-trip needed.

The outcome mapping is conservative: anything we cannot classify with
confidence stays ``unknown`` rather than being conflated with
"not defaulted". This matters because POD on a denominator that includes
unknown outcomes is meaningless.

Empty-corpus behavior
---------------------
Filip has zero mature funded deals today (2026-06-10 baseline — Commera
is a brand-new broker). The script must produce a structurally correct,
empty report without crashing. The cohort cut-off is "intake_date older
than 6 months", configurable via ``--min-age-days``.

Usage (from the production box)
-------------------------------
::

    ssh aegis@aegis-ssh.commerafunding.com 'cd /opt/aegis && bash -c "set -a; \
        source /etc/aegis/aegis.env; set +a; .venv/bin/python \
        scripts/cohort_backtest.py"'

The script reads ``MIGRATIONS_DB_URL_PROD`` (preferred) or ``DATABASE_URL``
for Supabase access and ``CLOSE_API_KEY`` for the Close client. Per
operating principle #1: production reads are OK without per-action
approval; this script NEVER writes to the DB or Close.

Output
------
* Tabular report to stdout
* ``reports/cohort_backtest_<YYYY-MM-DD>.json`` with the full dataset
  (gitignored — ``reports/`` covered in ``.gitignore``)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO_ROOT / "reports"

# Outcome literal — keep narrow and explicit. "unknown" is its own bucket;
# never collapse it into "not defaulted" (that would silently lower the
# default rate and is the cardinal POD-calibration sin).
Outcome = Literal[
    "funded_current",
    "funded_paid_off",
    "funded_defaulted",
    "funded_renewed",
    "not_funded",
    "unknown",
]

# Tiers exposed by ``ScoreResult.tier`` (see src/aegis/scoring/models.py).
# Listed explicitly so the report always emits all five buckets even
# when one is empty — empty buckets ARE informative ("we never funded
# a tier-A deal yet").
TIERS: tuple[str, ...] = ("A", "B", "C", "D", "F")

# Default minimum age before we consider a merchant "mature" for POD
# purposes. Six months is the floor the operator picked: most defaults
# happen in the first 90 days, so a 180-day window catches the bulk
# while not waiting forever to start the calibration loop.
DEFAULT_MIN_AGE_DAYS = 180

# Decimal precision for default_rate. Four places (0.3333) is the level
# the operator will read in a report — finer is noise on small N.
_RATE_QUANT = Decimal("0.0001")


# ---------------------------------------------------------------------
# Outcome resolution helpers
# ---------------------------------------------------------------------


# Close ``status_type`` is one of {"active", "won", "lost"}. The
# ``status_label`` is operator-configurable in the Close UI, so we
# match case-insensitively on substrings the operator has used in the
# Commera pipeline as of 2026-06-08 (verified via the Close MCP).
#
# Anything not in this map stays ``unknown`` — better to under-report
# defaults than to fabricate signals from labels we have not validated.
_DEFAULT_LABEL_SUBSTRINGS: tuple[str, ...] = ("default", "defaulted", "charged off")
_PAID_OFF_LABEL_SUBSTRINGS: tuple[str, ...] = ("paid off", "paid in full", "completed")
_RENEWED_LABEL_SUBSTRINGS: tuple[str, ...] = ("renewed", "renewal", "stacked")
_FUNDED_LABEL_SUBSTRINGS: tuple[str, ...] = ("funded", "active", "performing")


def _classify_opportunity_status(
    status_type: str | None, status_label: str | None
) -> Outcome:
    """Map one Close opportunity's status to an :class:`Outcome` literal.

    Conservative: any combination we cannot positively identify returns
    ``"unknown"``. The operator can extend the substring tables above
    once a new label is in production use AND validated against a known
    deal outcome.
    """
    label = (status_label or "").strip().lower()
    type_ = (status_type or "").strip().lower()

    if not label and not type_:
        return "unknown"

    # "lost" without a default-flavored label = not funded.
    if type_ == "lost" and not any(s in label for s in _DEFAULT_LABEL_SUBSTRINGS):
        return "not_funded"

    if any(s in label for s in _DEFAULT_LABEL_SUBSTRINGS):
        return "funded_defaulted"
    if any(s in label for s in _PAID_OFF_LABEL_SUBSTRINGS):
        return "funded_paid_off"
    if any(s in label for s in _RENEWED_LABEL_SUBSTRINGS):
        return "funded_renewed"
    if any(s in label for s in _FUNDED_LABEL_SUBSTRINGS):
        return "funded_current"

    # "won" with an unrecognized label is still ambiguous — could be
    # any of the funded outcomes. Mark unknown so it does NOT inflate
    # the "current" bucket.
    return "unknown"


def _resolve_close_outcome(
    close_lead_id: str | None, close_client: CloseLeadFetcher
) -> Outcome:
    """Resolve current outcome for one merchant via the embedded
    ``opportunities`` array on the Close Lead object.

    Returns the WORST observed outcome across all opportunities on the
    lead, where "worst" means: defaulted > renewed > paid_off > current
    > not_funded > unknown. Rationale: if a merchant has any defaulted
    opportunity in their history we want them counted as defaulted for
    POD purposes; the fact that an earlier deal paid off cleanly does
    not erase a later default.
    """
    if not close_lead_id:
        return "unknown"

    try:
        lead = close_client.get_lead(close_lead_id)
    except Exception:  # script must not crash on one bad lead
        return "unknown"

    opportunities = lead.get("opportunities") or []
    if not isinstance(opportunities, list):
        return "unknown"

    seen: set[Outcome] = set()
    for opp in opportunities:
        if not isinstance(opp, dict):
            continue
        status_type = opp.get("status_type")
        status_label = opp.get("status_label")
        # Both fields are str|None on the Close wire — coerce defensively
        # in case Close ever serves an int / bool.
        status_type_str = status_type if isinstance(status_type, str) else None
        status_label_str = status_label if isinstance(status_label, str) else None
        seen.add(_classify_opportunity_status(status_type_str, status_label_str))

    # Severity order — worst first. Typed explicitly so mypy can verify
    # each entry is a valid Outcome literal without a cast.
    severity_order: tuple[Outcome, ...] = (
        "funded_defaulted",
        "funded_renewed",
        "funded_paid_off",
        "funded_current",
        "not_funded",
    )
    for outcome in severity_order:
        if outcome in seen:
            return outcome
    return "unknown"


# ---------------------------------------------------------------------
# Tier statistics
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class CohortRow:
    """One merchant's cohort data point. Synthetic in tests, real in prod."""

    merchant_id: UUID
    tier: str
    outcome: Outcome
    score: int
    intake_date: date | None


@dataclass(frozen=True)
class TierStats:
    """Per-tier roll-up. ``default_rate`` is ``None`` when ``n_known == 0``
    so the report can distinguish "no data" from "0% default rate"."""

    tier: str
    n_total: int
    n_funded_current: int
    n_funded_paid_off: int
    n_funded_defaulted: int
    n_funded_renewed: int
    n_not_funded: int
    n_unknown: int
    # Default rate denominator is ``defaulted + paid_off + current + renewed``
    # — all merchants whose deal actually closed at the funder. Unknown
    # and not_funded stay out of the denominator so we report POD on
    # the population it actually describes.
    default_rate: Decimal | None


def _compute_default_rates(rows: Iterable[CohortRow]) -> dict[str, TierStats]:
    """Group ``rows`` by tier and roll up. Always emits an entry for
    every tier in :data:`TIERS`, even if the bucket is empty."""
    buckets: dict[str, list[CohortRow]] = {tier: [] for tier in TIERS}
    for row in rows:
        if row.tier in buckets:
            buckets[row.tier].append(row)
        # Tiers outside the published set are dropped silently. The
        # ScoreResult.tier Literal makes this impossible at type level,
        # but the cohort row can be hand-built (e.g. legacy audit data)
        # so the defense stays.

    stats: dict[str, TierStats] = {}
    for tier, tier_rows in buckets.items():
        counts: dict[Outcome, int] = {
            "funded_current": 0,
            "funded_paid_off": 0,
            "funded_defaulted": 0,
            "funded_renewed": 0,
            "not_funded": 0,
            "unknown": 0,
        }
        for row in tier_rows:
            counts[row.outcome] += 1

        n_funded_known = (
            counts["funded_current"]
            + counts["funded_paid_off"]
            + counts["funded_defaulted"]
            + counts["funded_renewed"]
        )
        if n_funded_known == 0:
            default_rate: Decimal | None = None
        else:
            default_rate = (
                Decimal(counts["funded_defaulted"]) / Decimal(n_funded_known)
            ).quantize(_RATE_QUANT, rounding=ROUND_HALF_UP)

        stats[tier] = TierStats(
            tier=tier,
            n_total=len(tier_rows),
            n_funded_current=counts["funded_current"],
            n_funded_paid_off=counts["funded_paid_off"],
            n_funded_defaulted=counts["funded_defaulted"],
            n_funded_renewed=counts["funded_renewed"],
            n_not_funded=counts["not_funded"],
            n_unknown=counts["unknown"],
            default_rate=default_rate,
        )
    return stats


def _format_report(stats: Mapping[str, TierStats]) -> str:
    """Plain-ASCII tabular report. No external dep on tabulate / rich —
    operator reads this over SSH, plain works everywhere."""
    header = (
        f"{'Tier':<6}{'N':>5}{'Funded':>9}{'PaidOff':>9}{'Default':>9}"
        f"{'Renewed':>9}{'NotFund':>9}{'Unknown':>9}{'DefaultRate':>14}"
    )
    lines = [header, "-" * len(header)]
    for tier in TIERS:
        s = stats[tier]
        rate_str = "—" if s.default_rate is None else f"{s.default_rate:.4f}"
        lines.append(
            f"{s.tier:<6}{s.n_total:>5}{s.n_funded_current:>9}"
            f"{s.n_funded_paid_off:>9}{s.n_funded_defaulted:>9}"
            f"{s.n_funded_renewed:>9}{s.n_not_funded:>9}{s.n_unknown:>9}"
            f"{rate_str:>14}"
        )
    return "\n".join(lines)


def _stats_to_json(stats: Mapping[str, TierStats]) -> dict[str, Any]:
    """Serialize ``TierStats`` to the JSON shape we persist in
    ``reports/cohort_backtest_<date>.json``. Decimal -> string so the
    file round-trips losslessly through any JSON tool."""
    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "tiers": {
            tier: {
                "tier": s.tier,
                "n_total": s.n_total,
                "n_funded_current": s.n_funded_current,
                "n_funded_paid_off": s.n_funded_paid_off,
                "n_funded_defaulted": s.n_funded_defaulted,
                "n_funded_renewed": s.n_funded_renewed,
                "n_not_funded": s.n_not_funded,
                "n_unknown": s.n_unknown,
                "default_rate": (
                    str(s.default_rate) if s.default_rate is not None else None
                ),
            }
            for tier, s in stats.items()
        },
    }


# ---------------------------------------------------------------------
# Data-source protocols (dependency injection seam for tests)
# ---------------------------------------------------------------------


class CohortDataSource(Protocol):
    """Reads mature merchants + their tier-at-funding from Supabase."""

    def fetch_mature_cohort(self, *, min_age_days: int) -> list[CohortInputRow]: ...


class CloseLeadFetcher(Protocol):
    """Subset of :class:`aegis.close.client.CloseClient` we use here."""

    def get_lead(self, lead_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CohortInputRow:
    """One row from the Supabase join of ``merchants`` + most-recent
    ``deal.score`` audit row. Distinct from :class:`CohortRow` (which
    carries the resolved outcome) so the DB read and the Close round-trip
    can be tested independently."""

    merchant_id: UUID
    close_lead_id: str | None
    tier: str
    score: int
    intake_date: date | None


# ---------------------------------------------------------------------
# Production data source (Supabase via psycopg)
# ---------------------------------------------------------------------


class _SupabaseCohortDataSource:
    """Production data source. Reads from ``merchants`` + ``audit_log``.

    Tier-at-funding is recovered from the LATEST ``deal.score`` audit row
    per merchant (subject_type='merchant', subject_id=merchant.id). The
    audit ``details`` JSONB carries ``tier``, ``score``, ``recommendation``
    — see ``src/aegis/api/routes/deals.py`` line ~296.

    The ``deal.score_with_matches`` action carries the same fields, so we
    union both. The window is "intake_date older than ``min_age_days``".
    Read-only — never writes.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def fetch_mature_cohort(self, *, min_age_days: int) -> list[CohortInputRow]:
        # Local import so unit tests that inject a fake data source do
        # NOT require psycopg to be installed (psycopg is in the dev
        # dep group on prod / dev boxes but not on every contributor's
        # workstation).
        import psycopg

        cutoff = date.today() - timedelta(days=min_age_days)
        sql = """
            WITH latest_score AS (
                SELECT DISTINCT ON (subject_id)
                    subject_id AS merchant_id,
                    details->>'tier'  AS tier,
                    (details->>'score')::int AS score,
                    created_at
                FROM audit_log
                WHERE action IN ('deal.score', 'deal.score_with_matches')
                  AND subject_type = 'merchant'
                  AND details ? 'tier'
                ORDER BY subject_id, created_at DESC
            )
            SELECT
                m.id,
                m.close_lead_id,
                ls.tier,
                ls.score,
                m.intake_date
            FROM merchants m
            JOIN latest_score ls ON ls.merchant_id = m.id
            WHERE m.intake_date IS NOT NULL
              AND m.intake_date <= %s
            ORDER BY m.intake_date
        """
        out: list[CohortInputRow] = []
        with psycopg.connect(self._dsn) as conn, conn.cursor() as cur:
            cur.execute(sql, (cutoff,))
            for row in cur.fetchall():
                merchant_id_raw, close_lead_id, tier, score, intake_date = row
                if tier is None or score is None:
                    continue
                if not isinstance(merchant_id_raw, UUID):
                    merchant_id_raw = UUID(str(merchant_id_raw))
                if intake_date is not None and not isinstance(intake_date, date):
                    intake_date = date.fromisoformat(str(intake_date))
                out.append(
                    CohortInputRow(
                        merchant_id=merchant_id_raw,
                        close_lead_id=(
                            close_lead_id if isinstance(close_lead_id, str) else None
                        ),
                        tier=str(tier),
                        score=int(score),
                        intake_date=intake_date,
                    )
                )
        return out


# ---------------------------------------------------------------------
# Entry point — pure function for testability
# ---------------------------------------------------------------------


def run(
    *,
    data_source: CohortDataSource,
    close_client: CloseLeadFetcher,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
) -> tuple[dict[str, TierStats], list[CohortRow]]:
    """Pull the cohort, resolve every outcome, compute tier stats.

    Returns ``(stats, rows)`` so the caller can both render the report
    and persist the full row-level dataset to the JSON report file.

    Pure with respect to its inputs — no env reads, no file IO, no time
    side-effects beyond what ``data_source`` and ``close_client`` do.
    Tests inject fakes for both and assert against the returns.
    """
    inputs = data_source.fetch_mature_cohort(min_age_days=min_age_days)
    rows: list[CohortRow] = []
    for inp in inputs:
        outcome = _resolve_close_outcome(inp.close_lead_id, close_client)
        rows.append(
            CohortRow(
                merchant_id=inp.merchant_id,
                tier=inp.tier,
                outcome=outcome,
                score=inp.score,
                intake_date=inp.intake_date,
            )
        )
    stats = _compute_default_rates(rows)
    return stats, rows


# ---------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------


def _load_dotenv() -> None:
    """Mirror :func:`scripts.audit_funders_table._load_dotenv` — load
    ``.env`` and ``.env.local`` without overwriting existing env."""
    for path in (REPO_ROOT / ".env", REPO_ROOT / ".env.local"):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _build_dsn() -> str:
    dsn = (
        os.environ.get("MIGRATIONS_DB_URL_PROD")
        or os.environ.get("DATABASE_URL")
        or ""
    )
    if not dsn:
        raise SystemExit(
            "ERROR: MIGRATIONS_DB_URL_PROD or DATABASE_URL must be set "
            "(see scripts/cohort_backtest.py docstring for the prod "
            "invocation)."
        )
    return dsn


def _build_close_client() -> CloseLeadFetcher:
    # Imported lazily so the test path never touches the real Close
    # client (which validates CLOSE_API_KEY at request time).
    from aegis.close.client import CloseClient

    return CloseClient()


def _write_report_json(stats: Mapping[str, TierStats]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"cohort_backtest_{date.today().isoformat()}.json"
    out_path.write_text(
        json.dumps(_stats_to_json(stats), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "R4.5 POD cohort backtest — bucket mature funded merchants by "
            "AEGIS tier-at-funding and report default_rate per tier. "
            "READ-ONLY against prod Supabase + Close."
        )
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=DEFAULT_MIN_AGE_DAYS,
        help=(
            f"Minimum days since intake_date for a merchant to be "
            f"considered mature (default {DEFAULT_MIN_AGE_DAYS})."
        ),
    )
    parser.add_argument(
        "--no-json",
        action="store_true",
        help="Skip the reports/cohort_backtest_<date>.json write.",
    )
    args = parser.parse_args(argv)

    _load_dotenv()
    dsn = _build_dsn()
    data_source = _SupabaseCohortDataSource(dsn)
    close_client = _build_close_client()

    stats, rows = run(
        data_source=data_source,
        close_client=close_client,
        min_age_days=args.min_age_days,
    )

    if not rows:
        print(
            "No mature merchants yet. Need >= 6mo old funded deals "
            "for POD calibration."
        )
        return 0

    print(_format_report(stats))
    if not args.no_json:
        out_path = _write_report_json(stats)
        print(f"\nWrote {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "TIERS",
    "CloseLeadFetcher",
    "CohortDataSource",
    "CohortInputRow",
    "CohortRow",
    "Outcome",
    "TierStats",
    "_classify_opportunity_status",
    "_compute_default_rates",
    "_format_report",
    "_load_dotenv",
    "_resolve_close_outcome",
    "_stats_to_json",
    "main",
    "run",
]
