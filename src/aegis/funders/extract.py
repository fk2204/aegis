"""Funder guideline extraction — PDF / image in, FunderGuidelineExtraction out.

Two entry points:

* `extract_funder_guidelines(pdf_bytes, llm)` — PDF route. Sends the
  document block + prompt to Bedrock.
* `extract_funder_guidelines_from_image(png_bytes, llm)` — PNG/JPEG
  route for funders who publish their criteria as a single-page
  screenshot (the Shor Capital case). The vision pass is gated by the
  same prompt and the same per-field confidence schema.

For the multi-doc case (guidelines PNG + signed ISO PDF together — the
Shor case loses half the picture if only one is submitted),
`merge_extractions` field-merges per-doc extractions with
highest-confidence-wins on scalars and union-of-sets on tuple fields.

The LLM client is injected (`LLMClient` Protocol) so tests can stub canned
responses. Production wiring uses `BedrockClient` from `aegis.llm`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError

from aegis.funders.models import FunderGuidelineExtraction, FunderRow
from aegis.funders.prompts import FUNDER_GUIDELINE_EXTRACTION_PROMPT
from aegis.llm import LLMClient

_MAX_PDF_BYTES: Final[int] = 25 * 1024 * 1024
_MAX_IMAGE_BYTES: Final[int] = 25 * 1024 * 1024

# Bedrock's per-image hard limit is 5 MB after base64 encoding. Base64
# inflates bytes by ~4/3, so any raw image over ~3.75 MB hits the limit.
# Cap raw input at 4.5 MB; downsample iteratively until under it. 70%
# linear (~0.49 area) per pass with LANCZOS keeps text legible at the
# scales we hit in practice (Logic Advance's 6.5 MB PNG resolved in one
# pass — 2026-06-18). Five passes (0.7**5 ~ 0.17) is the safety cap;
# beyond that text would be too small for the LLM to read anyway.
_BEDROCK_IMAGE_BYTES_SOFT_CAP: Final[int] = 4_500_000
_DOWNSAMPLE_SCALE: Final[float] = 0.7
_DOWNSAMPLE_MAX_PASSES: Final[int] = 5


class FunderExtractionError(RuntimeError):
    """Raised when the LLM response cannot be parsed into FunderGuidelineExtraction."""


def extract_funder_guidelines(
    pdf_bytes: bytes,
    llm: LLMClient,
) -> FunderGuidelineExtraction:
    """Extract a funder's underwriting criteria from a guideline PDF."""
    if len(pdf_bytes) == 0:
        raise FunderExtractionError("empty PDF buffer")
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        raise FunderExtractionError(
            f"PDF buffer too large: {len(pdf_bytes)} bytes (max {_MAX_PDF_BYTES})"
        )

    try:
        raw, truncated = llm.extract_raw_json(pdf_bytes, FUNDER_GUIDELINE_EXTRACTION_PROMPT)
    except ValueError as exc:
        raise FunderExtractionError(f"LLM returned malformed JSON: {exc}") from exc

    return _build_extraction(raw, truncated, source_bytes=pdf_bytes)


def extract_funder_guidelines_from_image(
    image_bytes: bytes,
    llm: LLMClient,
) -> FunderGuidelineExtraction:
    """Extract a funder's underwriting criteria from a PNG/JPEG screenshot.

    Used when the funder publishes criteria as an image (a Gmail
    screenshot of the guideline block, a slide deck export, etc.).
    Routes to `LLMClient.extract_raw_json_from_images` with a one-image
    list — the LLM call shape matches the parser's OCR fallback so the
    BedrockClient does not need a new code path.
    """
    if len(image_bytes) == 0:
        raise FunderExtractionError("empty image buffer")
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise FunderExtractionError(
            f"image buffer too large: {len(image_bytes)} bytes (max {_MAX_IMAGE_BYTES})"
        )

    image_bytes = _downsample_if_oversized(image_bytes)

    try:
        raw, truncated = llm.extract_raw_json_from_images(
            [image_bytes], FUNDER_GUIDELINE_EXTRACTION_PROMPT
        )
    except ValueError as exc:
        raise FunderExtractionError(f"LLM returned malformed JSON: {exc}") from exc

    return _build_extraction(raw, truncated, source_bytes=image_bytes)


