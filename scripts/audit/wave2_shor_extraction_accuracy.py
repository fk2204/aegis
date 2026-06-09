# ruff: noqa: E501, ANN401
# Operator diagnostic / extraction-accuracy audit script: readable single-line
# verdict output is the point (E501 file-wide). The verdict helpers walk LLM-
# returned JSON which is genuinely typing.Any (ANN401 file-wide).
"""Wave 2 accuracy probe — extract Shor Capital from guidelines PNG +
signed ISO PDF, compare against the production row at
``c5f05242-5c85-43ae-8a00-c32e48030a28``.

READ-ONLY. Two Bedrock calls + one Supabase select. Zero writes.

Two extraction passes:
  1. Guidelines PNG  → ``BedrockClient.extract_raw_json_from_images``
  2. ISO PDF         → ``BedrockClient.extract_raw_json``

Both use the SAME ``FUNDER_GUIDELINE_EXTRACTION_PROMPT`` the production
``/ui/funders/import`` route uses — we are measuring the engine, not a
new prompt.

Verdict per field:
  match                 — extraction equals ground truth
  blank_correctly       — both null (LLM honoured "don't guess")
  missed                — ground truth populated, extraction null
  confident_wrong       — extraction differs AND confidence >= 60  (DANGER)
  low_confidence_wrong  — extraction differs AND confidence < 60   (operator catches)
  invented_confidently  — ground truth null, extraction populated, conf >= 60
  invented_low          — ground truth null, extraction populated, conf < 60

For tuple fields (``excluded_industries``, ``conditional_requirements``,
``auto_decline_conditions``, ``excluded_states``) the comparison is
case-insensitive set membership and the report breaks down matched /
missed / extra members.

Usage on the box, with /etc/aegis/aegis.env sourced::

    set -a; source /etc/aegis/aegis.env; set +a
    cd /opt/aegis
    .venv/bin/python scripts/audit/wave2_shor_extraction_accuracy.py \\
        --guidelines-png /tmp/shor_guidelines.png \\
        --iso-pdf        /tmp/shor_iso.pdf
"""

from __future__ import annotations

import argparse
import hashlib
import json as _json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from pydantic import ValidationError

from aegis.funders.extract import (
    FunderExtractionError,
    _coerce_confidence,
    _coerce_draft,
    _coerce_int,
    _coerce_str_list,
)
from aegis.funders.models import FunderGuidelineExtraction, FunderRow
from aegis.funders.prompts import FUNDER_GUIDELINE_EXTRACTION_PROMPT
from aegis.funders.repository import FunderNotFoundError, SupabaseFunderRepository
from aegis.llm import BedrockClient, _text_blocks


def _first_balanced_json_object(text: str) -> dict[str, Any]:
    """Return the FIRST complete JSON object in text — robust to trailing junk.

    The production ``_first_json_object`` does first-{ / last-} which fails
    when Bedrock emits a second object or commentary after the schema
    object. ``raw_decode`` walks the parser to the end of the first
    well-formed object and ignores anything that follows.
    """
    start = text.find("{")
    if start == -1:
        raise FunderExtractionError(f"no JSON object in LLM response: {text[:200]!r}")
    decoder = _json.JSONDecoder()
    try:
        obj, _end = decoder.raw_decode(text[start:])
    except _json.JSONDecodeError as exc:
        raise FunderExtractionError(
            f"raw_decode failed: {exc}; head={text[start:start+200]!r}"
        ) from exc
    if not isinstance(obj, dict):
        raise FunderExtractionError(f"top-level JSON not object: {type(obj).__name__}")
    return obj


def _bedrock_extract_pdf_raw(
    pdf_bytes: bytes, llm: BedrockClient
) -> tuple[dict[str, Any], bool]:
    """Re-implement the PDF document call but with raw_decode JSON parsing.

    Mirrors ``BedrockClient.extract_raw_json`` exactly except for the final
    JSON extraction step — same Bedrock streaming, same content blocks.
    Lets the audit see the ISO PDF response when the production parser
    chokes on trailing content.
    """
    import base64

    with llm._client.messages.stream(
        model=llm._model,
        max_tokens=64000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.b64encode(pdf_bytes).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": FUNDER_GUIDELINE_EXTRACTION_PROMPT},
                ],
            }
        ],
    ) as stream:
        response = stream.get_final_message()
    truncated = getattr(response, "stop_reason", None) == "max_tokens"
    return _first_balanced_json_object(_text_blocks(response)), truncated

SHOR_ID: Final[UUID] = UUID("c5f05242-5c85-43ae-8a00-c32e48030a28")

