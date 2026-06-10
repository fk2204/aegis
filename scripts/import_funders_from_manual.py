# ruff: noqa: RUF001, RUF002, RUF003
# Em-dashes are intentional — the source MCA Funder Manual uses them in
# free-form descriptions and we preserve verbatim text. The ruff RUF00x
# "ambiguous dash" check is silenced file-wide for that reason.
"""One-shot funder-catalog import from Filip's internal MCA Funder Manual.

Deterministic parse of the manual's per-funder sections (no LLM needed —
the document is operator-curated and already structured). Outputs one
JSON preview per direct funder so the operator can confirm field values
before they land in the funders table.

Skips §8 Splash Advance, §9 Big Think Capital, §10 Bizi Connect — per
the manual itself: "Splash Advance (§8) is a broker/aggregator, Big
Think (§9) is a 35% commission-split affiliate, and Bizi Connect (§10)
is a loan marketplace — these three do not publish underwriting boxes."

After operator confirms the previews, generate migration 046 (or use
the existing /ui/funders/import save path) to insert the rows.

Usage:
    python scripts/import_funders_from_manual.py \
        --manual "C:\\Users\\fkozi\\Downloads\\MCA_Funder_Manual (4).docx" \
        --out /tmp/aegis-funder-previews

Per .claude/rules/operating-principles.md #4: this script DOES NOT
write to the production database. It produces previews. The operator
reviews, then explicitly authorizes the migration write.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Subsection-header stop patterns. When in_excluded / in_auto_decline /
# in_excluded_states is True, encountering one of these resets the flag
# so we stop appending to the current list.
_STOP_PATTERNS: tuple[str, ...] = (
    "how to treat",
    "conditional",
    "commission structure",
    "commission, bonuses",
    "submission process",
    "submission —",
    "submission:",
    "submission ",
    "required documents",
    "required for funding",
    "required to submit",
    "payment timing",
    "payment trigger",
    "payment schedule",
    "clawback policy",
    "clawback window",
    "clawback scope",
    "operational gotchas",
    "per-product underwriting",
    "products & commission",
    "standard stipulations",
    "merchant payment options",
    "psf fee policy",
    "pricing & holdback",
    "the four tiers",
    "tier ladder",
    "pricing ladder",
    "schedule a",
    "schedule b",
    "exhibit a",
    "exhibit b",
    "renewal",
    "monthly volume bonus",
    "deal submissions:",
    "rep —",
    "rep -",
    "operational notes",
    "merchant information sheet",
    "ach offset",
    "ach set-off",
)

_START_EXCLUDED: tuple[str, ...] = (
    "restricted industries",
    "prohibited industries",
    "do not submit",
    "restricted / declined industries",
)
_START_AUTO_DECLINE: tuple[str, ...] = (
    "auto declines",
    "auto-declines",
    "auto decline",
    "auto-decline",
    "common declines",
    "decline triggers",
)
_START_EXCLUDED_STATES: tuple[str, ...] = (
    "restricted states",
    "prohibited states",
    "do not fund states",
    "excluded states",
)


def _is_subsection_header(line: str) -> bool:
    """True if `line` looks like a subsection header that should end any
    currently-active list (excluded/auto-decline/excluded-states)."""
    ll = line.lower().strip()
    if any(ll.startswith(s) for s in _STOP_PATTERNS):
        return True
    # Title-cased short line ending with colon
    if (
        line.endswith(":")
        and len(line) < 50
        and any(c.isupper() for c in line[:5])
    ):
        return True
    return False


def _is_list_header(line: str, ll: str, triggers: tuple[str, ...]) -> bool:
    """True if `line` looks like a list-start header. Guards:
       (1) short (<60 chars)
       (2) starts with the trigger phrase
       (3) no mid-string colon (headers use em-dash or bare label;
           inline emphasis like "Do NOT submit: 1st position deals"
           in 'How to Treat' prose uses colon and would otherwise
           falsely re-enter the excluded list).
    """
    if len(line) >= 60:
        return False
    if ":" in line and not line.rstrip().endswith(":"):
        return False
    return any(ll.startswith(t) for t in triggers)


@dataclass
class FunderPreview:
    """Subset of FunderRow fields populated from the manual. Operator
    reviews this JSON; matched fields land in migration 046 (or via the
    existing import save endpoint)."""

    name: str
    section_number: int
    # Underwriting box (Quick Reference + section detail)
    min_months_in_business: int | None = None
    min_monthly_revenue: Decimal | None = None
    min_credit_score: int | None = None
    min_advance: Decimal | None = None
    max_advance: Decimal | None = None
    max_positions: int | None = None
    accepts_stacking: bool | None = None
    excluded_industries: list[str] = field(default_factory=list)
    excluded_states: list[str] = field(default_factory=list)
    typical_factor_low: Decimal | None = None
    typical_factor_high: Decimal | None = None
    typical_holdback_low: Decimal | None = None
    typical_holdback_high: Decimal | None = None
    requires_coj: bool | None = None
    charges_merchant_advance_fees: bool | None = None
    # Free-form context
    notes_residual: str = ""
    auto_decline_conditions: list[str] = field(default_factory=list)
    conditional_requirements: list[str] = field(default_factory=list)
    submission_email: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    # Operator review hints
    needs_operator_review: list[str] = field(default_factory=list)


def _read_paragraphs(docx_path: Path) -> list[str]:
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    root = ET.fromstring(xml)  # noqa: S314 — trusted: operator-supplied local docx
    out: list[str] = []
    for p in root.iter(_WORD_NS + "p"):
        text = "".join((t.text or "") for t in p.iter(_WORD_NS + "t")).strip()
        if text:
            out.append(text)
    return out


def _locate_sections(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return (section_start_idx, section_number, section_title) for each
    numbered section."""
    out: list[tuple[int, int, str]] = []
    pat = re.compile(r"^(\d+)\.\s+(.+)")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m and len(line) < 80:
            out.append((i, int(m.group(1)), m.group(2).strip()))
    return out