def _downsample_if_oversized(image_bytes: bytes) -> bytes:
    """Shrink the image until it fits Bedrock's 5 MB base64 ceiling.

    No-op when the input is already under the soft cap. Otherwise opens
    via Pillow, resizes ``_DOWNSAMPLE_SCALE`` linear per pass with
    ``LANCZOS`` resampling, re-encodes in the original format
    (defaulting to PNG when Pillow can't infer one), and returns the
    smallest pass that lands under the cap. Returns the last pass after
    ``_DOWNSAMPLE_MAX_PASSES`` even if it still exceeds the cap — the
    Bedrock call will then surface the size error so the operator
    knows the source is unusable rather than this routine silently
    eating the input.

    Pillow ships as a transitive dependency of ``weasyprint`` /
    ``reportlab`` and is always present in the install. The import is
    deferred so the module-level import graph stays unchanged for the
    PDF path that never needs it.
    """
    if len(image_bytes) <= _BEDROCK_IMAGE_BYTES_SOFT_CAP:
        return image_bytes

    import io

    from PIL import Image

    # ``Image.open`` returns ``ImageFile.ImageFile``; the resize loop below
    # reassigns to a plain ``Image.Image``. Annotate the binding once so
    # the assignment type-checks under ``mypy --strict``.
    image: Image.Image = Image.open(io.BytesIO(image_bytes))
    # Force a read so format detection finalises before we resize.
    image.load()
    fmt = image.format or "PNG"
    save_kwargs: dict[str, Any] = {"optimize": True}
    if fmt == "JPEG":
        save_kwargs["quality"] = 90

    result = image_bytes
    for _ in range(_DOWNSAMPLE_MAX_PASSES):
        new_size = (
            max(1, int(image.width * _DOWNSAMPLE_SCALE)),
            max(1, int(image.height * _DOWNSAMPLE_SCALE)),
        )
        image = image.resize(new_size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, format=fmt, **save_kwargs)
        result = buf.getvalue()
        if len(result) <= _BEDROCK_IMAGE_BYTES_SOFT_CAP:
            return result

    return result


def _build_extraction(
    raw: dict[str, Any],
    truncated: bool,
    *,
    source_bytes: bytes,
) -> FunderGuidelineExtraction:
    """Shared shape: validate the raw extraction dict into a FunderGuidelineExtraction.

    Both the PDF and image entry points end up here once the LLM has
    returned its JSON. Keeping the validation in one place avoids the
    pre-Wave-2 bug where the PDF path stamped provenance and the image
    path would skip it.
    """
    if truncated:
        raise FunderExtractionError(
            "LLM extraction was truncated at max_tokens — funder guideline "
            "document exceeds the model's output budget; try a smaller "
            "document or summary."
        )

    if "draft" not in raw:
        raise FunderExtractionError(
            f"extraction JSON missing 'draft' key; got keys={sorted(raw.keys())}"
        )

    draft_payload = _coerce_draft(raw["draft"])
    confidence = _coerce_confidence(raw.get("confidence_by_field", {}))
    unparseable = _coerce_str_list(raw.get("unparseable_fragments", []))
    overall = _coerce_int(raw.get("overall_confidence", 0))

    # Stamp provenance: when extraction ran + a hash of the source bytes
    # (PDF or image) so re-extraction is detectable. The column name
    # remains `guidelines_source_pdf_hash` for backward compatibility —
    # the value is still a SHA-256 hex digest, just over the image bytes
    # for the image path.
    draft_payload["guidelines_extracted_at"] = datetime.now(UTC)
    draft_payload["guidelines_source_pdf_hash"] = hashlib.sha256(source_bytes).hexdigest()

    try:
        draft = FunderRow.model_validate(draft_payload)
    except ValidationError as exc:
        raise FunderExtractionError(f"draft FunderRow failed validation: {exc}") from exc

    try:
        return FunderGuidelineExtraction(
            draft=draft,
            confidence_by_field=confidence,
            unparseable_fragments=unparseable,
            overall_confidence=overall,
        )
    except ValidationError as exc:
        raise FunderExtractionError(f"FunderGuidelineExtraction validation: {exc}") from exc


# -- merge ------------------------------------------------------------------

# FunderRow scalar fields that participate in the merge. Anything not
# listed is taken from the first extraction (name) or recomputed from
# the source bytes (provenance hashes/timestamps).
_MERGE_SCALAR_FIELDS: Final[tuple[str, ...]] = (
    "min_monthly_revenue",
    "min_avg_daily_balance",
    "min_credit_score",
    "min_months_in_business",
    "max_positions",
    "accepts_stacking",
    "min_advance",
    "max_advance",
    "max_nsf_tolerance",
    "typical_factor_low",
    "typical_factor_high",
    "typical_holdback_low",
    "typical_holdback_high",
    "funding_velocity_days",
    "contact_name",
    "contact_phone",
    "contact_email",
    "submission_email",
)

# Tuple-shaped fields that union (case-insensitive de-dupe).
_MERGE_TUPLE_FIELDS: Final[tuple[str, ...]] = (
    "excluded_industries",
    "excluded_states",
    "deal_types_accepted",
    "preferred_states",
    "auto_decline_conditions",
    "conditional_requirements",
)


