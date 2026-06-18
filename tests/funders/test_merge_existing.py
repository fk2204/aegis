"""Tests for ``aegis.funders.merge_existing.merge_preview_with_existing``.

Codifies the policy that grew out of the 2026-06-18 funder pass — the
``PRESERVE_IF_POPULATED`` set protects operator-curated fields from
silent overwrite when re-extracting against a fresh guidelines doc.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from aegis.funders.merge_existing import (
    PRESERVE_IF_POPULATED,
    merge_preview_with_existing,
)


def _existing(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "name": "Acme Capital",
        "active": True,
        "operator_status": "active",
        "min_monthly_revenue": None,
        "min_credit_score": None,
        "min_months_in_business": None,
        "max_positions": None,
        "accepts_stacking": False,
        "min_advance": None,
        "max_advance": None,
        "typical_factor_low": None,
        "typical_factor_high": None,
        "excluded_industries": [],
        "excluded_states": [],
        "deal_types_accepted": [],
        "contact_name": "",
        "contact_phone": "",
        "contact_email": "",
        "submission_email": "",
        "tiers": [],
        "auto_decline_conditions": [],
        "conditional_requirements": [],
        "notes": "",
        "notes_residual": "",
        "operator_notes": "",
        "created_at": "2026-06-10T00:00:00+00:00",
        "updated_at": "2026-06-10T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def _preview(**draft_overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "id": "99999999-9999-9999-9999-999999999999",
        "name": "ACME CAPITAL LLC",
        "active": True,
        "operator_status": "active",
        "min_monthly_revenue": None,
        "min_credit_score": None,
        "min_months_in_business": None,
        "max_positions": None,
        "accepts_stacking": False,
        "min_advance": None,
        "max_advance": None,
        "typical_factor_low": None,
        "typical_factor_high": None,
        "excluded_industries": [],
        "excluded_states": [],
        "deal_types_accepted": [],
        "contact_name": "",
        "contact_phone": "",
        "contact_email": "",
        "submission_email": "",
        "tiers": [],
        "auto_decline_conditions": [],
        "conditional_requirements": [],
        "notes": "",
        "notes_residual": "",
        "operator_notes": "",
        "guidelines_extracted_at": "2026-06-18T12:00:00+00:00",
        "guidelines_source_pdf_hash": "abc123",
    }
    draft.update(draft_overrides)
    return {
        "draft": draft,
        "confidence_by_field": {},
        "unparseable_fragments": [],
        "overall_confidence": 80,
    }


# ---------------------------------------------------------------------------
# id / name preservation
# ---------------------------------------------------------------------------


def test_existing_id_and_name_always_win() -> None:
    existing = _existing(id="11111111-1111-1111-1111-111111111111", name="Acme Capital")
    preview = _preview(id="22222222-2222-2222-2222-222222222222", name="ACME CAPITAL LLC")
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["id"] == "11111111-1111-1111-1111-111111111111"
    assert merged["draft"]["name"] == "Acme Capital"


# ---------------------------------------------------------------------------
# PRESERVE_IF_POPULATED — the 2026-06-18 lesson
# ---------------------------------------------------------------------------


def test_accepts_stacking_preserved_when_existing_true_new_false() -> None:
    """UCS / Logic Advance failure mode: re-extract flipped True→False."""
    existing = _existing(accepts_stacking=True)
    preview = _preview(accepts_stacking=False)
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["accepts_stacking"] is True


def test_contact_phone_preserved_when_existing_populated() -> None:
    """UCS failure mode: direct line overwritten with a vanity number."""
    existing = _existing(contact_phone="646-448-1711")
    preview = _preview(contact_phone="855-WE-FUND-U")
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["contact_phone"] == "646-448-1711"


def test_contact_email_preserved_when_existing_populated() -> None:
    existing = _existing(contact_email="ColleenS@example.com")
    preview = _preview(contact_email="info@example.com")
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["contact_email"] == "ColleenS@example.com"


def test_submission_email_preserved_when_existing_populated() -> None:
    existing = _existing(submission_email="isosubmissions@example.com")
    preview = _preview(submission_email="info@example.com")
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["submission_email"] == "isosubmissions@example.com"


def test_conditional_requirements_preserved_when_existing_populated() -> None:
    """VCG failure mode: operator's BK/judgments notes overwritten."""
    existing = _existing(
        conditional_requirements=[
            "Bankruptcies, judgments, and tax liens acceptable with documentation",
            "Deals of $100k+ may require additional financial documents",
        ]
    )
    preview = _preview(
        conditional_requirements=["application", "bank statements", "DL", "voided check"]
    )
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["conditional_requirements"] == [
        "Bankruptcies, judgments, and tax liens acceptable with documentation",
        "Deals of $100k+ may require additional financial documents",
    ]


def test_excluded_industries_preserved_when_existing_populated() -> None:
    """VCG / Logic Advance failure mode: qualifier strings lost."""
    existing = _existing(
        excluded_industries=[
            "Trucking — only restricted if <24mo TIB OR <$100K revenue",
            "Construction — only restricted if <24mo TIB OR <$100K revenue",
        ]
    )
    preview = _preview(excluded_industries=["trucking", "construction"])
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["excluded_industries"] == [
        "Trucking — only restricted if <24mo TIB OR <$100K revenue",
        "Construction — only restricted if <24mo TIB OR <$100K revenue",
    ]