def _decimal(text: str) -> Decimal | None:
    """Parse '$25K' / '$1.5M' / '$2,000,000' / '12,500' to Decimal."""
    if not text:
        return None
    t = text.strip().replace("$", "").replace(",", "").lower()
    # Strip trailing parentheticals like "$25K (High-Risk)"
    t = re.sub(r"\s*\([^)]+\)\s*$", "", t).strip()
    multiplier = Decimal(1)
    if t.endswith("k"):
        multiplier = Decimal(1000)
        t = t[:-1]
    elif t.endswith("m") or t.endswith("mm"):
        multiplier = Decimal(1_000_000)
        t = t.rstrip("m")
    elif t.endswith("b"):
        multiplier = Decimal(1_000_000_000)
        t = t[:-1]
    try:
        return (Decimal(t) * multiplier).quantize(Decimal("0.01"))
    except Exception:
        return None


def _months(text: str) -> int | None:
    """Parse '12+ months' / '6+ mo' / '1+ year' to integer months."""
    if not text:
        return None
    t = text.lower()
    m = re.search(r"(\d+)\s*\+?\s*(year|yr|mo|month)", t)
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2)
    if unit.startswith("y"):
        return val * 12
    return val


def _fico(text: str) -> int | None:
    if not text:
        return None
    m = re.search(r"\b(\d{3})\b", text)
    return int(m.group(1)) if m else None


def _factor_range(text: str) -> tuple[Decimal | None, Decimal | None]:
    """Parse '1.37+' / '1.45 – 1.49' / 'Buy 1.25 – 1.37' / 'Sell 1.45 – 1.49'."""
    if not text:
        return None, None
    # Strip Buy/Sell prefix
    t = re.sub(r"^\s*(buy|sell)\s+", "", text, flags=re.I).strip()
    # Range with dash variants
    m = re.search(r"(\d\.\d{1,3})\s*[–\-—]\s*(\d\.\d{1,3})", t)
    if m:
        return Decimal(m.group(1)), Decimal(m.group(2))
    # Single value with +
    m = re.search(r"(\d\.\d{1,3})\s*\+", t)
    if m:
        v = Decimal(m.group(1))
        return v, None
    # Single value
    m = re.search(r"(\d\.\d{1,3})", t)
    if m:
        v = Decimal(m.group(1))
        return v, v
    return None, None