def merge_extractions(
    parts: Sequence[FunderGuidelineExtraction],
) -> FunderGuidelineExtraction:
    """Merge per-doc extractions field-by-field.

    Merge rules:
      * Scalar fields: highest per-field confidence wins. Ties go to the
        earliest part (stable). A part that has confidence 0 for a field
        never wins.
      * Tuple fields (`excluded_industries`, `excluded_states`,
        `auto_decline_conditions`, `conditional_requirements`,
        `tiers`): union the entries. Strings de-duped case-insensitively
        (preserving the first casing seen). Tiers de-duped by name.
      * `notes_residual`: concatenated (deduped, newline-joined).
      * `unparseable_fragments`: concatenated, order-preserving dedupe.
      * `name`: first non-empty, non-"Unknown Funder" value, else
        falls back to the first part's name.
      * `confidence_by_field`: per-key max across contributing parts.
      * `overall_confidence`: max across parts.
      * Provenance (`guidelines_extracted_at`,
        `guidelines_source_pdf_hash`): taken from the most-recent part
        by `guidelines_extracted_at`.

    Raises FunderExtractionError on an empty sequence — the caller
    should have caught that before reaching merge time.
    """
    if not parts:
        raise FunderExtractionError("merge_extractions requires at least one part")
    if len(parts) == 1:
        return parts[0]

    base = parts[0]
    base_dump = base.draft.model_dump()
    merged_payload: dict[str, Any] = dict(base_dump)

    # Name: first non-empty, non-placeholder value across parts.
    merged_payload["name"] = _pick_name(parts)

    # Scalars: max confidence wins.
    for field in _MERGE_SCALAR_FIELDS:
        winner_part, winner_conf = _pick_winner(parts, field)
        if winner_part is None or winner_conf == 0:
            # No part reported confidence > 0 for this field — keep base
            # value (which may itself be None / "" / False).
            continue
        merged_payload[field] = winner_part.draft.model_dump()[field]

    # Tuples: union with case-insensitive dedupe.
    for field in _MERGE_TUPLE_FIELDS:
        merged_payload[field] = _union_tuple(parts, field)

    # Tiers: union by name (case-insensitive).
    merged_payload["tiers"] = _union_tiers(parts)

    # Residual notes: concatenate distinct non-empty blocks.
    merged_payload["notes_residual"] = _concat_notes(parts)

    # Provenance: take the latest extraction stamp + its hash. Falls
    # back to first part if none have a stamp.
    latest = max(
        parts,
        key=lambda p: p.draft.guidelines_extracted_at or datetime.min.replace(tzinfo=UTC),
    )
    merged_payload["guidelines_extracted_at"] = latest.draft.guidelines_extracted_at
    merged_payload["guidelines_source_pdf_hash"] = latest.draft.guidelines_source_pdf_hash

    try:
        merged_draft = FunderRow.model_validate(merged_payload)
    except ValidationError as exc:
        raise FunderExtractionError(f"merged FunderRow failed validation: {exc}") from exc

    merged_confidence = _merge_confidence(parts)
    merged_unparseable = _merge_unparseable(parts)
    merged_overall = max(p.overall_confidence for p in parts)

    try:
        return FunderGuidelineExtraction(
            draft=merged_draft,
            confidence_by_field=merged_confidence,
            unparseable_fragments=merged_unparseable,
            overall_confidence=merged_overall,
        )
    except ValidationError as exc:
        raise FunderExtractionError(f"merged FunderGuidelineExtraction validation: {exc}") from exc


def _pick_name(parts: Sequence[FunderGuidelineExtraction]) -> str:
    """Pick the first non-placeholder funder name across parts."""
    for p in parts:
        n = p.draft.name.strip()
        if n and n.lower() != "unknown funder":
            return n
    return parts[0].draft.name


def _pick_winner(
    parts: Sequence[FunderGuidelineExtraction],
    field: str,
) -> tuple[FunderGuidelineExtraction | None, int]:
    """Find the part with the highest confidence for `field`.

    Returns (winning_part, winning_confidence). Ties resolve to the
    earliest part. If no part has a positive confidence for the field,
    returns (None, 0).
    """
    best_part: FunderGuidelineExtraction | None = None
    best_conf = -1
    for p in parts:
        conf = p.confidence_by_field.get(field, 0)
        if conf > best_conf:
            best_conf = conf
            best_part = p
    if best_conf <= 0:
        return None, 0
    return best_part, best_conf


def _union_tuple(
    parts: Sequence[FunderGuidelineExtraction],
    field: str,
) -> tuple[str, ...]:
    """Union the tuple-typed `field` across parts; case-insensitive dedupe."""
    seen_lower: set[str] = set()
    out: list[str] = []
    for p in parts:
        values = getattr(p.draft, field)
        if not isinstance(values, tuple):
            continue
        for v in values:
            if not isinstance(v, str):
                continue
            key = v.strip().lower()
            if not key or key in seen_lower:
                continue
            seen_lower.add(key)
            out.append(v.strip())
    return tuple(out)


