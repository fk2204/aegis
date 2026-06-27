"""Settings + data-residency boot guard.

The app refuses to start unless:
  - AEGIS_DATA_RESIDENCY_CONFIRMED is true (acknowledges US-only routing)
  - BEDROCK_MODEL_ID begins with "us." (regional inference profile, never "global.")

Both are gates enforced at first import. A failure here means a misconfigured
environment routed bank statements outside the US — that must never silently boot.
"""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

if TYPE_CHECKING:
    from aegis.audit import AuditLog

# Module-level latch so the Zoho-residue boot warning fires exactly once
# per process. Toggled by ``warn_if_zoho_env_lingers``; safe to call
# multiple times.
_zoho_residue_warning_emitted = False


class DataResidencyError(RuntimeError):
    """Raised when residency invariants are violated. Refuses to boot."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Data residency
    aegis_data_residency_confirmed: bool = False

    # AWS Bedrock
    aws_region: str = "us-east-1"
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    bedrock_model_id: str = "us.anthropic.claude-sonnet-4-6"

    # Supabase
    supabase_url: str = ""
    supabase_service_key: SecretStr | None = None

    # Close CRM. API key auth — HTTP Basic with the key as username
    # and blank password. Replaced the legacy Zoho integration in the
    # close-integration branch (steps 1-12).
    close_api_key: SecretStr | None = None
    close_api_base: str = "https://api.close.com"
    # Webhook secret (hex-encoded). Returned in the subscription POST
    # response as `signature_key`. Used for HMAC-SHA256 over
    # `close-sig-timestamp + raw_body`.
    close_webhook_secret: SecretStr | None = None
    # Opportunity status id that triggers AEGIS underwriting. The default
    # is the "Docs In — Pre-UW" status in the Commera Sales pipeline,
    # verified 2026-05-20 via the Close MCP. If the operator renames or
    # replaces that status, override via env.
    close_docs_in_pre_uw_status_id: str = "stat_1YZuVqdPWC8HLjWWvnXqL3NBJUPSjw3upy9mdBYXRqI"
    # Sprint 4 F3 — Close lifecycle audit + submission sync. When the
    # webhook reports an opportunity status change INTO one of these
    # IDs, ``funder_note_submissions`` for the merchant get auto-synced
    # (Funded -> approved, Dead - Lender -> declined). Both IDs verified
    # 2026-06-15 via the Close MCP against the live Sales pipeline.
    # Other terminal statuses (Dead - Merchant, Dead - UW Fail) audit
    # the transition but do NOT touch submissions because they reflect
    # internal kills / merchant walk-aways, not funder decisions.
    close_funded_status_id: str = "stat_OXb0lwLgcuUwNqtm7S9FjdJxRIFtTRCbFdNZUURxcwh"
    close_dead_lender_status_id: str = "stat_jnyp9hrSneIA2b5z52Cj9EE9C98mEtlslfOlWzw7UTw"

    # Filename-substring filters for auto-flowing Close attachments
    # through the parser. Case-insensitive substring match against the
    # attachment filename in ``aegis.workers.process_close_attachments``.
    # Non-matching attachments are audited as ``close.attachment.skipped``
    # and never reach the parser. Override via comma-separated env var,
    # e.g. CLOSE_ATTACHMENT_FILENAME_FILTERS=statement,estmt,stmt,bank,monthly.
    close_attachment_filename_filters: Annotated[tuple[str, ...], NoDecode] = (
        "statement",
        "estmt",
        "stmt",
        "bank",
    )
    # Soft cap on attachments processed per orchestration run. Warn at
    # _warn_threshold (audit row), hard-cap at _hard_cap unless the
    # rescan-with-override path is taken (chunk 5). Both protect against
    # an operator dropping the wrong folder and burning N Bedrock calls.
    close_attachment_warn_threshold: int = Field(default=10, ge=1, le=100)
    close_attachment_hard_cap: int = Field(default=15, ge=1, le=100)

    # Funder-reply webhook (mp Phase 10). HMAC-SHA256 over the raw body
    # with this secret. Missing -> the webhook returns 503 so an
    # accidental deploy without the secret can't silently fail open.
    funder_reply_webhook_secret: SecretStr | None = None

    # API auth
    api_bearer_token: SecretStr | None = None

    # Close → AEGIS callback router (/api/close-callback/*). Bearer auth,
    # same shape as ``require_bearer`` for the operator API but scoped to
    # a separate env var so the two surfaces rotate independently.
    # Operator-generated (``openssl rand -hex 32``), pasted into both
    # ``/etc/aegis/aegis.env`` and the Close-side trigger config (e.g. a
    # Workflow HTTP Request action's custom ``Authorization: Bearer ...``
    # header). Unset → every /api/close-callback/* request 503s
    # fail-closed via ``warn_if_close_callback_token_unconfigured``.
    close_callback_token: SecretStr | None = None

    # Redis (arq)
    redis_url: str = "redis://localhost:6379"

    # App
    app_port: int = Field(default=5555, ge=1, le=65535)
    log_level: str = "INFO"

    # Storage backend selector. "memory" = in-process dict (tests + offline);
    # "supabase" = Postgres via supabase-py (production default).
    aegis_storage_backend: Literal["memory", "supabase"] = "supabase"

    # Where uploaded PDFs land before the worker picks them up. The worker
    # deletes the file in a finally block — nothing here is long-lived.
    aegis_upload_dir: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "aegis-uploads"
    )

    # Hard cap on uploaded PDF size (per CLAUDE.md security rules).
    aegis_max_upload_bytes: int = Field(default=25 * 1024 * 1024, gt=0)

    # Hard cap on TOTAL bytes accepted in one multi-statement intake
    # request. Lets the operator drop 3-4 statements at once without
    # uncapping individual file size. Default 100 MB = 4 statements at
    # the per-file cap.
    aegis_max_intake_total_bytes: int = Field(default=100 * 1024 * 1024, gt=0)

    # OFAC SDN cache. Refresh window 24h, hard cutoff 7d (see scoring/ofac.py).
    # Cache file is created on first refresh; the parent dir is auto-mkdir'd
    # by the get_ofac_client dependency.
    aegis_ofac_cache_path: Path = Field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "aegis-ofac" / "sdn.json"
    )

    # Worker tuning. Lowered from 4 → 3 (2026-06-27) to keep parallelism
    # under the 4GB Hetzner box's RAM ceiling — Bedrock vision parses
    # cost ~600-900MB resident each at ~150-180s, and the worker shares
    # the box with aegis-web + Redis. 3 concurrent jobs leaves headroom
    # for the web process under simultaneous parse + dossier render.
    # arq's default is 10; AEGIS pins via this env-driven setting so a
    # higher-spec box can opt in without a code change.
    aegis_worker_max_concurrent: int = Field(default=3, ge=1, le=32)
    aegis_worker_job_timeout: int = Field(
        default=600, ge=30, description="seconds; longer than typical parse to allow LLM retries"
    )

    # Per-page parser routing (mp Phase 6.5). When True, the pipeline
    # classifies each page and routes text-bearing pages through the
    # text extractor / image pages through vision, instead of using
    # the legacy whole-doc strategy choice. Off by default until the
    # corpus + token-cost data validates the per-deal savings.
    aegis_parser_page_routing: bool = False

    # Weekly funder folder monitor (cron: every Monday 09:00 UTC).
    # When set, ``run_funder_monitor_cron`` walks this folder recursively,
    # SHA-256s every PDF/PNG, and runs the extract + merge pipeline on any
    # file whose hash doesn't match a funder's stored
    # ``guidelines_source_pdf_hash``. Unset on prod by design — the folder
    # lives on the operator's Windows OneDrive sync; the cron audits
    # ``funder_monitor.path_unavailable`` and returns 0 work when not
    # configured or unmounted, so prod ticks become free no-ops.
    aegis_funder_monitor_path: str | None = None

    # Tampering composition rule mode. The rule itself
    # (``aegis.parser.tampering.evaluate_tampering``) always runs and
    # always writes an audit row when it fires:
    #
    #   * ``"shadow"`` (default) — audit row ``tampering_would_decline``;
    #     ``tampering_confirmed`` stays False at score time. Lets the
    #     operator measure real true-positive / false-positive behavior
    #     on the live corpus before any applicant gets rejected by it.
    #
    #   * ``"live"`` — audit row ``tampering_decline_applied``; the
    #     multi_month builder re-evaluates from the persisted
    #     fraud_score_breakdown and sets ``tampering_confirmed=True``,
    #     which surfaces ``bank_statement_tampering_confirmed`` as a
    #     hard decline via ``score.py``.
    #
    # See docs/AEGIS_MASTER_PLAN.md §19 task 3 and the catalog at
    # docs/FRAUD_SIGNAL_CATALOG.md for the composition rule and the
    # operator's risk-policy decision (2026-06-04).
    aegis_tampering_decline_mode: Literal["shadow", "live"] = "shadow"

    # Step 2 scoring-engine cutover (audit finding B2). Switches the live
    # decline path between the legacy ``fraud_score >= 65`` rule (audit
    # §A.2 fix — aligned with parser ``HARD_DECLINE_THRESHOLD``) and the
    # A/B/C tracks (``scoring_v2``) without a code deploy. Per CLAUDE.md
    # "Decision-boundary changes — shadow-first": the flip itself is a
    # config / env var change, not a code deploy. Gated on (a) corpus
    # growth, (b) Track A historical lookback, (c) regression review of
    # every ``old-caught-something-new-misses`` row (see
    # ``scripts/triage_disagreement.py`` + ``docs/REMAINING_WORK.md``).
    #
    # ``"legacy"`` (default) — existing behavior. Track A/B/C run shadow
    # only via the dossier panel; ``fraud_score`` drives the live decline.
    #
    # ``"track_abc"`` — Step 2 cutover. ``score_deal`` consumes the Track A
    # integrity verdict and Track B band (passed in by the caller that
    # already computes them for the dossier) and uses them to drive the
    # live decline path. Track A ``fail`` and Track B ``high`` are
    # promoted to hard-decline reasons; Track A ``review`` and Track B
    # ``elevated`` annotate as soft concerns. Legacy ``fraud_score``
    # becomes informational (no longer fires the threshold rule).
    aegis_scoring_engine: Literal["legacy", "track_abc"] = Field(
        default="legacy",
        description=(
            "Active scoring engine. 'legacy' (default) keeps the existing "
            "fraud_score >= 65 hard-decline path. 'track_abc' flips Step 2 "
            "cutover — Track A integrity verdict + Track B band drive the "
            "live decline path; legacy fraud_score becomes informational. "
            "Set in /etc/aegis/aegis.env after corpus triage clears all "
            "regression-sentinel rows (per scripts/triage_disagreement.py)."
        ),
    )

    # EOF-marker hard-decline threshold (R4.6 reconciliation).
    #
    # The scorer hard-declines when ``deal.eof_markers > aegis_eof_threshold``.
    # Default ``1`` preserves the legacy behavior (decline at 2+ EOF markers)
    # so a config-flag rollout is byte-identical to today's scorer until the
    # operator explicitly lifts the threshold.
    #
    # Set ``AEGIS_EOF_THRESHOLD=2`` to align with the pipeline routing policy
    # at docs/AUDIT_2026_05_10.md line 46 — "2 EOFs → review, 3+ →
    # manual_review". Per CLAUDE.md "Decision-boundary changes —
    # shadow-first": the flip itself is a config / env var change, not a
    # code deploy. Validate on the corpus before flipping in prod.
    aegis_eof_threshold: int = Field(
        default=1,
        ge=1,
        le=10,
        description=(
            "EOF marker count above which score_deal hard-declines. "
            "Default 1 matches the legacy scorer behavior (declines at 2+). "
            "Set to 2 to lift policy in line with pipeline routing "
            "(3+ → manual_review per docs/AUDIT_2026_05_10.md line 46)."
        ),
    )

    # ------------------------------------------------------------------
    # PDF retention redesign (chunk A) — see docs/PDF_RETENTION_DESIGN.md
    # ------------------------------------------------------------------

    # Supabase Storage bucket name. Per-env separation: prod / staging /
    # dev each get their own bucket so a cross-env service-role lookup
    # can't reach the wrong corpus. Boot guard
    # (storage_objects.assert_bucket_private_at_startup) asserts the
    # bucket exists and is PRIVATE (service_role only).
    aegis_document_bucket: str = "documents"

    # Defense-in-depth header injected by cloudflared and verified by
    # require_tunnel_secret on every CF-authenticated route. Protects
    # against an attacker who got code execution as a non-root user on
    # the box but no read of /etc/aegis/aegis.env. base64-random-32-bytes;
    # rotation procedure in deploy/RUNBOOK.md.
    # Unset → require_tunnel_secret fails closed (every route 503s). See
    # chunk-C deployment ordering note in project-pdf-retention-redesign
    # memory: configure cloudflared FIRST, verify header arrives, THEN
    # enable require_tunnel_secret app-side.
    aegis_tunnel_shared_secret: SecretStr | None = None

    # Encryption-key versioning. PDF_ENCRYPTION_KEYS_CURRENT names the
    # version used for new writes. Old versions stay configured as long
    # as any documents row references them; rotation procedure in
    # docs/PDF_KEY_ROTATION.md.
    #
    # Set to None / 0 until chunk B deploys (no callers depend on it
    # before then). Once chunk B is live, the systemd unit + ops
    # runbook ensure the current-version key is configured at boot.
    pdf_encryption_keys_current: int | None = None

    # Versioned keys (base64-encoded, must decode to exactly 32 bytes).
    # Declared explicitly v1..v10 because pydantic-settings binds env
    # vars at class-definition time, not dynamically; supporting 10
    # rotations without a code change is enough headroom for v1 design.
    # The boot guard (crypto.validate_crypto_config_at_boot) verifies
    # that PDF_ENCRYPTION_KEYS_CURRENT points at a populated key that
    # decodes to exactly 32 bytes.
    pdf_encryption_key_v1: SecretStr | None = None
    pdf_encryption_key_v2: SecretStr | None = None
    pdf_encryption_key_v3: SecretStr | None = None
    pdf_encryption_key_v4: SecretStr | None = None
    pdf_encryption_key_v5: SecretStr | None = None
    pdf_encryption_key_v6: SecretStr | None = None
    pdf_encryption_key_v7: SecretStr | None = None
    pdf_encryption_key_v8: SecretStr | None = None
    pdf_encryption_key_v9: SecretStr | None = None
    pdf_encryption_key_v10: SecretStr | None = None

    @field_validator("bedrock_model_id")
    @classmethod
    def _model_must_be_regional_us(cls, v: str) -> str:
        if not v.startswith("us."):
            raise DataResidencyError(
                f"BEDROCK_MODEL_ID must start with 'us.' (regional US inference profile); "
                f"got {v!r}. Bank statements must not transit non-US regions."
            )
        return v

    @field_validator("close_attachment_filename_filters", mode="before")
    @classmethod
    def _split_csv_filename_filters(cls, v: object) -> object:
        """Accept either a CSV string (operator-friendly env var) or a
        native tuple/list. ``CLOSE_ATTACHMENT_FILENAME_FILTERS=statement,estmt``
        parses into ``("statement", "estmt")``. Whitespace + empty tokens
        are stripped. Pre-tuple/list inputs pass through untouched."""
        if isinstance(v, str):
            return tuple(token.strip() for token in v.split(",") if token.strip())
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings, run boot guard, cache. Raises DataResidencyError on violation."""
    settings = Settings()
    if not settings.aegis_data_residency_confirmed:
        raise DataResidencyError(
            "AEGIS_DATA_RESIDENCY_CONFIRMED must be true. "
            "Refusing to boot until US-only data routing is acknowledged."
        )
    # PDF retention chunk A — crypto key sanity check. No-op when
    # PDF_ENCRYPTION_KEYS_CURRENT is unset (chunk A ships before any
    # caller depends on a populated key); once chunk B deploys, the
    # systemd unit ensures the current-version key is configured at
    # boot and a misconfiguration here refuses to start.
    #
    # Lazy import — aegis.crypto imports get_settings() at module level
    # for _key_for_version lookups, so a top-level import here would
    # create a cycle.
    #
    # ``settings`` is passed EXPLICITLY because we're still inside the
    # first-time get_settings() call (the @lru_cache hasn't returned
    # yet); without the explicit arg validate_crypto_config_at_boot
    # would call get_settings() recursively → RecursionError.
    from aegis.crypto import validate_crypto_config_at_boot

    validate_crypto_config_at_boot(settings)
    return settings