def _positions(text: str) -> tuple[int | None, bool | None]:
    """Parse 'Up to 5' / '1st – 4th' / 'Max 2 (MCA)' / '2nd – 8th (no 1st)'.
    Returns (max_positions, accepts_stacking)."""
    if not text:
        return None, None
    t = text.lower()
    # "Up to N"
    m = re.search(r"up to\s+(\d+)", t)
    if m:
        n = int(m.group(1))
        return n, n > 1
    # "Max N"
    m = re.search(r"max\s+(\d+)", t)
    if m:
        n = int(m.group(1))
        return n, n > 1
    # "1st – 4th" pattern
    m = re.search(r"(\d+)(?:st|nd|rd|th)\s*[–\-—]\s*(\d+)(?:st|nd|rd|th)", t)
    if m:
        n = int(m.group(2))
        return n, True  # multi-position == stacking
    # "2nd & up only"
    if "2nd" in t and "only" in t:
        return None, True  # explicit subordinate-only; no max declared
    return None, None


def _parse_quick_reference(lines: list[str]) -> dict[str, dict[str, str]]:
    """Quick Reference table is rendered as flat lines (header row +
    data rows interleaved). This identifies funder names and pairs each
    with the next N cells matching the standard column order.

    Standard columns per the manual:
      Min TIB | Min Monthly Rev | Min FICO | Max Funding | Positions | Buy / Sell Rate
    """
    funder_names = [
        "Logic Advance",
        "Velocity Capital Group",
        "SwiftSource Funding",
        "Shor Capital",
        "United Capital Source",
        "Highland Hill Capital",
        "Splash Advance",
        "Big Think Capital",
        "Bizi Connect",
    ]
    out: dict[str, dict[str, str]] = {}
    columns = [
        "min_tib",
        "min_monthly_rev",
        "min_fico",
        "max_funding",
        "positions",
        "buy_sell_rate",
    ]
    for name in funder_names:
        try:
            idx = lines.index(name)
        except ValueError:
            continue
        # Next 6 cells (skipping cells that are themselves headers)
        cells: list[str] = []
        scan = idx + 1
        while scan < len(lines) and len(cells) < len(columns):
            cell = lines[scan]
            # Stop if we hit the next funder name
            if cell in funder_names:
                break
            cells.append(cell)
            scan += 1
        out[name] = dict(zip(columns, cells, strict=False))
    return out


# Section-number → folder-name mapping (for operator UX)
_FOLDER_NAMES: dict[int, str] = {
    2: "LAG",
    3: "VCG",
    4: "Swiftsource",
    5: "Shor capital",
    6: "UCS",
    7: "Highland capital",
    8: "Splash advance",
    9: "Big think capital",
    10: "Bizi connect",
}

# Direct funders publish underwriting boxes (Quick Reference rows have
# real criteria). Brokers/affiliates/marketplace funders (§8/§9/§10) do
# NOT — their rows show "Per funder", "Not published", "N/A". We still
# track them in the funders table for operator visibility + commission
# tracking, with criteria fields left NULL and notes_residual capturing
# their model (broker / affiliate / marketplace).
_DIRECT_FUNDER_SECTIONS = {2, 3, 4, 5, 6, 7}
_BROKER_FUNDER_SECTIONS = {8, 9, 10}
_ALL_FUNDER_SECTIONS = _DIRECT_FUNDER_SECTIONS | _BROKER_FUNDER_SECTIONS

# Per-section model classification (lands in notes_residual prefix).
_FUNDER_MODEL: dict[int, str] = {
    8: "BROKER / AGGREGATOR",
    9: "AFFILIATE PARTNER",
    10: "LOAN MARKETPLACE",
}

# Canonical funder name per section (the manual sometimes uses
# abbreviations vs full names; we standardize on the full form).
_CANONICAL_NAME: dict[int, str] = {
    2: "Logic Advance",
    3: "Velocity Capital Group",
    4: "SwiftSource Funding",
    5: "Shor Capital",
    6: "United Capital Source",
    7: "Highland Hill Capital",
    8: "Splash Advance",
    9: "Big Think Capital",
    10: "Bizi Connect",
}


def _extract_emails(text: str) -> list[str]:
    return re.findall(r"[\w\.\-]+@[\w\.\-]+\.\w+", text)


def _extract_phone(text: str) -> str | None:
    m = re.search(r"\(?\d{3}\)?[\s\-]\d{3}[\s\-]\d{4}", text)
    return m.group(0).strip() if m else None