def _union_tiers(parts: Sequence[FunderGuidelineExtraction]) -> tuple[Any, ...]:
    """Union FunderTier entries across parts, deduped by tier name."""
    seen_lower: set[str] = set()
    out: list[Any] = []
    for p in parts:
        for tier in p.draft.tiers:
            key = tier.name.strip().lower()
            if not key or key in seen_lower:
                continue
            seen_lower.add(key)
            out.append(tier)
    return tuple(out)


def _concat_notes(parts: Sequence[FunderGuidelineExtraction]) -> str:
    """Concatenate `notes_residual` blocks across parts, deduped + newline-joined."""
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        block = p.draft.notes_residual.strip()
        if not block or block in seen:
            continue
        seen.add(block)
        out.append(block)
    return "\n\n".join(out)


def _merge_confidence(
    parts: Sequence[FunderGuidelineExtraction],
) -> dict[str, int]:
    """Per-key max confidence across parts."""
    out: dict[str, int] = {}
    for p in parts:
        for key, value in p.confidence_by_field.items():
            if value > out.get(key, -1):
                out[key] = value
    return out


def _merge_unparseable(
    parts: Sequence[FunderGuidelineExtraction],
) -> list[str]:
    """Concatenate unparseable_fragments across parts, order-preserving dedupe."""
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        for fragment in p.unparseable_fragments:
            key = fragment.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(fragment)
    return out


# -- coercion helpers --------------------------------------------------------


# Decimal-bearing fields inside each tier dict that need string coercion
# before Pydantic validation (same float-avoidance rule as top-level money).
_TIER_DECIMAL_KEYS: Final[tuple[str, ...]] = (
    "buy_rate_low",
    "buy_rate_high",
    "min_monthly_revenue",
    "max_advance",
    "max_holdback",
)


def _coerce_draft(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FunderExtractionError(f"draft must be an object, got {type(value).__name__}")
    out: dict[str, Any] = dict(value)

    # Top-level money / Decimal fields: convert numbers to strings so
    # Pydantic Decimal stays float-free.
    for key in (
        "min_monthly_revenue",
        "min_avg_daily_balance",
        "min_advance",
        "max_advance",
        "typical_factor_low",
        "typical_factor_high",
        "typical_holdback_low",
        "typical_holdback_high",
    ):
        if key in out and out[key] is not None:
            out[key] = _num_to_str(out[key])

    # Tuple-shaped string-list fields.
    for key in (
        "excluded_industries",
        "excluded_states",
        "deal_types_accepted",
        "preferred_states",
        "auto_decline_conditions",
        "conditional_requirements",
    ):
        seq = out.get(key)
        if seq is None:
            out[key] = ()
        elif isinstance(seq, list):
            out[key] = tuple(str(v) for v in seq)

    # Tiers: list of dicts. Each tier's Decimal-bearing fields get the
    # same str-coercion treatment; the list becomes a tuple. Malformed
    # tier entries pass through and surface as Pydantic ValidationError
    # when FunderRow.tiers is validated downstream.
    tiers_raw = out.get("tiers")
    if tiers_raw is None:
        out["tiers"] = ()
    elif isinstance(tiers_raw, list):
        out["tiers"] = tuple(_coerce_tier(t) for t in tiers_raw)

    # Contact string fields default to empty string if missing.
    for key in (
        "contact_name",
        "contact_phone",
        "contact_email",
        "submission_email",
        "notes_residual",
    ):
        if out.get(key) is None:
            out[key] = ""

    # `name` falls back to "Unknown Funder" if missing — operator must
    # rename before saving, but extraction shouldn't fail validation here.
    if not out.get("name"):
        out["name"] = "Unknown Funder"

    return out


def _coerce_tier(value: object) -> dict[str, Any]:
    """Coerce one tier dict: stringify Decimal fields, leave the rest alone.

    Non-dict entries pass through unchanged; Pydantic surfaces the type
    error at FunderTier validation, where the operator sees which tier
    row was malformed.
    """
    if not isinstance(value, dict):
        return value  # type: ignore[return-value]
    out: dict[str, Any] = dict(value)
    for key in _TIER_DECIMAL_KEYS:
        if key in out and out[key] is not None:
            out[key] = _num_to_str(out[key])
    return out


def _coerce_confidence(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            continue
        out[key] = _coerce_int(raw)
    return out


def _coerce_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v is not None]


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, min(100, value))
    if isinstance(value, float):
        return max(0, min(100, int(value)))
    if isinstance(value, str):
        try:
            return max(0, min(100, int(float(value))))
        except ValueError:
            return 0
    return 0


def _num_to_str(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


__all__ = [
    "FunderExtractionError",
    "extract_funder_guidelines",
    "extract_funder_guidelines_from_image",
    "merge_extractions",
]