# Fields the extraction prompt produces + how to compare them.
# (label, kind) where kind ∈ {"scalar", "tuple", "string"}
COMPARED_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("name", "string"),
    ("min_monthly_revenue", "scalar"),
    ("min_avg_daily_balance", "scalar"),
    ("min_credit_score", "scalar"),
    ("min_months_in_business", "scalar"),
    ("max_positions", "scalar"),
    ("accepts_stacking", "scalar"),
    ("min_advance", "scalar"),
    ("max_advance", "scalar"),
    ("max_nsf_tolerance", "scalar"),
    ("typical_factor_low", "scalar"),
    ("typical_factor_high", "scalar"),
    ("typical_holdback_low", "scalar"),
    ("typical_holdback_high", "scalar"),
    ("excluded_industries", "tuple"),
    ("excluded_states", "tuple"),
    ("contact_name", "string"),
    ("contact_phone", "string"),
    ("contact_email", "string"),
    ("submission_email", "string"),
    ("auto_decline_conditions", "tuple"),
    ("conditional_requirements", "tuple"),
)

CONF_THRESHOLD: Final[int] = 60


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Wave 2 Shor extraction-accuracy probe.")
    p.add_argument("--guidelines-png", required=True, type=Path)
    p.add_argument("--iso-pdf", required=True, type=Path)
    return p.parse_args()


def _extract_from_image_bytes(
    png_bytes: bytes, llm: BedrockClient
) -> FunderGuidelineExtraction:
    """Same shape as ``extract_funder_guidelines`` but via the images path,
    using ``raw_decode`` JSON parsing so trailing commentary doesn't crash."""
    import base64

    if not png_bytes:
        raise FunderExtractionError("empty PNG buffer")
    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(png_bytes).decode("ascii"),
            },
        },
        {"type": "text", "text": FUNDER_GUIDELINE_EXTRACTION_PROMPT},
    ]
    with llm._client.messages.stream(
        model=llm._model,
        max_tokens=64000,
        messages=[{"role": "user", "content": content}],  # type: ignore[typeddict-item]
    ) as stream:
        response = stream.get_final_message()
    truncated = getattr(response, "stop_reason", None) == "max_tokens"
    raw = _first_balanced_json_object(_text_blocks(response))
    if truncated:
        raise FunderExtractionError("extraction truncated at max_tokens")
    if "draft" not in raw:
        raise FunderExtractionError(
            f"extraction JSON missing 'draft' key; got keys={sorted(raw.keys())}"
        )
    draft_payload = _coerce_draft(raw["draft"])
    confidence = _coerce_confidence(raw.get("confidence_by_field", {}))
    unparseable = _coerce_str_list(raw.get("unparseable_fragments", []))
    overall = _coerce_int(raw.get("overall_confidence", 0))
    draft_payload["guidelines_extracted_at"] = datetime.now(UTC)
    draft_payload["guidelines_source_pdf_hash"] = hashlib.sha256(png_bytes).hexdigest()
    try:
        draft = FunderRow.model_validate(draft_payload)
    except ValidationError as exc:
        raise FunderExtractionError(f"draft FunderRow failed validation: {exc}") from exc
    return FunderGuidelineExtraction(
        draft=draft,
        confidence_by_field=confidence,
        unparseable_fragments=unparseable,
        overall_confidence=overall,
    )


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (tuple, list, set)) and len(value) == 0:
        return True
    return False


def _scalar_equal(a: Any, b: Any) -> bool:
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        return abs(a - b) < Decimal("0.01")
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        try:
            return abs(Decimal(str(a)) - Decimal(str(b))) < Decimal("0.01")
        except Exception:
            return False
    return bool(a == b)


def _norm_token(s: Any) -> str:
    return str(s).strip().lower()


def _tuple_diff(truth: tuple[Any, ...], extracted: tuple[Any, ...]) -> dict[str, list[str]]:
    truth_set = {_norm_token(x) for x in truth}
    extr_set = {_norm_token(x) for x in extracted}
    return {
        "matched": sorted(truth_set & extr_set),
        "missed": sorted(truth_set - extr_set),
        "extra": sorted(extr_set - truth_set),
    }


def _verdict_scalar(truth: Any, extracted: Any, confidence: int) -> str:
    truth_blank = _is_blank(truth)
    extr_blank = _is_blank(extracted)
    if truth_blank and extr_blank:
        return "blank_correctly"
    if truth_blank and not extr_blank:
        return "invented_confidently" if confidence >= CONF_THRESHOLD else "invented_low"
    if not truth_blank and extr_blank:
        return "missed"
    if _scalar_equal(truth, extracted):
        return "match"
    return "confident_wrong" if confidence >= CONF_THRESHOLD else "low_confidence_wrong"