def _build_broker_preview(
    section_num: int,
    section_lines: list[str],
) -> FunderPreview:
    """Brokers / affiliates / marketplaces (§8/§9/§10) don't publish an
    underwriting box. Capture model + entity + contact + commission/
    clawback summary in notes_residual; leave criteria fields NULL so
    the matcher won't false-positive route deals to them. Operator
    flips active=false in /ui/funders/{id} if they should be excluded
    from match results entirely."""
    name = _CANONICAL_NAME[section_num]
    model = _FUNDER_MODEL[section_num]
    p = FunderPreview(name=name, section_number=section_num)

    section_text = "\n".join(section_lines)
    emails = _extract_emails(section_text)
    phone = _extract_phone(section_text)

    # Submission email — first email near a "Submissions" / "Contact" line
    for line in section_lines[:8]:
        ll = line.lower()
        found = _extract_emails(line)
        if found and ("submission" in ll or "contact" in ll):
            if "submission" in ll:
                p.submission_email = found[0]
            elif "contact" in ll:
                p.contact_email = found[0]
    if not p.submission_email and not p.contact_email and emails:
        p.contact_email = emails[0]
    if phone:
        p.contact_phone = phone

    # Contact name: the manual uses "Contact: Name, Title — email" or
    # "Entity: ... — Name, Title" patterns. Capture first plausible name.
    for line in section_lines[:6]:
        m = re.search(r"(?:Contact:|Entity:.*—|Co-Founder)\s+([A-Z][a-z]+\s+[A-Z][a-z]+)", line)
        if m:
            p.contact_name = m.group(1)
            break

    # notes_residual: model prefix + the first "How to Treat" line +
    # commission + clawback summary if present in the section.
    summary_bits = [f"{model} — not a direct-underwriting funder."]
    for line in section_lines:
        ll = line.lower()
        if ll.startswith("commission:"):
            summary_bits.append(line[:200].rstrip("."))
        elif ll.startswith("clawback:"):
            summary_bits.append(line[:200].rstrip("."))
        elif "no published box" in ll or "loan marketplace" in ll:
            summary_bits.append(line[:200].rstrip("."))
    p.notes_residual = " | ".join(summary_bits)

    # Criteria fields stay NULL — operator review surfaces this:
    p.needs_operator_review = [
        f"{model.lower()} — no published underwriting box; "
        "decide active flag + populate criteria via /ui/funders/{id} if you "
        "want match-list inclusion.",
    ]
    return p