def test_operator_notes_preserved_when_existing_populated() -> None:
    existing = _existing(operator_notes="prefers small trucking deals; call Erik for quirks")
    preview = _preview(operator_notes="")
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["operator_notes"] == "prefers small trucking deals; call Erik for quirks"


def test_preserve_if_populated_takes_new_when_existing_empty() -> None:
    """The set protects existing data — but not empty existing data.

    Field is in PRESERVE_IF_POPULATED, existing is empty/null, new has
    a value → take the new value. Otherwise re-extraction could never
    fill these fields on a sparse record.
    """
    existing = _existing(contact_phone="", accepts_stacking=False)
    preview = _preview(contact_phone="555-1234", accepts_stacking=True)
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["contact_phone"] == "555-1234"
    # accepts_stacking=False is "empty"-equivalent only for strings/None/empty
    # containers; bool False is still a real value. Existing False wins.
    assert merged["draft"]["accepts_stacking"] is False


def test_every_preserve_if_populated_field_is_a_real_funder_row_field() -> None:
    """Guard against the set drifting from the model. Every name in
    PRESERVE_IF_POPULATED must be a field the merge actually touches —
    i.e. a column on the funders row."""
    existing = _existing()
    for field in PRESERVE_IF_POPULATED:
        assert field in existing, (
            f"PRESERVE_IF_POPULATED contains {field!r} which isn't on the "
            f"funders row shape — merge wouldn't apply the rule"
        )


# ---------------------------------------------------------------------------
# notes_residual concat behaviour
# ---------------------------------------------------------------------------


def test_notes_residual_concat_with_date_separator() -> None:
    existing = _existing(notes_residual="BROKER aggregator; commission case-by-case")
    preview = _preview(notes_residual="Min $20K revenue, factor 1.499")
    merged = merge_preview_with_existing(
        existing,
        preview,
        extracted_at=datetime.fromisoformat("2026-07-04T10:00:00+00:00"),
    )
    assert merged["draft"]["notes_residual"] == (
        "BROKER aggregator; commission case-by-case | "
        "GUIDELINES (2026-07-04 extract): Min $20K revenue, factor 1.499"
    )


def test_notes_residual_existing_only_when_new_empty() -> None:
    existing = _existing(notes_residual="legacy operator notes")
    preview = _preview(notes_residual="")
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["notes_residual"] == "legacy operator notes"


def test_notes_residual_new_only_when_existing_empty() -> None:
    existing = _existing(notes_residual="")
    preview = _preview(notes_residual="fresh extraction notes")
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["notes_residual"] == "fresh extraction notes"


def test_notes_residual_falls_back_to_extracted_at_in_draft() -> None:
    existing = _existing(notes_residual="A")
    preview = _preview(
        notes_residual="B",
        guidelines_extracted_at="2026-08-15T14:30:00Z",
    )
    merged = merge_preview_with_existing(existing, preview)
    assert "2026-08-15 extract" in merged["draft"]["notes_residual"]


# ---------------------------------------------------------------------------
# Default merge (non-PRESERVE fields)
# ---------------------------------------------------------------------------


def test_default_merge_takes_new_when_both_populated() -> None:
    """min_monthly_revenue is NOT in PRESERVE_IF_POPULATED — default policy
    takes new value when both sides have one."""
    existing = _existing(min_monthly_revenue=40000)
    preview = _preview(min_monthly_revenue=25000)
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["min_monthly_revenue"] == 25000


def test_default_merge_falls_back_to_existing_when_new_empty() -> None:
    """Default fields: if new is null and existing has data, keep existing."""
    existing = _existing(min_monthly_revenue=40000)
    preview = _preview(min_monthly_revenue=None)
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["min_monthly_revenue"] == 40000


def test_tiers_preserved_when_new_empty_existing_populated() -> None:
    """Highland Hill case: existing tiers structured, new tiers []. Keep existing."""
    tiers = [
        {"name": "Tier 1", "buy_rate_low": "1.499", "buy_rate_high": "1.499"},
        {"name": "Tier 2", "buy_rate_low": "1.459", "buy_rate_high": "1.459"},
    ]
    existing = _existing(tiers=tiers)
    preview = _preview(tiers=[])
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["tiers"] == tiers


def test_tiers_taken_when_new_populated() -> None:
    """SwiftSource case: existing tiers [], new tiers structured. Take new."""
    new_tiers = [{"name": "Tier 1", "buy_rate_low": "1.35", "buy_rate_high": "1.35"}]
    existing = _existing(tiers=[])
    preview = _preview(tiers=new_tiers)
    merged = merge_preview_with_existing(existing, preview)
    assert merged["draft"]["tiers"] == new_tiers


# ---------------------------------------------------------------------------
# Input immutability
# ---------------------------------------------------------------------------


def test_merge_does_not_mutate_inputs() -> None:
    existing = _existing(min_monthly_revenue=40000, contact_phone="OLD")
    preview = _preview(min_monthly_revenue=25000, contact_phone="NEW")
    preview_before = dict(preview["draft"])
    existing_before = dict(existing)
    _ = merge_preview_with_existing(existing, preview)
    assert preview["draft"] == preview_before
    assert existing == existing_before