def _verdict_tuple(
    truth: tuple[Any, ...], extracted: tuple[Any, ...], confidence: int
) -> tuple[str, dict[str, list[str]]]:
    truth_blank = _is_blank(truth)
    extr_blank = _is_blank(extracted)
    if truth_blank and extr_blank:
        return "blank_correctly", {"matched": [], "missed": [], "extra": []}
    diff = _tuple_diff(truth, extracted)
    if truth_blank and not extr_blank:
        return (
            "invented_confidently" if confidence >= CONF_THRESHOLD else "invented_low"
        ), diff
    if not truth_blank and extr_blank:
        return "missed", diff
    if not diff["missed"] and not diff["extra"]:
        return "match", diff
    # partial: if the extracted set covers most of truth and has few extras,
    # call it partial_match; otherwise wrong.
    if not diff["missed"] and diff["extra"]:
        # superset — captured all known + extras (often acceptable)
        return ("match_with_extras", diff)
    return (
        ("confident_wrong" if confidence >= CONF_THRESHOLD else "low_confidence_wrong"),
        diff,
    )


def _fmt(value: Any, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "tuple":
        if not value:
            return "—"
        return f"{len(value)} items"
    if isinstance(value, Decimal):
        return f"{value}"
    if isinstance(value, str):
        if not value.strip():
            return "—"
        return value if len(value) <= 32 else value[:29] + "..."
    return str(value)


def _short_field(name: str) -> str:
    return name if len(name) <= 28 else name[:25] + "..."


_VERDICT_GLYPHS: Final[dict[str, str]] = {
    "match":                "OK ",
    "match_with_extras":    "OK+",
    "blank_correctly":      "BLK",
    "missed":               "MIS",
    "confident_wrong":      "!! ",  # DANGER
    "low_confidence_wrong": "?? ",
    "invented_confidently": "INV",  # DANGER
    "invented_low":         "inv",
}


def _report_pass(
    label: str,
    ground_truth: FunderRow,
    extraction: FunderGuidelineExtraction,
) -> dict[str, int]:
    """Print one extraction's verdict table; return verdict-tally for summary."""
    print()
    print("=" * 110)
    print(f"  EXTRACTION PASS: {label}")
    print(f"  overall_confidence = {extraction.overall_confidence}/100")
    if extraction.unparseable_fragments:
        print(f"  unparseable fragments: {len(extraction.unparseable_fragments)}")
        for frag in extraction.unparseable_fragments[:5]:
            print(f"    · {frag[:100]}")
    print("=" * 110)
    print(
        f"{'verdict':4} {'field':28}  {'ground truth':32}  "
        f"{'extracted':32}  conf"
    )
    print("-" * 110)

    tally: dict[str, int] = {}
    draft = extraction.draft
    for fname, kind in COMPARED_FIELDS:
        truth = getattr(ground_truth, fname)
        got = getattr(draft, fname)
        conf = extraction.confidence_by_field.get(fname, 0)
        if kind == "tuple":
            verdict, diff = _verdict_tuple(
                truth if isinstance(truth, tuple) else tuple(truth or ()),
                got if isinstance(got, tuple) else tuple(got or ()),
                conf,
            )
        else:
            verdict = _verdict_scalar(truth, got, conf)
            diff = {}
        tally[verdict] = tally.get(verdict, 0) + 1
        glyph = _VERDICT_GLYPHS.get(verdict, "???")
        print(
            f"{glyph:4} {_short_field(fname):28}  "
            f"{_fmt(truth, kind):32}  "
            f"{_fmt(got, kind):32}  "
            f"{conf:3d}"
        )
        # Show tuple-diff detail for impactful fields.
        if kind == "tuple" and (diff.get("missed") or diff.get("extra")):
            if diff["missed"]:
                print(f"     missed ({len(diff['missed'])}): " + ", ".join(diff["missed"][:6]))
            if diff["extra"]:
                print(f"     extra  ({len(diff['extra'])}): " + ", ".join(diff["extra"][:6]))

    print("-" * 110)
    print("  tally:", ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return tally


def _spotlight(
    label: str,
    ground_truth: FunderRow,
    extraction: FunderGuidelineExtraction,
) -> None:
    """Operator-flagged high-impact fields: factor band + excluded industries."""
    print()
    print(f"  --- spotlight on {label} ---")
    d = extraction.draft
    conf = extraction.confidence_by_field

    print(
        f"  factor_low : truth={ground_truth.typical_factor_low} "
        f"got={d.typical_factor_low} conf={conf.get('typical_factor_low', 0)}"
    )
    print(
        f"  factor_high: truth={ground_truth.typical_factor_high} "
        f"got={d.typical_factor_high} conf={conf.get('typical_factor_high', 0)}"
    )
    diff_ind = _tuple_diff(
        tuple(ground_truth.excluded_industries),
        tuple(d.excluded_industries),
    )
    print(
        f"  excluded_industries: truth={len(ground_truth.excluded_industries)} "
        f"got={len(d.excluded_industries)} "
        f"conf={conf.get('excluded_industries', 0)} "
        f"matched={len(diff_ind['matched'])} "
        f"missed={len(diff_ind['missed'])} extra={len(diff_ind['extra'])}"
    )


def _print_grand_summary(
    tallies: dict[str, dict[str, int]],
) -> None:
    print()
    print("=" * 110)
    print("  GRAND SUMMARY")
    print("=" * 110)
    all_verdicts = sorted({k for t in tallies.values() for k in t})
    header = f"  {'verdict':24}"
    for pass_label in tallies:
        header += f"  {pass_label:>18}"
    print(header)
    print("-" * 110)
    for v in all_verdicts:
        row = f"  {v:24}"
        for pass_label in tallies:
            row += f"  {tallies[pass_label].get(v, 0):>18d}"
        print(row)
    print()
    print("  DANGER bucket  = confident_wrong + invented_confidently")
    print("  SAFE bucket    = match + match_with_extras + blank_correctly + low_confidence_wrong + invented_low + missed")
    print("                   (the 'missed' and 'low_*' ones are caught by operator review;")
    print("                    only 'confident_wrong' and 'invented_confidently' represent silent failures.)")


def main() -> int:
    args = _parse_args()
    if not args.guidelines_png.exists():
        print(f"ERROR: guidelines PNG not found: {args.guidelines_png}", file=sys.stderr)
        return 2
    if not args.iso_pdf.exists():
        print(f"ERROR: ISO PDF not found: {args.iso_pdf}", file=sys.stderr)
        return 2

    print(f"reading {args.guidelines_png} ({args.guidelines_png.stat().st_size} bytes)")
    png_bytes = args.guidelines_png.read_bytes()
    print(f"reading {args.iso_pdf} ({args.iso_pdf.stat().st_size} bytes)")
    pdf_bytes = args.iso_pdf.read_bytes()

    repo = SupabaseFunderRepository()
    try:
        ground_truth = repo.get(SHOR_ID)
    except FunderNotFoundError:
        print(f"ERROR: Shor Capital row {SHOR_ID} not in prod funders table", file=sys.stderr)
        return 3
    print(f"ground truth: name={ground_truth.name!r}  id={ground_truth.id}")

    llm = BedrockClient()

    print("\nrunning extract_raw_json_from_images on guidelines PNG ...")
    try:
        guidelines_extraction = _extract_from_image_bytes(png_bytes, llm)
    except FunderExtractionError as exc:
        print(f"  guidelines PNG extraction FAILED: {exc}", file=sys.stderr)
        guidelines_extraction = None

    print("\nrunning extract_raw_json on ISO PDF (audit JSON parser) ...")
    try:
        raw, truncated = _bedrock_extract_pdf_raw(pdf_bytes, llm)
        if truncated:
            raise FunderExtractionError("extraction truncated at max_tokens")
        if "draft" not in raw:
            raise FunderExtractionError(
                f"extraction JSON missing 'draft' key; got keys={sorted(raw.keys())}"
            )
        _draft_payload = _coerce_draft(raw["draft"])
        _draft_payload["guidelines_extracted_at"] = datetime.now(UTC)
        _draft_payload["guidelines_source_pdf_hash"] = hashlib.sha256(pdf_bytes).hexdigest()
        iso_extraction = FunderGuidelineExtraction(
            draft=FunderRow.model_validate(_draft_payload),
            confidence_by_field=_coerce_confidence(raw.get("confidence_by_field", {})),
            unparseable_fragments=_coerce_str_list(raw.get("unparseable_fragments", [])),
            overall_confidence=_coerce_int(raw.get("overall_confidence", 0)),
        )
    except (FunderExtractionError, ValidationError) as exc:
        print(f"  ISO PDF extraction FAILED: {exc}", file=sys.stderr)
        iso_extraction = None

    tallies: dict[str, dict[str, int]] = {}
    if guidelines_extraction is not None:
        tallies["guidelines PNG"] = _report_pass(
            "guidelines PNG (vision)", ground_truth, guidelines_extraction
        )
        _spotlight("guidelines PNG", ground_truth, guidelines_extraction)
    if iso_extraction is not None:
        tallies["ISO PDF"] = _report_pass("ISO PDF (document)", ground_truth, iso_extraction)
        _spotlight("ISO PDF", ground_truth, iso_extraction)

    if tallies:
        _print_grand_summary(tallies)

    return 0


if __name__ == "__main__":
    sys.exit(main())