def _build_preview(
    section_num: int,
    section_lines: list[str],
    qref: dict[str, str],
) -> FunderPreview:
    """Combine Quick Reference fields with section-detail enrichment."""
    if section_num in _BROKER_FUNDER_SECTIONS:
        return _build_broker_preview(section_num, section_lines)

    name = _CANONICAL_NAME[section_num]
    p = FunderPreview(name=name, section_number=section_num)

    # From Quick Reference
    p.min_months_in_business = _months(qref.get("min_tib", ""))
    p.min_monthly_revenue = _decimal(qref.get("min_monthly_rev", ""))
    p.min_credit_score = _fico(qref.get("min_fico", ""))
    p.max_advance = _decimal(qref.get("max_funding", ""))
    max_pos, accepts = _positions(qref.get("positions", ""))
    p.max_positions = max_pos
    p.accepts_stacking = accepts
    factor_lo, factor_hi = _factor_range(qref.get("buy_sell_rate", ""))
    p.typical_factor_low = factor_lo
    p.typical_factor_high = factor_hi

    # Per-section detail
    section_text = "\n".join(section_lines)
    # Submission email + rep details (common pattern in sections)
    emails = _extract_emails(section_text)
    phone = _extract_phone(section_text)
    if emails:
        # First email is usually the submissions inbox
        if "submission" in section_text.lower():
            for line in section_lines[:10]:
                if "submission" in line.lower():
                    found = _extract_emails(line)
                    if found:
                        p.submission_email = found[0]
                        break
        if not p.submission_email and emails:
            p.submission_email = emails[0]
        # Rep email is the second one or labeled "Rep —"
        for line in section_lines[:20]:
            if "Rep" in line or "rep" in line:
                found = _extract_emails(line)
                if found:
                    p.contact_email = found[0]
                # Rep name often before the colon
                m = re.search(r"Rep\s+[\-—]\s+([A-Z][a-z]+)", line)
                if m:
                    p.contact_name = m.group(1)
                break
    if phone:
        p.contact_phone = phone

    # Auto-decline + excluded-industries extraction.
    #
    # Each funder section is a sequence of subsections — restricted-industry
    # list, auto-decline list, commission structure, submission process,
    # required docs, payment/clawback, operational gotchas, etc. We capture
    # only the first two; everything after triggers a stop.
    #
    # Constants + _is_subsection_header + _is_list_header live at module
    # level for testability and to satisfy ruff N806 (no UPPER_CASE local
    # variable names).

    in_excluded = False
    in_auto_decline = False
    in_excluded_states = False

    def _bullet_split(text: str) -> list[str]:
        """Split bullet-delimited industries into individual entries.
        Handles "Bail Bonds  •  Gas Stations  •  Investment Firms" style."""
        parts = re.split(r"\s*[•·]\s*", text)
        return [p.strip() for p in parts if p.strip()]

    def _looks_like_industry(text: str) -> bool:
        """Industry names are short, don't end with periods, aren't sentences."""
        if not text or len(text) < 2 or len(text) > 100:
            return False
        if text[0].isdigit():
            return False
        if text.endswith("."):
            # Sentence, not a name — except dash-prefixed exceptions like
            # "Trucking — only restricted if <24 months TIB" which manual
            # writes without trailing periods.
            return False
        if text.lower() in {"industries", "products", "criterion", "requirement"}:
            return False
        return True

    def _is_us_state_token(text: str) -> bool:
        """Recognize 2-letter USPS codes in slash-delimited / comma lists."""
        text = text.strip()
        if len(text) != 2:
            return False
        return text.isupper() and text.isalpha()

    for line in section_lines:
        ll = line.lower()
        # Stop FIRST — reset all active lists if this line is a subsection header.
        if (in_excluded or in_auto_decline or in_excluded_states) and _is_subsection_header(line):
            in_excluded = False
            in_auto_decline = False
            in_excluded_states = False
            # don't continue — the stop line might itself start a new list below

        # Header line guards live at module level (see _is_list_header).
        if _is_list_header(line, ll, _START_EXCLUDED):
            in_excluded = True
            in_auto_decline = False
            in_excluded_states = False
            continue
        if _is_list_header(line, ll, _START_AUTO_DECLINE):
            in_auto_decline = True
            in_excluded = False
            in_excluded_states = False
            continue
        if _is_list_header(line, ll, _START_EXCLUDED_STATES):
            in_excluded_states = True
            in_excluded = False
            in_auto_decline = False
            continue

        # Append rules
        if in_excluded:
            # Bullet-separated multi-industry lines → split
            entries = _bullet_split(line) if ("•" in line or "·" in line) else [line]
            for entry in entries:
                if _looks_like_industry(entry):
                    p.excluded_industries.append(entry)
        if in_auto_decline and 2 < len(line) < 200 and not line[0].isdigit():
            p.auto_decline_conditions.append(line)
        if in_excluded_states:
            # States are slash- or comma-separated 2-letter codes
            for token in re.split(r"[\s,/]+", line):
                if _is_us_state_token(token):
                    p.excluded_states.append(token)

    # Conditional requirements (e.g., "Requires CoJ" / "Charges merchant fees")
    for line in section_lines:
        ll = line.lower()
        if "confession of judgment" in ll or "coj" in ll:
            if "required" in ll or "requires" in ll:
                p.requires_coj = True
            elif "no coj" in ll or "not required" in ll:
                p.requires_coj = False
        if "advance fee" in ll or "merchant fee" in ll or "origination fee" in ll:
            if "no" in ll[:5] or "not charge" in ll:
                p.charges_merchant_advance_fees = False

    # Highland Hill is documented as subordinate-only (no 1st position)
    if section_num == 7:  # Highland Hill
        p.accepts_stacking = True  # explicit "2nd-8th positions" → accepts existing

    # Notes residual: free-form caveat for operator review
    notes = []
    if section_num == 2:
        notes.append(
            "Logic Advance has 4 tiers (Elite / Premium / Standard / High-Risk). "
            "Quick Reference reflects loosest tier (High-Risk). Full tier breakdown "
            "lives in manual §2; consider tier-aware FunderTier rows in a follow-up."
        )
    if section_num == 4:
        notes.append(
            "Swiftsource: '2nd & up only' — does not fund 1st position. "
            "max_positions field semantics need operator review."
        )
    if section_num == 6:
        notes.append(
            "UCS is multi-product (MCA / Term / SBA / Equipment / LOC). Quick "
            "Reference reflects MCA-product criteria; other products have "
            "different boxes in manual §6."
        )
    if section_num == 7:
        notes.append(
            "Highland Hill: subordinate positions 2nd–8th (does NOT fund 1st). "
            "Specialty reverse-consolidation funder."
        )
    p.notes_residual = " | ".join(notes) if notes else ""

    # Surface gaps for operator review
    if p.min_credit_score is None:
        p.needs_operator_review.append("min_credit_score (not in Quick Reference)")
    if p.max_advance is None:
        p.needs_operator_review.append("max_advance (not parseable from QR)")
    if p.typical_holdback_low is None and p.typical_holdback_high is None:
        p.needs_operator_review.append("typical_holdback range (not in manual)")

    return p


