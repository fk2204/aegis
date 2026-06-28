"""One-shot Close re-sync for every AEGIS merchant carrying a close_lead_id.

Walks the ``merchants`` table for every row with a non-NULL
``close_lead_id``, re-fetches the Close Lead, and refreshes the
``stated_*`` application fields.

Two paths per merchant:

* **FINANCIAL-block refresh** — the Close Lead description has a
  structured ``FINANCIAL:`` block. The migration-087 pure-string parser
  (``aegis.close.field_map._parse_close_lead_description``) extracts the
  fields and writes them DIRECTLY to the live ``merchants.stated_*``
  columns. Same path the webhook uses today.

* **Description-parse staging fallback** — the Lead description has NO
  ``FINANCIAL:`` block or the FINANCIAL parser returned empty (newer
  labels, ``DEAL:`` blocks, free-form text). Runs the Bedrock-driven
  extractor (``aegis.close.description_extractor.extract_from_description``)
  and STAGES the result to ``merchants.stated_extracted_pending``.
  NEVER auto-promotes to the live ``stated_*`` columns — the operator
  confirms via the dossier.

Per CLAUDE.md "Extraction & automation assists, never replaces judgment":
the staging column is the operator-confirmation gate. Scoring never
reads it; only the confirmed live columns drive decisions.

Per AEGIS operating-principles §1, the default is DRY-RUN. ``--apply``
writes the merchant rows; ``--dry-run`` only prints what would be
written. Both modes hit Close (one ``GET /api/v1/lead/{id}/`` per
merchant) AND, when needed, Bedrock (one extraction call per merchant
on the fallback path).

Per AEGIS operating-principles §3, the script NEVER prints PII into
stdout / stderr. Counts, parse-status buckets, action verbs only —
no business names, no merchant ids tied to PII shapes, no description
bodies. The CSV / per-merchant artefact carries internal ids the
operator can cross-reference but stays gitignored.

Exit codes (aligned with sibling scripts):

  * ``0`` — every merchant processed cleanly.
  * ``1`` — runtime error (Supabase / Close / Bedrock init failed,
            settings missing, etc.).
  * ``3`` — at least one merchant failed (Close 404, Bedrock error,
            field-map error, etc.). Operator triage required via the
            stderr log.

Usage (on the prod box, with ``/etc/aegis/aegis.env`` sourced)::

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis

    # dry-run: classify every merchant; print bucket counts, no writes
    .venv/bin/python scripts/resync_close_leads.py --dry-run

    # apply: refresh FINANCIAL fields + stage description-parsed fallback
    .venv/bin/python scripts/resync_close_leads.py --apply
"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol
from uuid import UUID

from aegis.audit import AuditLog, SupabaseAuditLog
from aegis.close.client import CloseClient, CloseError
from aegis.close.description_extractor import (
    extract_from_description,
)
from aegis.close.field_map import _parse_close_lead_description
from aegis.config import get_settings
from aegis.logger import get_logger
from aegis.merchants.models import MerchantRow
from aegis.merchants.repository import SupabaseMerchantRepository

if TYPE_CHECKING:
    pass

# Exit codes — keep aligned with sibling scripts.
EXIT_OK: Final[int] = 0
EXIT_RUNTIME_ERROR: Final[int] = 1
EXIT_ISSUES_FOUND: Final[int] = 3

# Actor stamp used on audit rows.
_ACTOR: Final[str] = "resync_close_leads_script"

# Audit action strings — distinct so the operator can grep for one
# specific decision class.
_AUDIT_FINANCIAL_REFRESH: Final[str] = "close.resync.financial_block_refreshed"
_AUDIT_DESCRIPTION_STAGED: Final[str] = "close.resync.description_parsed_staged"
_AUDIT_SKIP_NO_DATA: Final[str] = "close.resync.skip_no_data"
_AUDIT_SKIP_EMPTY: Final[str] = "close.resync.skip_no_description"
_AUDIT_ERROR: Final[str] = "close.resync.error"

_log = get_logger(__name__)


# ----------------------------------------------------------------------
# Per-merchant outcome shape
# ----------------------------------------------------------------------


Bucket = Literal[
    "financial_block_refresh",  # FINANCIAL: parsed → live columns refreshed
    "description_parse_stage",  # description sent to Bedrock → staged blob
    "skip_no_description",  # Lead has no description body at all
    "skip_no_data",  # description present but extractor returned nothing
    "error",  # Close fetch / Bedrock / mapper raised
]


@dataclass(frozen=True)
class MerchantResyncOutcome:
    """Per-merchant resync result. Carries enough for the operator to
    triage from the CSV / stderr summary without leaking PII into the
    main stdout bucket roll-up.
    """

    merchant_id: UUID
    close_lead_id: str
    bucket: Bucket
    changed_field_count: int
    error: str = ""

    @property
    def is_error(self) -> bool:
        return self.bucket == "error"


# ----------------------------------------------------------------------
# Bedrock extractor Protocol — lets the dry-run path skip the live
# client construction when no merchant actually needs it.
# ----------------------------------------------------------------------


class _ExtractorProtocol(Protocol):
    def invoke_tool_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tool_name: str,
        tool_schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], str]: ...


# ----------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """CLI surface — dry-run / apply mutual flag plus an optional limit."""
    parser = argparse.ArgumentParser(
        description=(
            "Re-sync every AEGIS merchant with a close_lead_id against "
            "Close: refresh FINANCIAL-block stated_* columns OR stage a "
            "description-parsed fallback for operator confirmation."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply the writes (refresh live columns + stage extracted fallback).",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Default. Classify each merchant and print bucket counts; no DB writes.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Process at most N merchants (debug / smoke). Default: every "
            "merchant with a close_lead_id."
        ),
    )
    parser.add_argument(
        "--no-bedrock",
        action="store_true",
        help=(
            "Skip the description-parse fallback (Bedrock call). Useful "
            "to count buckets cheaply without paying for the extraction."
        ),
    )
    return parser.parse_args()


# ----------------------------------------------------------------------
# Dependency wiring
# ----------------------------------------------------------------------


def _load_dependencies(
    *,
    with_bedrock: bool,
) -> tuple[
    SupabaseMerchantRepository,
    AuditLog,
    CloseClient,
    _ExtractorProtocol | None,
]:
    """Build the prod-shape dependencies. Bedrock client is optional —
    skipped on ``--no-bedrock`` so a debug run doesn't fail on a missing
    aws_region setting when the operator just wants bucket counts."""
    # ``get_settings()`` triggers the data-residency boot guard. Even on
    # ``--no-bedrock`` we still call it so a misconfigured environment
    # fails loud before we touch Close.
    _ = get_settings()
    audit: AuditLog = SupabaseAuditLog()
    merchants_repo = SupabaseMerchantRepository()
    close_client = CloseClient(audit=audit)
    llm: _ExtractorProtocol | None = None
    if with_bedrock:
        # Lazy import so the script can run in --no-bedrock mode without
        # the AWS SDK init cost.
        from aegis.llm import BedrockClient

        llm = BedrockClient()
    return merchants_repo, audit, close_client, llm


# ----------------------------------------------------------------------
# Core per-merchant resync
# ----------------------------------------------------------------------


def _resync_one_merchant(
    *,
    merchant: MerchantRow,
    close_client: CloseClient,
    llm: _ExtractorProtocol | None,
    merchants_repo: SupabaseMerchantRepository,
    audit: AuditLog,
    apply_writes: bool,
) -> MerchantResyncOutcome:
    """Resync one merchant: classify, parse, write (or dry-run)."""
    close_lead_id = merchant.close_lead_id
    assert close_lead_id is not None  # caller filters

    # Step 1 — fetch the Lead. A 404 here means the Close lead was
    # deleted; surface as an error so the operator can decide whether
    # to soft-delete the merchant.
    try:
        lead = close_client.get_lead(close_lead_id)
    except CloseError as exc:
        if apply_writes:
            audit.record(
                actor=_ACTOR,
                action=_AUDIT_ERROR,
                subject_type="merchant",
                subject_id=merchant.id,
                details={
                    "close_lead_id": close_lead_id,
                    "stage": "fetch_lead",
                    "error": type(exc).__name__,
                    "message": str(exc)[:200],
                },
            )
        return MerchantResyncOutcome(
            merchant_id=merchant.id,
            close_lead_id=close_lead_id,
            bucket="error",
            changed_field_count=0,
            error=f"close_fetch: {type(exc).__name__}",
        )

    raw_description = lead.get("description")
    description_str = raw_description if isinstance(raw_description, str) else None

    # Step 2 — FINANCIAL block parse. The pure-string parser returns an
    # empty dict when the description has no FINANCIAL: header line.
    financial = _parse_close_lead_description(description_str)

    if financial:
        diff = _financial_diff(merchant, financial)
        if apply_writes and diff:
            updated = merchant.model_copy(update=diff)
            merchants_repo.upsert(updated)
            audit.record(
                actor=_ACTOR,
                action=_AUDIT_FINANCIAL_REFRESH,
                subject_type="merchant",
                subject_id=merchant.id,
                details={
                    "close_lead_id": close_lead_id,
                    "changed_keys": sorted(diff.keys()),
                },
            )
        return MerchantResyncOutcome(
            merchant_id=merchant.id,
            close_lead_id=close_lead_id,
            bucket="financial_block_refresh",
            changed_field_count=len(diff),
        )

    # Step 3 — no FINANCIAL block. Try the description-parse fallback
    # via Bedrock (when enabled).
    if description_str is None or not description_str.strip():
        if apply_writes:
            audit.record(
                actor=_ACTOR,
                action=_AUDIT_SKIP_EMPTY,
                subject_type="merchant",
                subject_id=merchant.id,
                details={"close_lead_id": close_lead_id},
            )
        return MerchantResyncOutcome(
            merchant_id=merchant.id,
            close_lead_id=close_lead_id,
            bucket="skip_no_description",
            changed_field_count=0,
        )

    if llm is None:
        # Bedrock disabled — count this in the description bucket but
        # don't write anything. Dry-run + --no-bedrock is the cheap
        # classifier mode.
        return MerchantResyncOutcome(
            merchant_id=merchant.id,
            close_lead_id=close_lead_id,
            bucket="description_parse_stage",
            changed_field_count=0,
        )

    try:
        payload = extract_from_description(description_str, llm_client=llm)
    except Exception as exc:
        if apply_writes:
            audit.record(
                actor=_ACTOR,
                action=_AUDIT_ERROR,
                subject_type="merchant",
                subject_id=merchant.id,
                details={
                    "close_lead_id": close_lead_id,
                    "stage": "extract_from_description",
                    "error": type(exc).__name__,
                    "message": str(exc)[:200],
                },
            )
        return MerchantResyncOutcome(
            merchant_id=merchant.id,
            close_lead_id=close_lead_id,
            bucket="error",
            changed_field_count=0,
            error=f"bedrock: {type(exc).__name__}",
        )

    if payload is None or not payload.fields:
        if apply_writes:
            audit.record(
                actor=_ACTOR,
                action=_AUDIT_SKIP_NO_DATA,
                subject_type="merchant",
                subject_id=merchant.id,
                details={
                    "close_lead_id": close_lead_id,
                    "source_chars": len(description_str.strip()),
                },
            )
        return MerchantResyncOutcome(
            merchant_id=merchant.id,
            close_lead_id=close_lead_id,
            bucket="skip_no_data",
            changed_field_count=0,
        )

    # Stage the payload as JSONB. ``model_dump(mode="json")`` returns
    # a JSON-safe dict (datetime → ISO string, ints stay ints, str stays
    # str). Supabase's postgrest serialiser will pass it through to the
    # ``jsonb`` column unchanged.
    staged = payload.model_dump(mode="json")

    if apply_writes:
        updated = merchant.model_copy(update={"stated_extracted_pending": staged})
        merchants_repo.upsert(updated)
        audit.record(
            actor=_ACTOR,
            action=_AUDIT_DESCRIPTION_STAGED,
            subject_type="merchant",
            subject_id=merchant.id,
            details={
                "close_lead_id": close_lead_id,
                "field_count": len(payload.fields),
                "field_names": sorted(payload.fields.keys()),
                "model_id": payload.model_id,
                "source_chars": payload.source_chars,
            },
        )

    return MerchantResyncOutcome(
        merchant_id=merchant.id,
        close_lead_id=close_lead_id,
        bucket="description_parse_stage",
        changed_field_count=len(payload.fields),
    )


# ----------------------------------------------------------------------
# FINANCIAL-block diff — only land changed columns to avoid noisy
# upserts when the merchant row already matches the parser output.
# ----------------------------------------------------------------------


# Field names the FINANCIAL-block parser populates. Mirrors the keys in
# ``_FINANCIAL_LABEL_TO_FIELD`` over in ``aegis.close.field_map``.
_FINANCIAL_FIELDS: Final[tuple[str, ...]] = (
    "requested_amount",
    "use_of_funds",
    "monthly_revenue",
    "avg_monthly_cc_sales",
    "stated_monthly_deposits",
    "stated_mca_positions",
    "stated_current_lenders",
    "stated_mca_balance",
    "stated_daily_payment",
    "stated_bank",
)


def _financial_diff(merchant: MerchantRow, parsed: dict[str, Any]) -> dict[str, Any]:
    """Return only the FINANCIAL fields whose parsed value differs from
    the merchant row's current value.

    A field absent from ``parsed`` is left untouched (the parser only
    surfaces fields that appeared in the description block). Empty
    list for ``stated_current_lenders`` IS a write — it represents
    "operator removed the lenders" — when the existing value is non-empty.
    """
    diff: dict[str, Any] = {}
    for field in _FINANCIAL_FIELDS:
        if field not in parsed:
            continue
        new_value = parsed[field]
        current = getattr(merchant, field, None)
        if new_value != current:
            diff[field] = new_value
    return diff


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------


def main() -> int:
    args = _parse_args()

    # Default mode is dry-run when neither flag is set; resolve up front
    # so the rest of the function can reason about a single bool.
    apply_writes = bool(args.apply)
    with_bedrock = not args.no_bedrock

    try:
        merchants_repo, audit, close_client, llm = _load_dependencies(
            with_bedrock=with_bedrock,
        )
    except Exception as exc:
        print(
            f"ERROR: could not initialise dependencies: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        return EXIT_RUNTIME_ERROR

    try:
        all_merchants = merchants_repo.list_all()
    except Exception as exc:
        print(
            f"ERROR: could not list merchants: {exc}",
            file=sys.stderr,
        )
        traceback.print_exc(file=sys.stderr)
        close_client.close()
        return EXIT_RUNTIME_ERROR

    candidates = [m for m in all_merchants if m.close_lead_id]
    if args.limit is not None:
        candidates = candidates[: args.limit]

    mode = "APPLY" if apply_writes else "DRY-RUN"
    bedrock_label = "+bedrock" if with_bedrock else "+no-bedrock"
    print(
        f"# mode={mode} {bedrock_label} merchants_with_close_lead_id={len(candidates)}",
        file=sys.stderr,
    )

    bucket_counter: Counter[str] = Counter()
    outcomes: list[MerchantResyncOutcome] = []

    try:
        for merchant in candidates:
            outcome = _resync_one_merchant(
                merchant=merchant,
                close_client=close_client,
                llm=llm,
                merchants_repo=merchants_repo,
                audit=audit,
                apply_writes=apply_writes,
            )
            outcomes.append(outcome)
            bucket_counter[outcome.bucket] += 1
            # One terse line per merchant — id only, no PII. Operator
            # cross-references the merchant_id back to a name in Close.
            print(
                f"merchant_id={outcome.merchant_id} "
                f"close_lead_id={outcome.close_lead_id} "
                f"bucket={outcome.bucket} "
                f"changed_fields={outcome.changed_field_count}"
                + (f" error={outcome.error}" if outcome.error else ""),
                file=sys.stderr,
            )
    finally:
        close_client.close()

    # Bucket roll-up — the operator-facing summary.
    print("", file=sys.stderr)
    print(f"# RESULT mode={mode} {bedrock_label}", file=sys.stderr)
    for bucket in (
        "financial_block_refresh",
        "description_parse_stage",
        "skip_no_description",
        "skip_no_data",
        "error",
    ):
        print(f"  {bucket}: {bucket_counter[bucket]}", file=sys.stderr)

    has_errors = any(o.is_error for o in outcomes)
    return EXIT_ISSUES_FOUND if has_errors else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