def warn_if_zoho_env_lingers(audit: AuditLog | None = None) -> list[str]:
    """One-shot detection of ``ZOHO_*`` env vars remaining after the
    Close cutover.

    Returns the sorted list of detected variable NAMES (never values —
    a stray ``ZOHO_REFRESH_TOKEN`` is a secret and must not land in
    logs). Emits a structured WARN at ``config.zoho_residue_detected``
    and, when an ``AuditLog`` is injected, writes one audit row with the
    same action. The latch above keeps the warning at-most-once per
    process; subsequent calls return the same list silently.

    This is non-fatal: AEGIS still boots. The signal tells the operator
    to clean up ``/etc/aegis/aegis.env`` on Hetzner (see
    ``deploy/RUNBOOK.md`` § Close Migration Cutover) so the residue
    doesn't drift from the codebase state.
    """
    global _zoho_residue_warning_emitted

    residue = sorted(k for k in os.environ if k.startswith("ZOHO_"))
    if not residue:
        return []

    if _zoho_residue_warning_emitted:
        return residue
    _zoho_residue_warning_emitted = True

    # Lazy logger import — aegis.logger imports aegis.config, so a
    # module-level import here creates a cycle. Inside the function
    # body it's resolved by the time we run.
    from aegis.logger import get_logger

    log = get_logger(__name__)
    log.warning(
        "config.zoho_residue_detected env_vars=%s",
        residue,
    )
    if audit is not None:
        try:
            audit.record(
                actor="config",
                action="config.zoho_residue_detected",
                details={"env_vars": residue},
            )
        except Exception:
            # Best-effort: the logger warning above is the primary
            # signal. An audit-write failure here must not mask that.
            log.warning(
                "config.zoho_residue_audit_write_failed",
                exc_info=True,
            )
    return residue


def reset_zoho_residue_latch() -> None:
    """Reset the one-shot latch — test-only convenience so each test
    that exercises ``warn_if_zoho_env_lingers`` starts from a clean state.
    """
    global _zoho_residue_warning_emitted
    _zoho_residue_warning_emitted = False