def _serialize(p: FunderPreview) -> dict[str, Any]:
    d = asdict(p)
    # Decimal → str so JSON dumps cleanly
    for k, v in d.items():
        if isinstance(v, Decimal):
            d[k] = str(v)
    return d


def parse_manual(docx_path: Path) -> list[FunderPreview]:
    lines = _read_paragraphs(docx_path)
    sections = _locate_sections(lines)
    qref = _parse_quick_reference(lines)
    previews: list[FunderPreview] = []
    for i, (start_idx, sect_num, _title) in enumerate(sections):
        if sect_num not in _ALL_FUNDER_SECTIONS:
            continue
        end_idx = sections[i + 1][0] if i + 1 < len(sections) else len(lines)
        section_lines = lines[start_idx:end_idx]
        canonical_name = _CANONICAL_NAME[sect_num]
        previews.append(_build_preview(sect_num, section_lines, qref.get(canonical_name, {})))
    return previews


def _sql_text_array(items: list[str]) -> str:
    """Emit a Postgres TEXT[] literal: ARRAY['a','b']::TEXT[]. Empty
    list becomes '{}'::TEXT[]."""
    if not items:
        return "'{}'::TEXT[]"
    escaped = [s.replace("'", "''") for s in items]
    return "ARRAY[" + ", ".join(f"'{s}'" for s in escaped) + "]::TEXT[]"


def _sql_text(value: str | None) -> str:
    if value is None:
        return "''"
    return "'" + value.replace("'", "''") + "'"


def _sql_decimal(value: Decimal | None) -> str:
    return "NULL" if value is None else str(value)


def _sql_int(value: int | None) -> str:
    return "NULL" if value is None else str(value)


def _sql_bool(value: bool | None) -> str:
    if value is None:
        return "NULL"
    return "true" if value else "false"


_MIGRATION_HEADER = """-- Seed funders from Filip's internal MCA Funder Manual.
--
-- Source: C:\\Users\\fkozi\\OneDrive\\Radna površina\\COMMERA FUNDING\\
--         (referenced) MCA_Funder_Manual.docx
--
-- Generated by scripts/import_funders_from_manual.py. The manual is
-- operator-curated by Filip's team — every threshold here is
-- operator-real per .claude/rules/operating-principles.md #4 (this
-- supersedes migration 035's placeholder seed which was deleted by
-- migration 045).
--
-- Contains all 9 funders from the manual:
--   §2-§7 Direct funders with published underwriting boxes — full
--          criteria (TIB / monthly revenue / FICO / max advance /
--          positions / factor range) populated.
--   §8-§10 Brokers / affiliates / marketplace — no published box;
--          criteria fields NULL, notes_residual prefixed with the
--          model (BROKER / AFFILIATE PARTNER / LOAN MARKETPLACE) +
--          commission + contact. Operator decides via /ui/funders/{id}
--          whether to flip active=false (exclude from match-list) or
--          populate criteria for manual routing.
--
-- Idempotent via ON CONFLICT (name) DO NOTHING — operator-edited rows
-- are never overwritten. If migration 046 already landed the 6 direct
-- funders, only the 3 broker rows insert; the 6 direct rows are
-- effectively a no-op on re-run.
--
-- To replace a single row with fresh extraction: manually DELETE the
-- row first via /ui/funders/{id} or SQL, then re-apply.

"""


