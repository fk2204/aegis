"""End-to-end parser pipeline orchestrator.

Order of operations
-------------------
1. metadata    -> tampering signals (deterministic, pikepdf)
2. extract     -> raw transactions + printed summary (pass 1, LLM)
3. validate    -> deterministic gate (period tie-out + daily reconciliation
                  + source attribution). FAIL -> manual_review, no retry.
4. classify    -> per-transaction category + confidence (pass 2, LLM)
5. patterns    -> deterministic fraud detectors over classified rows
6. aggregate   -> deterministic metrics with full source attribution

``fraud_score`` is still computed and persisted for analytics / audit
(portfolio_analytics reads it, the dossier renders the breakdown), but
NO routing decision in this module reads it as of 2026-06-25 — the
parser-layer pre-screening gate now uses Track A integrity signals
exclusively. See ``TRACK_A_PRESCREEN_THRESHOLD`` and
``_track_a_prescreen_severity_sum`` below.

Hard-decline triggers
---------------------
- ``metadata.eof_markers > EOF_HARD_DECLINE`` — incremental save spam.
  Pre-extraction signal; runs independently of any score.
- ``Track A integrity pre-screen severity sum >=
  TRACK_A_PRESCREEN_THRESHOLD`` — when the [META] forensic-family
  signals (editor_detected, page_layer_anomaly,
  text_overlay_detected, creator_mismatch_detected,
  font_inconsistency_detected) sum to or above the threshold, the
  document routes to manual_review with reason
  ``track_a_prescreen_integrity_fail``. Replaces the prior
  ``fraud_score >= HARD_DECLINE_THRESHOLD`` /
  ``metadata.fraud_score >= METADATA_HARD_DECLINE`` gates so the parser
  pre-screen and the scoring engine no longer split-brain on which
  number gates the decline path (the live engine has been
  ``track_abc`` since 2026-06-23 — ``fraud_score`` was already
  informational at the scorer layer).
- ``confidence_failures`` — LLM signaled it couldn't classify
  accurately; labels are suspect and downstream patterns + aggregates
  inherit the noise.

EOF threshold
-------------
Originally `eof_markers > 1` auto-rejected any PDF with 2+ `%%EOF`
markers. Real-world testing on operator-supplied bank statements (Nov
2025 - May 2026, 3 banks) surfaced this as a false-positive factory:
legitimate online-banking exports routinely have 2 EOFs (the bank's
export tool writes one, the user's PDF viewer or browser re-saves and
appends another). The bar is now `EOF_HARD_DECLINE = 2`, so 3+ EOFs
still hard-fail (genuine incremental-save tampering) but 2 EOFs is
demoted to a `review` flag the operator can clear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final, Literal, cast

from aegis.bank_layouts import BankLayoutRepository, BankLayoutWriteError
from aegis.bank_layouts.auto_hints import (
    generate_hints_from_parse_result,
    merge_hints,
)
from aegis.config import get_settings
from aegis.llm import LLMClient
from aegis.logger import get_logger
from aegis.parser.aggregate import aggregate
from aegis.parser.classify import (
    avg_classification_confidence,
    classify_transactions,
    per_category_confidence,
)
from aegis.parser.extract import (
    ExtractionError,
    ExtractionPass1Result,
    extract_statement,
    extract_statement_per_page,
    extract_statement_via_vision,
)
from aegis.parser.fintech_banks import detect_fintech_bank
from aegis.parser.metadata import MetadataAnalysis, analyze_metadata
from aegis.parser.models import Aggregates, ClassifiedTransaction, ValidationResult
from aegis.parser.nsf_secondary import secondary_validate_nsf
from aegis.parser.page_router import (
    PageStrategyDecision,
    classify_pages,
    has_low_confidence,
    is_homogeneous,
    summarize,
)
from aegis.parser.patterns import Pattern, PatternAnalysis, analyze_patterns
from aegis.parser.processor.stripe_router import (
    processor_type_for_document as determine_processor_type,
)
from aegis.parser.tampering import TamperingEvaluation, evaluate_tampering
from aegis.parser.validate import validate_extraction

_log = get_logger(__name__)

# THE thresholds. Read from here, not from anywhere else. (TS had three.)
#
# Forensic-layer signals (2026-06-24+) — font_inconsistency_detected,
# creator_mismatch_detected, text_overlay_detected — contribute to
# metadata_score directly inside ``aegis.parser.metadata.analyze_metadata``
# (additive with the existing ``_font_inconsistency`` / ``_page_layer_anomaly``
# contributions). They therefore get weighted by FRAUD_WEIGHTS["metadata"]
# alongside every other metadata-class signal — no separate FRAUD_WEIGHTS
# key, because adding a fourth key would break the sum-to-1 invariant
# without restructuring ``_fraud_score``. The per-signal point values
# (+15 / +20 / +25) are documented next to each signal's wiring in
# ``metadata.py``.
FRAUD_WEIGHTS: Final[dict[str, float]] = {
    "metadata": 0.35,
    "math": 0.40,
    "patterns": 0.25,
    # Shadow-only carve-out: 0.0 weight makes the new bundle-scope
    # unreconciled-internal-transfer v2 detector (operator spec
    # 2026-06-24, ``patterns.detect_unreconciled_internal_transfers``,
    # ``Pattern.code == "unreconciled_internal_transfer_v2"``)
    # explicitly NON-CONTRIBUTING to ``fraud_score``. The sum-to-1
    # invariant above intentionally excludes 0.0 entries — they
    # document the shadow-mode discipline (CLAUDE.md "Decision-
    # boundary changes — shadow-first") in the same constant that
    # carries the live weights, so a future operator can flip the
    # detector to live by changing this value plus the wiring in
    # ``_fraud_score`` without grepping for a separate config.
    "shadow_unreconciled_internal_transfer_v2": 0.0,
    # Shadow-only carve-out (operator spec 2026-06-24, composite AI-
    # generated-statement detector ``forensic.ai_statement.detect_ai_generated_statement``,
    # ``Pattern.code == "ai_generated_statement"``). Same shadow-mode
    # discipline as the v2 unreconciled-transfer detector above —
    # explicit 0.0 entry documents the contract that this signal does
    # NOT contribute to ``fraud_score`` and is reviewed via the
    # ``[SHADOW] ai_generated_statement: ...`` entry in ``all_flags``.
    "shadow_ai_generated_statement": 0.0,
}
# Severity-sum threshold for the Track A integrity pre-screen. When the
# sum of severities of [META] family forensic flags
# (``editor_detected``, ``page_layer_anomaly``,
# ``text_overlay_detected``, ``creator_mismatch_detected``,
# ``font_inconsistency_detected``) meets or exceeds this value, the
# pipeline routes the document to ``manual_review`` with reason
# ``track_a_prescreen_integrity_fail`` (synthetic validation failure).
# Replaces the old ``HARD_DECLINE_THRESHOLD`` (which gated on the
# weighted ``fraud_score``) — the value 65 is preserved so existing
# downstream consumers that compare a recomputed legacy ``fraud_score``
# against this constant continue to behave identically. The pre-screen
# is a SEVERITY SUM, not a weighted average — every signal that fires
# is direct evidence of paste-over / editor-touch fraud, and adding
# multiple weak signals to clear the threshold matches the underwriting
# reality that two forensic hits beat one strong hit alone. The per-
# signal contributions live in ``_PRESCREEN_FLAG_SEVERITIES`` below
# (mirrored from the values ``parser.metadata`` adds to
# ``metadata.fraud_score`` so the two stay aligned).
TRACK_A_PRESCREEN_THRESHOLD: Final[int] = 65
# Backwards-compatible alias. ``HARD_DECLINE_THRESHOLD`` was the
# pre-2026-06-25 name when the gate compared against the weighted
# ``fraud_score``; existing consumers (legacy scoring engine,
# Track A historical lookback, portfolio analytics) still want the
# numeric value 65 as a stable score-comparison constant. Keep the
# alias so those callers don't need to import a new symbol and don't
# accidentally drift from the canonical value.
HARD_DECLINE_THRESHOLD: Final[int] = TRACK_A_PRESCREEN_THRESHOLD
REVIEW_THRESHOLD: Final[int] = 35

# Per-flag severities used by the Track A integrity pre-screen. Keys
# are the literal prefixes that ``parser.metadata`` and
# ``parser.pipeline`` emit on ``MetadataAnalysis.flags`` (without the
# ``[META] `` category prefix — the pre-screen reads the metadata
# object directly, before ``_collect_flags`` prefixes the entries).
# Values mirror the deltas those modules add to ``metadata.fraud_score``
# so the pre-screen sum tracks the metadata-score contribution exactly
# for these five families. Other [META] families that contribute to
# ``metadata.fraud_score`` (``incremental_saves``, ``page_size_inconsistency``,
# ``stripped_metadata``, ``personal_author``, the date-gap signals,
# ``xref_offset_mismatch``, the page-level ``font_inconsistency``) are
# INTENTIONALLY EXCLUDED — they're useful audit signals but the new
# Track A pre-screen scopes itself to the five forensic-family signals
# the redesign identified as the load-bearing integrity gate.
_PRESCREEN_FLAG_SEVERITIES: Final[tuple[tuple[str, int], ...]] = (
    # Hard editors (itext / pdflib / foxit phantompdf / etc.) — emitted
    # by ``parser.metadata`` with +35 contribution. Medium editors
    # (Adobe Acrobat / Preview / Word) share the ``editor_detected:``
    # prefix and contribute +15 to metadata.fraud_score. The pre-screen
    # uses the hard-editor value here so a hard-editor hit alone (35)
    # plus one strong forensic signal (text_overlay_detected +25 or
    # creator_mismatch_detected +20) crosses the 65 threshold. Medium
    # editors stay informational on their own; the operator can tune
    # this later by splitting the prefix match into hard vs medium
    # families if the corpus shows the gate is too aggressive.
    ("editor_detected:", 35),
    # Forensic layer #3 (text_overlay_detected) — +25 in metadata.py.
    # Strongest of the three deterministic forensic detectors;
    # direct evidence of content-stream manipulation.
    ("text_overlay_detected:", 25),
    # Forensic layer #2 (creator_mismatch_detected) — +20 in
    # parser.pipeline.run_pipeline (post-extraction because it needs
    # bank_name). Specific fingerprint: editing-tool family in
    # /Creator + bank doesn't match the known-good profile.
    ("creator_mismatch_detected:", 20),
    # Page-level layer anomaly (_page_layer_anomaly) — +15 in
    # metadata.py. Multiple /Contents streams on a subset of pages —
    # paste-over composition signature.
    ("page_layer_anomaly:", 15),
    # Forensic layer (row-level font_inconsistency_detected) — +15
    # in metadata.py. Row-level font/size mismatch across transaction
    # spans. Distinct from the page-level ``font_inconsistency:``
    # signal (which is intentionally NOT in this table).
    ("font_inconsistency_detected:", 15),
)

# EOF marker count above which a PDF is treated as genuinely tampered.
# 2 EOFs are normal for legit online-banking exports (bank writes one,
# viewer/browser re-save appends another). 3+ indicates real incremental
# save tampering.
EOF_HARD_DECLINE: Final[int] = 2

# Maximum page count for the OCR vision fallback. Vision tokens are
# ~5-8x text-extraction tokens; capping the page count bounds the
# Bedrock cost on accidentally-uploaded huge scans. Statements above
# this cap that lack a text layer land in manual_review with reason
# `ocr_oversize_image_pdf` so the operator can split or rescan them.
MAX_OCR_PAGES: Final[int] = 20

# Total non-whitespace chars across the first ``_TEXT_LAYER_PROBE_PAGES``
# pages required to send a doc through the text extraction path. Below
# this floor the pipeline routes straight to vision and emits the
# ``[META] vision_routed: chars=N`` flag — see the image-only routing
# branch in ``run_pipeline``. Mirrors ``metadata._TEXT_LAYER_MIN_CHARS``
# (the metadata-layer detection threshold); re-declared here so the
# pipeline log line + flag detail read against the same name without
# importing a private constant from the metadata module.
VISION_ROUTE_THRESHOLD: Final[int] = 50

# Average classification-confidence floor. Below this, the document goes
# to manual_review regardless of math / metadata / pattern scores —
# classifier signaling low confidence is the LLM telling us it can't
# read the rows accurately, and downstream patterns + aggregates are
# only as good as the labels.
#
# Tune based on real-deal data after ~50 funded deals. Track per-statement
# avg confidence distribution in operator dashboard to inform tuning.
CLASSIFICATION_CONFIDENCE_FLOOR: Final[int] = 60

# Per-category floor for high-impact categories (mca_debit drives the
# stacking pattern + scoring penalties; low confidence there poisons
# both). Same tuning guidance as above — adjust once we have signal
# from real funded deals.
HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR: Final[int] = 70
HIGH_IMPACT_CATEGORIES: Final[frozenset[str]] = frozenset({"mca_debit", "nsf_fee"})

# R1.7 — ADB partial-coverage escalation threshold (shadow). When the
# aggregator's adb_partial_coverage flag reports skipped/period > 10%,
# the average-daily-balance metric is computed over too few days to be
# trusted. Decision-boundary rule (CLAUDE.md) requires shadow-mode first
# for anything that would route a doc from proceed → manual_review, so
# this commit only EMITS a flag (`adb_coverage_thin:...would_route_review`)
# and does NOT change parse_status. Operator validates against corpus
# then flips via config in a follow-up commit.
ADB_COVERAGE_THIN_RATIO_THRESHOLD: Final[Decimal] = Decimal("0.10")

ParseStatus = Literal["proceed", "review", "manual_review"]


@dataclass(frozen=True)
class MerchantContext:
    """Free-text deal context injected into the Bedrock extraction prompt.

    Feature D (migration 064). The four fields mirror the merchants-row
    columns:

      * ``deal_context``           — operator-written notes about the
        deal.
      * ``close_lead_description`` — Close Lead ``description`` field.
      * ``close_notes_summary``    — concatenated bodies of the most
        recent Close notes.
      * ``close_call_transcripts`` — concatenated post-call note text
        from the most recent Close calls.

    All four are ``None`` by default; the prompt builder omits lines
    whose value is empty. An instance whose every field is ``None`` /
    empty is treated as "no context" by
    ``_build_extraction_prompt_suffix`` — equivalent to passing
    ``merchant_context=None``.

    Frozen + slot-less so existing pipeline machinery (the
    ``asyncio.to_thread`` boundary in the worker) can pass it across
    threads without copy concerns and tests can compare instances via
    ``==``.
    """

    deal_context: str | None = None
    close_lead_description: str | None = None
    close_notes_summary: str | None = None
    close_call_transcripts: str | None = None

    def is_empty(self) -> bool:
        """True when no field has any non-whitespace content."""
        for value in (
            self.deal_context,
            self.close_lead_description,
            self.close_notes_summary,
            self.close_call_transcripts,
        ):
            if value and value.strip():
                return False
        return True


@dataclass
class PipelineResult:
    parse_status: ParseStatus
    metadata: MetadataAnalysis
    extraction: ExtractionPass1Result | None
    validation: ValidationResult
    classified: list[ClassifiedTransaction] = field(default_factory=list)
    patterns: PatternAnalysis | None = None
    aggregates: Aggregates | None = None
    fraud_score: int = 0
    fraud_score_breakdown: dict[str, int] = field(default_factory=dict)
    all_flags: list[str] = field(default_factory=list)
    # Processor brand discriminator. Always ``None`` on the bank
    # pipeline result — the bank pipeline never produces a processor
    # statement. The worker decides processor routing UPSTREAM of
    # ``run_pipeline`` (see ``aegis.workers._run_processor_branch``);
    # the field exists here so downstream code can read a uniform
    # ``result.processor_type`` regardless of which pipeline ran.
    # See ``aegis.parser.processor.processor_type_for_document`` for the
    # public routing decision helper.
    processor_type: str | None = None
    # Average classification confidence across all classified rows
    # (100 when no rows were classified — e.g. validation failed). Used
    # by the parse_status gate and surfaced on the merchant detail page.
    avg_classification_confidence: int = 100
    # Per-category averages keyed by category name. Empty when no rows
    # were classified. Used for high-impact category gating.
    classification_confidence_by_category: dict[str, int] = field(default_factory=dict)
    # Per-calendar-month roll-up (deposits, withdrawals, avg_balance).
    # Persisted on analyses.monthly_breakdown for renewal-merchant
    # month-over-month deltas. Decimals stored as strings for jsonb.
    monthly_breakdown: list[dict[str, str]] = field(default_factory=list)
    # Tampering composition evaluation — see
    # ``aegis.parser.tampering.evaluate_tampering``. None for pipeline
    # paths that short-circuit before metadata + math signals are
    # available (e.g. page-router low-confidence early exit). The
    # worker writes the shadow / live audit row when this is set and
    # ``tampering_evaluation.fires`` is True.
    tampering_evaluation: TamperingEvaluation | None = None
    # U15 — cross-statement / related-account shadow flags emitted by
    # the U12 detector via ``aegis.merchants.cross_statement_pipeline``.
    # Populated by the WORKER (not ``run_pipeline``) after the new
    # analysis is persisted — the detector queries the merchant's
    # prior parses and only the worker has the merchant context +
    # repository. Always severity 0 per U12's shadow-only invariant;
    # NOT merged into ``patterns.patterns`` so a future scoring change
    # can't accidentally pull cross-document severities into the
    # per-document ``fraud_score`` sum. Empty list on first uploads /
    # bearer uploads / merchant_id is None.
    cross_statement_patterns: list[Pattern] = field(default_factory=list)


def run_pipeline(
    pdf_path: str,
    llm: LLMClient,
    *,
    today: date | None = None,
    bank_layouts: BankLayoutRepository | None = None,
    known_bank_name: str | None = None,
    vision_fallback_on_extraction_error: bool = False,
    merchant_context: MerchantContext | None = None,
) -> PipelineResult:
    """Run the full parser pipeline.

    Phase 5 will wrap this in an arq worker; for now it's synchronous.
    LLM is injected so tests can pass a fake.

    ``bank_layouts`` is the optional layout-learning surface (migration
    059). When provided AND ``known_bank_name`` resolves to a row that
    has crossed ``HINTS_AVAILABLE_THRESHOLD`` successful parses with
    non-empty hints, the operator's hints text is appended to the
    Bedrock extraction prompt. After a successful parse
    (``parse_status in ('proceed', 'review')``), the pipeline calls
    ``bank_layouts.upsert_success`` with a PII-free layout fingerprint
    so subsequent parses of the same bank can benefit.

    ``known_bank_name`` is the caller-provided bank-name hint for the
    upcoming parse — typically pulled from a prior analysis's
    ``BankIdentity.bank_name`` on the same merchant. The first-ever
    parse for a bank (no prior analyses) has no hint; that's expected
    behavior — there's nothing to learn from yet. Tests that don't
    care about layout learning leave both kwargs at their defaults.

    ``vision_fallback_on_extraction_error`` is the third-pass escape
    hatch wired by ``scripts/recover_legacy_docs.py``'s
    ``_run_pipeline_with_retry``. When True AND the text-layer pass
    raises ``ExtractionError`` (most commonly a pydantic
    ``ValidationError`` chained from ``summary.period_start=None`` /
    ``period_end=None`` — Bedrock seeing the page-1 period block as
    layout chrome and dropping it), the pipeline catches the error and
    re-runs through ``extract_statement_via_vision`` with the same
    prompt suffix. Vision sees the page as an image instead of text,
    which sidesteps the text-layer dropouts that defeat the second-pass
    text-with-hint retry. The fallback is opt-in because vision tokens
    are ~5-8x text tokens (see ``MAX_OCR_PAGES``) — callers pay for it
    only when the text path has already cost a round-trip. The
    image-only path (``not metadata.has_text_layer``) is unaffected:
    that branch already routes straight to vision and there is no
    text-extraction call to wrap. ``used_ocr_fallback`` is set to True
    when the fallback fires so the ``[META] ocr_fallback_used`` flag
    surfaces on the merchant detail page and the operator can see WHY
    the doc went through vision.
    """
    metadata = analyze_metadata(pdf_path)

    pdf_bytes = _read_pdf(pdf_path)

    settings = get_settings()
    page_decisions: list[PageStrategyDecision] = []
    if settings.aegis_parser_page_routing:
        page_decisions = classify_pages(pdf_path)
        _log.info(
            "parser.page_router.decisions",
            extra={"document": str(pdf_path), **summarize(page_decisions)},
        )
        if page_decisions and has_low_confidence(page_decisions):
            # Per master plan §6.5: any page with both strategies below
            # the floor fails the whole doc closed. Partial extraction
            # would poison the aggregates.
            synthetic_validation = ValidationResult(
                passed=False,
                failures=["page_router_low_confidence"],
            )
            return PipelineResult(
                parse_status="manual_review",
                metadata=metadata,
                extraction=None,
                validation=synthetic_validation,
                all_flags=_collect_flags(metadata, synthetic_validation, None, []),
            )

    # Bank-layout hint injection. Only fires when the caller wired a
    # repo AND supplied a bank-name hint AND that bank has crossed the
    # hints-available threshold with non-empty operator text. Otherwise
    # extraction_prompt_suffix stays ``None`` and the base prompt runs
    # unchanged.
    #
    # Feature D (migration 064) — the same builder also folds in the
    # caller-supplied merchant context block when provided. When both
    # are present the two suffixes concatenate with a blank line between.
    extraction_prompt_suffix = _build_extraction_prompt_suffix(
        bank_layouts=bank_layouts,
        known_bank_name=known_bank_name,
        merchant_context=merchant_context,
    )

    used_ocr_fallback = False
    used_vision_routed = False
    used_per_page_routing = False
    if settings.aegis_parser_page_routing and page_decisions and not is_homogeneous(page_decisions):
        # Mixed strategies → per-page extraction. is_homogeneous None /
        # homogeneous → fall through to the legacy single-call path so
        # the simple cases keep the cheaper one-shot behavior.
        extraction = extract_statement_per_page(
            pdf_bytes,
            llm,
            page_decisions,
            prompt_suffix=extraction_prompt_suffix,
        )
        used_per_page_routing = True
        used_ocr_fallback = any(d.strategy == "vision" for d in page_decisions)
    elif not metadata.has_text_layer:
        if metadata.page_count > MAX_OCR_PAGES:
            # Image-only PDF larger than the vision-cost cap. Refuse to
            # OCR; route to manual_review so the operator can split the
            # document or request a fresh export with a text layer.
            synthetic_validation = ValidationResult(
                passed=False,
                failures=[
                    f"ocr_oversize_image_pdf: page_count={metadata.page_count} max={MAX_OCR_PAGES}"
                ],
            )
            return PipelineResult(
                parse_status="manual_review",
                metadata=metadata,
                extraction=None,
                validation=synthetic_validation,
                all_flags=_collect_flags(metadata, synthetic_validation, None, []),
            )
        # Image-only → vision-first. Skip the layout-hints block: hints
        # describe text-layer structure (column labels, section
        # delimiters, line geometry) that the vision model doesn't see
        # the same way. The merchant-context block stays (it carries
        # deal narrative, not extraction-layout instructions).
        _log.info(
            "parser.pipeline.vision_routed file=%s chars=%d threshold=%d",
            getattr(pdf_path, "name", str(pdf_path)),
            metadata.text_layer_char_count,
            VISION_ROUTE_THRESHOLD,
        )
        vision_prompt_suffix = _build_extraction_prompt_suffix(
            bank_layouts=None,
            known_bank_name=None,
            merchant_context=merchant_context,
        )
        extraction = extract_statement_via_vision(
            pdf_bytes, llm, prompt_suffix=vision_prompt_suffix
        )
        used_vision_routed = True
    else:
        try:
            extraction = extract_statement(pdf_bytes, llm, prompt_suffix=extraction_prompt_suffix)
        except ExtractionError as text_err:
            if not vision_fallback_on_extraction_error:
                raise
            if metadata.page_count > MAX_OCR_PAGES:
                # The vision fallback's per-page token cost would blow
                # the budget on a huge doc — let the original text-pass
                # failure surface unchanged so the operator sees the
                # underlying error rather than a misleading "vision also
                # failed". Same cap the dedicated image-only branch
                # enforces above.
                raise
            _log.warning(
                "parser.pipeline.vision_fallback_on_text_failure error=%s",
                text_err,
            )
            extraction = extract_statement_via_vision(
                pdf_bytes, llm, prompt_suffix=extraction_prompt_suffix
            )
            used_ocr_fallback = True

    validation = validate_extraction(
        extraction.statement,
        truncated=extraction.truncated,
        today=today,
    )

    # Forensic layer #2 — PDF-creator fingerprinting. Runs HERE (post-
    # extraction, pre-_fraud_score) so the +20 contribution propagates
    # through ``_fraud_score`` and ``_decide``. Mutates
    # ``metadata.creator_mismatch_detected`` so the dossier can show
    # the boolean alongside the rest of the metadata fields. Appends
    # the flag to ``metadata.flags`` so ``_collect_flags`` prefixes it
    # with ``[META]`` naturally — same flow as
    # ``font_inconsistency_detected`` / ``text_overlay_detected``.
    from aegis.parser.forensic.creator_fingerprint import (
        analyze as _creator_fingerprint_analyze,
    )

    parsed_bank_name = extraction.statement.summary.bank_name if extraction is not None else None
    creator_fingerprint = _creator_fingerprint_analyze(
        metadata.pdf_creator,
        metadata.pdf_producer,
        parsed_bank_name,
    )
    if creator_fingerprint.mismatch_detected:
        metadata.creator_mismatch_detected = True
        metadata.flags.append(
            f"creator_mismatch_detected: detected={creator_fingerprint.detected_creator!r}; "
            f"editing_tool={creator_fingerprint.editing_tool_match!r}; "
            f"expected_one_of={creator_fingerprint.expected_patterns}"
        )
        # +20 contribution to metadata.fraud_score — stronger than the
        # generic ``editor_detected`` signal (+15 inside
        # ``_HARD_EDITORS``) because creator-vs-bank mismatch is a
        # SPECIFIC fingerprint ("PDFlib on a BoA statement", not "PDFlib
        # somewhere"). Mirrors the per-detector weights documented next
        # to ``font_inconsistency_detected`` (+15) and
        # ``text_overlay_detected`` (+25) in ``parser/metadata.py``.
        # Clamped to 100 to match ``analyze_metadata``'s exit clamp.
        metadata.fraud_score = min(100, metadata.fraud_score + 20)

    if not validation.passed:
        flags = _collect_flags(metadata, validation, None, [])
        if used_vision_routed:
            flags.append(f"[META] vision_routed: chars={metadata.text_layer_char_count}")
        if used_ocr_fallback:
            flags.append("[META] ocr_fallback_used")
        if used_per_page_routing:
            flags.append("[META] per_page_routing_used")
        return PipelineResult(
            parse_status="manual_review",
            metadata=metadata,
            extraction=extraction,
            validation=validation,
            all_flags=flags,
        )

    classified = classify_transactions(extraction.statement.transactions, llm)
    patterns = analyze_patterns(
        classified,
        period_start=extraction.statement.summary.period_start,
        period_end=extraction.statement.summary.period_end,
        today=today,
    )
    aggregate_result = aggregate(
        classified,
        period_start=extraction.statement.summary.period_start,
        period_end=extraction.statement.summary.period_end,
        beginning_balance=extraction.statement.summary.beginning_balance,
    )
    aggregates = aggregate_result.aggregates

    avg_conf = avg_classification_confidence(classified)
    per_cat_conf = per_category_confidence(classified)
    confidence_failures = _confidence_failures(avg_conf, per_cat_conf)

    triangulation_flag = _fraud_cluster_triangulation(patterns)

    math_score = _math_score(validation)
    patterns_score_with_bump = patterns.fraud_score
    if triangulation_flag is not None:
        # Triangulated cluster: multiple independent red flags fired
        # together. Bump the patterns score by 10 (capped 100) so the
        # combined fraud_score reflects the correlation. The triangulation
        # rule is intentionally simple — refine after real-deal signal.
        patterns_score_with_bump = min(100, patterns.fraud_score + 10)

    fraud_score, breakdown, compound_flags = _fraud_score(
        metadata.fraud_score, math_score, patterns_score_with_bump
    )

    # Tampering composition (operator policy 2026-06-04): runs on every
    # parse, attached to the result; the worker decides whether to gate
    # (live mode) or just audit (shadow mode). Pure function over the
    # already-computed scores + validation failures.
    tampering_eval = evaluate_tampering(
        metadata_score=metadata.fraud_score,
        math_score=math_score,
        validation_failures=list(validation.failures),
    )

    # Track A integrity pre-screen — replaces the legacy
    # ``fraud_score >= HARD_DECLINE_THRESHOLD`` /
    # ``metadata.fraud_score >= METADATA_HARD_DECLINE`` gates. Sums the
    # severities of the five [META] forensic-family flags emitted by
    # ``parser.metadata`` + the ``creator_mismatch_detected`` flag
    # appended above. See ``_PRESCREEN_FLAG_SEVERITIES`` for the
    # per-signal contributions.
    track_a_prescreen_sum = _track_a_prescreen_severity_sum(metadata)
    parse_status = _decide(
        metadata,
        validation,
        confidence_failures,
        track_a_prescreen_sum=track_a_prescreen_sum,
    )

    all_flags = _collect_flags(metadata, validation, patterns, compound_flags)
    if (
        parse_status == "manual_review"
        and track_a_prescreen_sum >= TRACK_A_PRESCREEN_THRESHOLD
        and metadata.eof_markers <= EOF_HARD_DECLINE
        and not confidence_failures
    ):
        # Surface the gate that fired so the operator can see WHY the
        # pipeline routed to manual_review. Mirrors the
        # ``page_router_low_confidence`` / ``ocr_oversize_image_pdf``
        # synthetic-reason pattern; lands on ``all_flags`` with the
        # ``[META] `` category prefix via ``_collect_flags``'s output
        # already, so we append directly to ``all_flags`` here with the
        # explicit prefix and the severity sum for audit.
        all_flags.append(
            f"[META] track_a_prescreen_integrity_fail: severity_sum={track_a_prescreen_sum} "
            f"threshold={TRACK_A_PRESCREEN_THRESHOLD}"
        )
    all_flags.extend(f"[AGGREGATE] {f}" for f in aggregate_result.flags)
    all_flags.extend(f"[CONFIDENCE] {f}" for f in confidence_failures)
    if triangulation_flag is not None:
        all_flags.append(f"[COMPOUND] {triangulation_flag}")
    if used_vision_routed:
        all_flags.append(f"[META] vision_routed: chars={metadata.text_layer_char_count}")
    if used_ocr_fallback:
        all_flags.append("[META] ocr_fallback_used")
    if used_per_page_routing:
        all_flags.append("[META] per_page_routing_used")

    # Period regex fallback — surface the matched pattern + fragment on
    # ``all_flags`` so the worker writes the corresponding audit row
    # (``parser.period_regex_fallback_used``) and the operator can see
    # WHY the doc parsed despite Bedrock dropping the period. The
    # fragment is already truncated to ``_FRAGMENT_MAX_LEN`` (200 chars)
    # inside ``period_regex.py`` and contains no PII (period text only).
    period_fallback = getattr(extraction, "period_regex_fallback_used", None)
    if period_fallback is not None:
        all_flags.append(
            f"[META] period_regex_fallback_used:{period_fallback.pattern_name}:"
            f"{period_fallback.fragment}"
        )
        _log.info(
            "parser.period_regex_fallback_used pattern=%s fragment=%r",
            period_fallback.pattern_name,
            period_fallback.fragment,
        )

    # R1.7 — ADB partial-coverage escalation (SHADOW). When the aggregator
    # reports more than 10% of period days skipped, the ADB metric is
    # computed over too narrow a window to be trustworthy. Emit a shadow
    # flag that documents the would-be routing change without touching
    # parse_status — operator flips via config after corpus validation.
    adb_thin_flag = _adb_coverage_thin_flag(aggregate_result.flags)
    if adb_thin_flag is not None:
        all_flags.append(f"[SHADOW] {adb_thin_flag}")

    # R1.8 — NSF secondary validation (SHADOW). Re-checks every LLM-labeled
    # nsf_fee row against running-balance + co-located return / reversal /
    # chargeback evidence. Low-confidence NSF rows fire separately. Pure
    # evidence emission; the row's category and parse_status are unchanged.
    nsf_issues = secondary_validate_nsf(
        classified,
        beginning_balance=extraction.statement.summary.beginning_balance,
        period_start=extraction.statement.summary.period_start,
        period_end=extraction.statement.summary.period_end,
    )
    all_flags.extend(f"[SHADOW] {issue.flag_text}" for issue in nsf_issues)

    # ------------------------------------------------------------------
    # Forensic + shadow-detector emit ladder. Ordering (operator spec):
    #   1. tampering_flag                  — doc-level forensic
    #   2. fintech_bank_detected           — bank-of-record forensic (WARN)
    #   3. unreconciled_internal_transfer_v2 — txn-level shadow detector
    #   4. ai_generated_statement          — composite forensic shadow
    # All blocks are append-only on ``all_flags``; none consume another's
    # output, so ordering is purely about the row order an operator sees
    # in the dossier flags table. Bank-layout learning runs after.
    # ------------------------------------------------------------------

    # 1. Plan 5.1 — Persist tampering evaluation on documents.all_flags.
    # Pure visibility extension; not a decision-boundary change. See
    # ``_tampering_persistence_flag`` docstring for the prefix-vs-mode
    # rationale.
    tampering_flag = _tampering_persistence_flag(
        tampering_eval, settings.aegis_tampering_decline_mode
    )
    if tampering_flag is not None:
        all_flags.append(tampering_flag)

    # 2. Fintech bank-of-record detection (warning only). When the
    # extracted ``bank_name`` matches a known fintech / neobank
    # (Mercury, Brex, Novo, etc.), surface a ``[WARN]`` flag so the
    # operator + the per-funder match grid see the bank as a soft
    # concern. This is NOT a decline path — funder appetite for
    # fintech accounts varies (see ``parser.fintech_banks`` module
    # docstring). parse_status, fraud_score, and FRAUD_WEIGHTS are
    # unchanged; the only side effect is one new entry on all_flags.
    fintech_bank_name = extraction.statement.summary.bank_name if extraction is not None else None
    fintech_hit = detect_fintech_bank(fintech_bank_name)
    if fintech_hit is not None:
        canonical_name, _warning = fintech_hit
        all_flags.append(
            f"[WARN] fintech_bank_detected: {canonical_name} — "
            f"many funders decline fintech bank accounts"
        )

    # 3. Shadow unreconciled-internal-transfer v2 (operator spec 2026-06-24).
    # Surface each shadow Pattern with code
    # ``unreconciled_internal_transfer_v2`` produced by
    # ``patterns.detect_unreconciled_internal_transfers`` as a
    # ``[SHADOW] unreconciled_internal_transfer_v2:...`` entry in
    # ``all_flags``. The ``_v2`` suffix disambiguates this shadow detector
    # from the live ``unreconciled_internal_transfer`` Pattern emitted by
    # ``_unreconciled_internal_transfer`` — both can fire in parallel
    # during shadow validation. Per CLAUDE.md decision-boundary discipline
    # this is evidence-only — the parse_status branch is unchanged because
    # the detector emits to ``patterns.shadow_patterns`` (not ``patterns``)
    # and its FRAUD_WEIGHTS entry is 0.0.
    for shadow_pat in patterns.shadow_patterns:
        if shadow_pat.code == "unreconciled_internal_transfer_v2":
            all_flags.append(f"[SHADOW] {shadow_pat.code}: {shadow_pat.detail}")

    # 4. Shadow composite AI-generated-statement detector (operator spec
    # 2026-06-24). Fuses math-perfection + description-uniformity +
    # round-number clustering + font-uniformity into one 0..100
    # composite score. Emits when composite >= 40. Reads the already-
    # computed ``FontConsistencyResult`` off ``metadata`` so the PDF
    # is NOT re-opened. Per CLAUDE.md decision-boundary discipline this
    # is shadow-only: appended to ``patterns.shadow_patterns`` (not
    # ``patterns``) and surfaced as ``[SHADOW] ai_generated_statement:``
    # in ``all_flags``. ``FRAUD_WEIGHTS["shadow_ai_generated_statement"]``
    # is 0.0 — the carve-out is documented next to the live weights.
    from aegis.parser.forensic.ai_statement import detect_ai_generated_statement

    ai_period_flags = [f for f in aggregate_result.flags if f.startswith("period_")]
    ai_pattern = detect_ai_generated_statement(
        classified,
        math_flags=list(validation.failures),
        font_result=metadata.font_consistency_result,
        period_flags=ai_period_flags,
    )
    if ai_pattern is not None:
        patterns.shadow_patterns.append(ai_pattern)
        all_flags.append(f"[SHADOW] {ai_pattern.code}: {ai_pattern.detail}")

    # Bank-layout learning: record the successful parse so the next
    # parse for the same bank can leverage operator-curated hints. Only
    # fires on parse_status ('proceed', 'review') — manual_review docs
    # are not "successful" by the learning surface's definition. The
    # learning is best-effort; persistence failures are logged but do
    # NOT fail the parse (an inability to learn must not break the
    # working pipeline).
    if parse_status in ("proceed", "review") and bank_layouts is not None:
        parsed_bank_name = (
            extraction.statement.summary.bank_name if extraction is not None else None
        )
        if parsed_bank_name:
            try:
                bank_layouts.upsert_success(
                    bank_name=parsed_bank_name,
                    fingerprint=_build_layout_fingerprint(extraction, metadata),
                )
            except BankLayoutWriteError:
                _log.warning(
                    "parser.bank_layout.upsert_failed bank_name=%s",
                    parsed_bank_name,
                )
            # Auto-hint generation — runs after upsert_success so the
            # bumped successful_parses count is visible to any caller
            # querying mid-flight. Best-effort: a generator exception or
            # a set_hints failure does NOT fail the parse (the learning
            # surface is opportunistic, not load-bearing). Deliberately
            # skipped when the LLM extracted no bank name; merging into
            # an unkeyed row is meaningless.
            try:
                first_page_text = _extract_first_page_text(pdf_path)
                # Duck-typed parse_result shim — ``auto_hints`` consumes
                # ``parse_result.classified.transactions`` via attribute
                # walk so any object with that path works. Avoids the
                # circular import of ``PipelineResult`` here.
                from types import SimpleNamespace

                parse_result_shim = SimpleNamespace(
                    extraction=extraction,
                    classified=SimpleNamespace(transactions=classified),
                )
                auto_hint = generate_hints_from_parse_result(
                    bank_name=parsed_bank_name,
                    first_page_text=first_page_text,
                    parse_result=parse_result_shim,
                )
                if auto_hint:
                    existing_raw = bank_layouts.get_raw_hints(parsed_bank_name)
                    merged = merge_hints(existing_raw, auto_hint)
                    if merged != (existing_raw or ""):
                        bank_layouts.set_hints(
                            bank_name=parsed_bank_name,
                            hints=merged,
                            source="auto",
                        )
            except Exception as exc:
                # Auto-hint write failures are best-effort — log + move
                # on. The next parse will retry the generation. Broad
                # except is intentional: a regex compilation issue, a
                # pymupdf decode error, or a Supabase write failure all
                # land here without breaking the parse.
                _log.warning(
                    "parser.bank_layout.auto_hint_failed bank_name=%s error=%s",
                    parsed_bank_name,
                    type(exc).__name__,
                )

    return PipelineResult(
        parse_status=parse_status,
        metadata=metadata,
        extraction=extraction,
        validation=validation,
        classified=classified,
        patterns=patterns,
        aggregates=aggregates,
        fraud_score=fraud_score,
        fraud_score_breakdown=breakdown,
        all_flags=all_flags,
        avg_classification_confidence=avg_conf,
        classification_confidence_by_category=per_cat_conf,
        monthly_breakdown=aggregate_result.monthly_breakdown,
        tampering_evaluation=tampering_eval,
    )


def _build_extraction_prompt_suffix(
    *,
    bank_layouts: BankLayoutRepository | None,
    known_bank_name: str | None,
    merchant_context: MerchantContext | None = None,
) -> str | None:
    """Return the prompt-suffix text for the extraction call.

    Composed of two optional blocks:

      * MERCHANT CONTEXT block (Feature D). When ``merchant_context`` is
        non-None AND has at least one non-empty field, prepends a block
        of the form::

            MERCHANT CONTEXT (use this to better understand the deal):
            Operator notes: <deal_context>
            Close lead description: <close_lead_description>
            Recent Close notes: <close_notes_summary>
            Recent call summaries: <close_call_transcripts>

        Lines whose value is empty / None are omitted entirely.

      * Layout-hints block. When ``bank_layouts`` is wired AND
        ``known_bank_name`` is set AND the repository returns non-None
        hints for that bank, appends::

            Layout hints from prior successful parses of this bank:
            <verbatim hints text>

    When BOTH blocks exist they are concatenated with a single blank
    line between (merchant-context first, layout-hints second). When
    NEITHER exists this returns ``None`` and the base extraction prompt
    runs unchanged.
    """
    merchant_block = _build_merchant_context_block(merchant_context)
    layout_block = _build_layout_hints_block(
        bank_layouts=bank_layouts, known_bank_name=known_bank_name
    )

    if merchant_block is not None and layout_block is not None:
        return f"{merchant_block}\n\n{layout_block}"
    if merchant_block is not None:
        return merchant_block
    if layout_block is not None:
        return layout_block
    return None


def _build_merchant_context_block(
    merchant_context: MerchantContext | None,
) -> str | None:
    """Format the MERCHANT CONTEXT block, omitting empty lines.

    Returns ``None`` when the context is None or every field is empty —
    that's the "no merchant context" case and the suffix should not
    include the heading.
    """
    if merchant_context is None or merchant_context.is_empty():
        return None
    lines: list[str] = ["MERCHANT CONTEXT (use this to better understand the deal):"]
    for label, value in (
        ("Operator notes", merchant_context.deal_context),
        ("Close lead description", merchant_context.close_lead_description),
        ("Recent Close notes", merchant_context.close_notes_summary),
        ("Recent call summaries", merchant_context.close_call_transcripts),
    ):
        if value and value.strip():
            lines.append(f"{label}: {value.strip()}")
    return "\n".join(lines)


def _build_layout_hints_block(
    *,
    bank_layouts: BankLayoutRepository | None,
    known_bank_name: str | None,
) -> str | None:
    """Format the layout-hints block. Returns ``None`` when no usable
    hints are available.
    """
    if bank_layouts is None or not known_bank_name:
        return None
    hints = bank_layouts.get_hints(known_bank_name)
    if hints is None:
        return None
    return "Layout hints from prior successful parses of this bank:\n" + hints


def _build_layout_fingerprint(
    extraction: object,
    metadata: MetadataAnalysis,
) -> dict[str, object]:
    """Build the PII-free fingerprint dict for ``upsert_success``.

    Captures observable layout properties only: transaction count,
    whether running balances were printed on the rows, page count,
    currency. NEVER includes account holder names, transaction
    descriptions, or any merchant identifier (CLAUDE.md PII rule).

    The ``extraction`` parameter is typed ``object`` rather than
    ``ExtractionPass1Result`` to keep this helper module-local without
    importing the type at runtime — the structural attribute access
    suffices and the caller guarantees the value is non-None on the
    success path.
    """
    # Attribute access is duck-typed against ExtractionPass1Result.
    statement = getattr(extraction, "statement", None)
    transactions = getattr(statement, "transactions", []) if statement else []
    has_running_balance = any(getattr(t, "running_balance", None) is not None for t in transactions)
    return {
        "transaction_count": len(transactions),
        "has_running_balance": has_running_balance,
        "page_count": metadata.page_count,
        "currency": "USD",
    }


def _read_pdf(pdf_path: str) -> bytes:
    from pathlib import Path

    return Path(pdf_path).read_bytes()


def _extract_first_page_text(pdf_path: str) -> str:
    """Return the first-page text layer (empty string on any failure).

    Used by the auto-hint generator at the tail of every successful
    parse. The vision-routed branch (image-only PDFs) returns empty
    string here too — auto-hints describe text-layer structure that
    the vision model doesn't surface the same way, so returning empty
    correctly short-circuits the auto-hint generator's pattern probes.
    Best-effort: any pymupdf exception (encrypted PDF, malformed
    stream) returns empty string. The caller (auto-hint generation
    block in ``run_pipeline``) wraps this in its own try/except too.
    """
    try:
        import pymupdf

        with pymupdf.open(pdf_path) as doc:  # type: ignore[no-untyped-call]
            if doc.page_count == 0:
                return ""
            text = doc.load_page(0).get_text("text") or ""
            return cast(str, text)
    except Exception:
        return ""


def _math_score(validation: ValidationResult) -> int:
    """Same severity grading as TS: critical failures count more.

    Critical = reconciliation_failed_*, future_dated, extraction_truncated.
    """
    if not validation.failures:
        return 0
    critical_prefixes = ("reconciliation_failed", "future_dated", "extraction_truncated")
    critical = sum(1 for f in validation.failures if f.startswith(critical_prefixes))
    n = len(validation.failures)
    if n == 1:
        return 55 if critical else 25
    if n == 2:
        return 85 if critical else 65
    return 100


def _fraud_score(
    metadata_score: int, math_score: int, patterns_score: int
) -> tuple[int, dict[str, int], list[str]]:
    raw = round(
        metadata_score * FRAUD_WEIGHTS["metadata"]
        + math_score * FRAUD_WEIGHTS["math"]
        + patterns_score * FRAUD_WEIGHTS["patterns"]
    )

    escalated = raw
    compound: list[str] = []
    if metadata_score >= 50 and patterns_score >= 40:
        escalated = max(escalated, HARD_DECLINE_THRESHOLD)
        compound.append("metadata+patterns elevated together")
    if math_score >= 55 and patterns_score >= 40:
        escalated = max(escalated, HARD_DECLINE_THRESHOLD + 5)
        compound.append("math_failure+patterns elevated together")
    if metadata_score >= 40 and math_score >= 40 and patterns_score >= 30:
        escalated = max(escalated, HARD_DECLINE_THRESHOLD + 5)
        compound.append("three-layer signal convergence")
    if patterns_score >= 80:
        escalated = max(escalated, HARD_DECLINE_THRESHOLD + 5)

    return (
        escalated,
        {
            "metadata_score": metadata_score,
            "math_score": math_score,
            "patterns_score": patterns_score,
        },
        compound,
    )


# fraud_score is informational only — routing decisions use Track A
# integrity pre-screen + track_abc scoring engine.
def _decide(
    metadata: MetadataAnalysis,
    validation: ValidationResult,
    confidence_failures: list[str],
    *,
    track_a_prescreen_sum: int,
) -> ParseStatus:
    """Resolve ``parse_status`` from deterministic pre-screen signals.

    Decision ladder (top wins):

    1. ``metadata.eof_markers > EOF_HARD_DECLINE`` → ``manual_review``.
       Independent gate — incremental-save spam is its own signal class
       and never blended into the integrity sum.
    2. ``track_a_prescreen_sum >= TRACK_A_PRESCREEN_THRESHOLD`` →
       ``manual_review``. Replaces the prior fraud_score-based gates
       (``metadata.fraud_score >= METADATA_HARD_DECLINE`` and
       ``fraud_score >= HARD_DECLINE_THRESHOLD``) — pre-screen now reads
       Track A integrity signals exclusively. Reason
       ``track_a_prescreen_integrity_fail`` is appended to the
       synthetic validation failures by the caller so the operator
       sees the gate that fired.
    3. ``confidence_failures`` → ``manual_review``. LLM classifier
       can't read the rows; downstream metrics inherit the noise.
    4. ``metadata.eof_markers > 1`` → ``review``. Single extra EOF is
       a soft signal that the operator can clear.
    5. Otherwise ``proceed``.

    Note: ``validation.passed`` is always True here — the caller short-
    circuits to ``manual_review`` BEFORE this function on a failed
    validation, so a ``review`` branch keyed on ``not validation.passed``
    would be unreachable.
    """
    if metadata.eof_markers > EOF_HARD_DECLINE:
        return "manual_review"
    if track_a_prescreen_sum >= TRACK_A_PRESCREEN_THRESHOLD:
        return "manual_review"
    if confidence_failures:
        # LLM signaled it couldn't classify accurately — labels are
        # suspect and downstream patterns/aggregates inherit the noise.
        # Same severity as a math gate failure.
        return "manual_review"
    if metadata.eof_markers > 1:
        return "review"
    return "proceed"


def _track_a_prescreen_severity_sum(metadata: MetadataAnalysis) -> int:
    """Sum severities of the [META] forensic-family flags on ``metadata``.

    Walks ``metadata.flags`` (the raw, unprefixed form — the caller
    has not yet run ``_collect_flags`` to add the ``[META] `` prefix)
    and adds the severity for each entry whose prefix matches an entry
    in ``_PRESCREEN_FLAG_SEVERITIES``. Order-independent and
    duplicate-tolerant (parser emits at most one entry per flag family
    per document, but the helper sums all matches rather than dedupes
    to keep the math transparent).

    Returns 0 when no qualifying flags fired — that's the clean-statement
    case and the pre-screen passes the document through to scoring.

    Per-signal contributions mirror the deltas
    ``parser.metadata.analyze_metadata`` adds to
    ``metadata.fraud_score`` so the pre-screen sum tracks the underlying
    metadata-score contribution for these five families. See
    ``_PRESCREEN_FLAG_SEVERITIES`` for the table and rationale.
    """
    total = 0
    for flag in metadata.flags:
        for prefix, severity in _PRESCREEN_FLAG_SEVERITIES:
            if flag.startswith(prefix):
                total += severity
                break
    return total


def _fraud_cluster_triangulation(patterns: PatternAnalysis | None) -> str | None:
    """Three+ independent patterns with at least one severity >= 25 -> triangulated.

    Single patterns are routine; clusters of three are not. The flag is
    informational + a +10 bump on the patterns score so the combined
    fraud_score reflects the correlation between independent red flags.

    Tune based on real-deal data after ~50 funded deals.
    """
    if patterns is None or len(patterns.patterns) < 3:
        return None
    if not any(p.severity >= 25 for p in patterns.patterns):
        return None
    codes = [p.code for p in patterns.patterns]
    return f"fraud_cluster_triangulated:{len(patterns.patterns)}_signals_" + ",".join(codes[:5])


def _confidence_failures(avg_conf: int, per_cat_conf: dict[str, int]) -> list[str]:
    """Return failure codes for classification confidence below the floor.

    Empty list = no failure. Two paths trigger:
      1. Overall avg below CLASSIFICATION_CONFIDENCE_FLOOR.
      2. Any high-impact category (mca_debit, nsf_fee) below
         HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR.
    """
    failures: list[str] = []
    if avg_conf < CLASSIFICATION_CONFIDENCE_FLOOR:
        failures.append(
            f"classification_confidence_below_floor: avg={avg_conf} "
            f"floor={CLASSIFICATION_CONFIDENCE_FLOOR}"
        )
    for cat in HIGH_IMPACT_CATEGORIES:
        cat_conf = per_cat_conf.get(cat)
        if cat_conf is None:
            continue
        if cat_conf < HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR:
            failures.append(
                f"classification_confidence_below_floor_{cat}: "
                f"avg={cat_conf} floor={HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR}"
            )
    return failures


def _adb_coverage_thin_flag(aggregate_flags: list[str]) -> str | None:
    """Parse the aggregator's `adb_partial_coverage:{skipped}/{period}` flag.

    Returns a shadow-flag string when ``skipped / period > 10%``, else None.
    The flag name explicitly carries ``would_route_review`` so operators
    can see what the live routing would do after the shadow-mode flip
    (decision-boundary rule from CLAUDE.md).

    Format of the source flag (set in ``parser/aggregate.py``):
        ``adb_partial_coverage:{int}/{int}``

    Returns None on:
      - absent flag (clean coverage, nothing to surface)
      - malformed numerator/denominator (defense against future format drift)
      - zero or negative denominator (math undefined)
      - ratio at or below the threshold (10% is allowed)
    """
    prefix = "adb_partial_coverage:"
    for flag in aggregate_flags:
        if not flag.startswith(prefix):
            continue
        payload = flag[len(prefix) :]
        parts = payload.split("/", maxsplit=1)
        if len(parts) != 2:
            return None
        try:
            skipped = int(parts[0])
            period = int(parts[1])
        except ValueError:
            return None
        if period <= 0:
            return None
        if skipped <= 0:
            return None
        ratio = Decimal(skipped) / Decimal(period)
        if ratio <= ADB_COVERAGE_THIN_RATIO_THRESHOLD:
            return None
        # Quantize to whole-percent for stable rendering; threshold is 10%
        # so single-digit precision is sufficient.
        ratio_pct = int((ratio * Decimal("100")).to_integral_value())
        threshold_pct = int(
            (ADB_COVERAGE_THIN_RATIO_THRESHOLD * Decimal("100")).to_integral_value()
        )
        return (
            f"adb_coverage_thin:skip_ratio={ratio_pct}pct_"
            f"threshold={threshold_pct}pct_would_route_review"
        )
    return None


def _tampering_persistence_flag(
    evaluation: TamperingEvaluation,
    decline_mode: str,
) -> str | None:
    """Render the parse-time visibility flag for a tampering evaluation.

    Plan 5.1 — closes the gap REMAINING_WORK.md called out: the
    tampering composition was computed and audited at score-time,
    but the flag itself only existed on ``PipelineResult.tampering_evaluation``
    — not persisted on the document — so the close-queue gating-reason
    labels and the dossier flag list couldn't see it.

    Returns ``None`` when the evaluation didn't fire (no flag to
    persist). Otherwise returns a single string of the form
    ``"<prefix> bank_statement_tampering_confirmed:<branch>"`` that the
    caller appends to ``all_flags``.

    Prefix is mode-dependent. The flag fires either way; the prefix
    controls whether it surfaces in the close-queue's gating-reason
    label:

    * ``"shadow"`` (default) → ``[SHADOW]`` prefix. The close-queue
      ignores SHADOW prefixes when building gating-reason labels (see
      ``aegis.web.routers.close_queue._gating_reason_labels``), so the
      operator sees the flag on the dossier flag list but the queue
      label is unchanged. Matches the shadow-mode contract: signal
      visible for audit, decision unchanged.
    * ``"live"`` → ``[META]`` prefix. Surfaces via the META category
      as ``"editor metadata"`` on the close-queue. Aligns with the
      live-mode contract: ``tampering_confirmed`` drives
      ``bank_statement_tampering_confirmed`` as a hard decline at
      score-time, and the operator-facing queue label reflects that
      classification.

    NOT a decision-boundary change. The score-time decline gate still
    reads ``deal.tampering_confirmed`` (``score.py:380``), which is
    only set in live mode via the multi-month re-evaluation path. This
    helper is pure visibility — no new auto-decline path.
    """
    if not evaluation.fires:
        return None
    prefix = "[META]" if decline_mode == "live" else "[SHADOW]"
    return f"{prefix} bank_statement_tampering_confirmed:{evaluation.branch}"


def _collect_flags(
    metadata: MetadataAnalysis,
    validation: ValidationResult,
    patterns: PatternAnalysis | None,
    compound: list[str],
) -> list[str]:
    out: list[str] = []
    out.extend(f"[META] {f}" for f in metadata.flags)
    out.extend(f"[MATH] {f}" for f in validation.failures)
    out.extend(f"[WARN] {f}" for f in validation.warnings)
    if patterns:
        out.extend(f"[PATTERN] {p.code}: {p.detail}" for p in patterns.patterns)
    out.extend(f"[COMPOUND] {f}" for f in compound)
    return out


# Public routing helper re-exported above (top-level import for E402).
# ``determine_processor_type`` is the public name callers spell as
# ``aegis.parser.pipeline.determine_processor_type``. None means
# "route to bank pipeline"; "stripe" / "square" means "route to
# processor pipeline".

__all__ = [
    "ADB_COVERAGE_THIN_RATIO_THRESHOLD",
    "CLASSIFICATION_CONFIDENCE_FLOOR",
    "EOF_HARD_DECLINE",
    "FRAUD_WEIGHTS",
    # Backwards-compatible alias for ``TRACK_A_PRESCREEN_THRESHOLD`` —
    # retained so downstream consumers (legacy scoring engine, track_a
    # historical lookback, portfolio analytics) don't break on import.
    "HARD_DECLINE_THRESHOLD",
    "HIGH_IMPACT_CATEGORIES",
    "HIGH_IMPACT_CATEGORY_CONFIDENCE_FLOOR",
    "MAX_OCR_PAGES",
    "REVIEW_THRESHOLD",
    "TRACK_A_PRESCREEN_THRESHOLD",
    "MerchantContext",
    "PipelineResult",
    "determine_processor_type",
    "run_pipeline",
]