def emit_migration_sql(previews: list[FunderPreview]) -> str:
    """Emit migration 046 with INSERT statements for the 6 direct funders.

    All values come straight from FunderPreview — same data the JSON
    preview shows. ON CONFLICT (name) DO NOTHING so operator-edited
    rows survive re-apply.
    """
    lines: list[str] = [_MIGRATION_HEADER, "INSERT INTO funders ("]
    cols = [
        "name",
        "active",
        "min_monthly_revenue",
        "min_credit_score",
        "min_months_in_business",
        "max_positions",
        "accepts_stacking",
        "min_advance",
        "max_advance",
        "typical_factor_low",
        "typical_factor_high",
        "excluded_industries",
        "excluded_states",
        "requires_coj",
        "charges_merchant_advance_fees",
        "contact_name",
        "contact_email",
        "contact_phone",
        "submission_email",
        "auto_decline_conditions",
        "notes_residual",
        "guidelines_extracted_at",
    ]
    lines.append("  " + ", ".join(cols))
    lines.append(") VALUES")

    value_lines: list[str] = []
    for p in previews:
        v = [
            _sql_text(p.name),
            "true",
            _sql_decimal(p.min_monthly_revenue),
            _sql_int(p.min_credit_score),
            _sql_int(p.min_months_in_business),
            _sql_int(p.max_positions),
            _sql_bool(p.accepts_stacking) if p.accepts_stacking is not None else "false",
            _sql_decimal(p.min_advance),
            _sql_decimal(p.max_advance),
            _sql_decimal(p.typical_factor_low),
            _sql_decimal(p.typical_factor_high),
            _sql_text_array(p.excluded_industries),
            _sql_text_array(p.excluded_states),
            _sql_bool(p.requires_coj) if p.requires_coj is not None else "false",
            (
                _sql_bool(p.charges_merchant_advance_fees)
                if p.charges_merchant_advance_fees is not None
                else "false"
            ),
            _sql_text(p.contact_name),
            _sql_text(p.contact_email),
            _sql_text(p.contact_phone),
            _sql_text(p.submission_email),
            _sql_text_array(p.auto_decline_conditions),
            _sql_text(p.notes_residual),
            "NOW()",
        ]
        value_lines.append(f"-- §{p.section_number} {p.name}\n(  " + ",\n   ".join(v) + ")")
    lines.append(",\n".join(value_lines))
    lines.append("ON CONFLICT (name) DO NOTHING;")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manual",
        required=True,
        type=Path,
        help="Path to MCA_Funder_Manual.docx",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(tempfile.gettempdir()) / "aegis-funder-previews",
        help="Output directory for per-funder JSON files",
    )
    parser.add_argument(
        "--emit-sql",
        type=Path,
        default=None,
        help="Optional: write a migration SQL file with INSERT statements "
        "for the 6 direct funders. Use migrations/046_seed_funders_from_manual.sql.",
    )
    args = parser.parse_args(argv)

    if not args.manual.exists():
        print(f"ERROR: manual file not found: {args.manual}", file=sys.stderr)
        return 2

    # UTF-8 stdout for Unicode glyphs in source text
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    previews = parse_manual(args.manual)
    args.out.mkdir(parents=True, exist_ok=True)

    for p in previews:
        canonical_slug = p.name.lower().replace(" ", "_")
        path = args.out / f"{canonical_slug}.json"
        path.write_text(
            json.dumps(_serialize(p), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[ok]  {p.name}  →  {path}")
        # Quick summary line
        print(
            f"      TIB={p.min_months_in_business}mo  rev=${p.min_monthly_revenue}  "
            f"FICO={p.min_credit_score}  max=${p.max_advance}  pos={p.max_positions}  "
            f"factor={p.typical_factor_low}-{p.typical_factor_high}"
        )
        if p.needs_operator_review:
            print(f"      ⚠ review: {', '.join(p.needs_operator_review)}")

    print(f"\nWrote {len(previews)} previews to {args.out}")
    direct = [p for p in previews if p.section_number in _DIRECT_FUNDER_SECTIONS]
    brokers = [p for p in previews if p.section_number in _BROKER_FUNDER_SECTIONS]
    print(f"  Direct funders: {len(direct)}; Brokers/affiliates/marketplace: {len(brokers)}")

    if args.emit_sql:
        sql = emit_migration_sql(previews)
        args.emit_sql.write_text(sql, encoding="utf-8")
        print(f"\nWrote SQL migration → {args.emit_sql}")
    else:
        print("Operator confirms previews, then migration inserts confirmed rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
