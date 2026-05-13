"""OFAC hard-decline match-detail wiring.

Covers the regulatory hand-off after a positive screen — per
``docs/compliance/07_ofac_sanctions.md`` AEGIS must capture the SDN
candidate name + uid that fired so the operator can file the
10-business-day Initial Report of Blocked Property without re-running
the screen.

The brief:

* mock the OFAC client (no Treasury network in tests),
* assert hard-decline fires with ``ofac_sanctions_match`` and the right
  ``decline_details`` payload (input field, matched_name, sdn_uid),
* assert a non-matching name does NOT decline,
* assert the audit_log row captures ``decline_details`` (CLAUDE.md
  audit-write rule: audit-write failures FAIL the calling operation,
  so this row MUST land).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aegis.audit import InMemoryAuditLog
from aegis.scoring.models import ScoreInput
from aegis.scoring.ofac import OFACClient, SDNMatch
from aegis.scoring.score import score_deal

# A fictional sanctioned-entity name used only in tests. Not a real SDN
# entry — the brief requires fictional names so unit tests never carry
# real OFAC data even by accident.
FICTIONAL_SDN_NAME = "Zorath Holdings International"
FICTIONAL_SDN_UID = "999001"


def _panic_fetcher() -> bytes:
    raise RuntimeError("fetcher must not run when cache is fresh")


@pytest.fixture
def ofac_with_fictional_entry(tmp_path: Path) -> Iterator[OFACClient]:
    """Real ``OFACClient`` over a cache holding one fictional SDN entry.

    Uses the real client (not a Mock) because the brief requires we
    verify the integration path end-to-end — ``score_deal`` ->
    ``OFACClient.find_match`` -> ``SDNMatch`` -> ``ScoreResult.decline_details``.
    The Treasury network call is mocked out via ``_panic_fetcher``: any
    network attempt fails the test loudly.
    """
    cache = tmp_path / "ofac" / "sdn.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "primary_name": FICTIONAL_SDN_NAME,
                        "aliases": ["Zorath Intl"],
                        "uid": FICTIONAL_SDN_UID,
                    }
                ],
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    client = OFACClient(
        cache_path=cache,
        fetcher=_panic_fetcher,
        now=lambda: datetime.now(UTC),
    )
    yield client


def test_business_name_match_emits_ofac_decline_with_details(
    clean_deal: ScoreInput,
    ofac_with_fictional_entry: OFACClient,
) -> None:
    """Hard-decline fires AND match details are captured per OFAC dossier §reporting."""
    deal = clean_deal.model_copy(
        update={"business_name": f"{FICTIONAL_SDN_NAME} (DBA Acme)"}
    )

    result = score_deal(deal, ofac=ofac_with_fictional_entry)

    assert result.recommendation == "decline"
    assert result.tier == "F"
    assert "ofac_sanctions_match" in result.hard_decline_reasons
    assert "ofac_matches" in result.decline_details
    matches = result.decline_details["ofac_matches"]
    assert len(matches) == 1
    only = matches[0]
    assert only["input_field"] == "business_name"
    assert only["matched_name"] == FICTIONAL_SDN_NAME
    assert only["sdn_uid"] == FICTIONAL_SDN_UID


def test_owner_name_match_emits_ofac_decline_with_details(
    clean_deal: ScoreInput,
    ofac_with_fictional_entry: OFACClient,
) -> None:
    deal = clean_deal.model_copy(
        update={"owner_name": f"managed by {FICTIONAL_SDN_NAME}"}
    )

    result = score_deal(deal, ofac=ofac_with_fictional_entry)

    assert "ofac_sanctions_match" in result.hard_decline_reasons
    matches = result.decline_details["ofac_matches"]
    assert len(matches) == 1
    assert matches[0]["input_field"] == "owner_name"
    assert matches[0]["sdn_uid"] == FICTIONAL_SDN_UID


def test_both_fields_match_captures_both_in_details(
    clean_deal: ScoreInput,
    ofac_with_fictional_entry: OFACClient,
) -> None:
    """If business name AND owner name both match, both rows surface for reporting."""
    deal = clean_deal.model_copy(
        update={
            "business_name": f"{FICTIONAL_SDN_NAME} LLC",
            "owner_name": f"Owner of {FICTIONAL_SDN_NAME}",
        }
    )

    result = score_deal(deal, ofac=ofac_with_fictional_entry)

    matches = result.decline_details["ofac_matches"]
    fields = {m["input_field"] for m in matches}
    assert fields == {"business_name", "owner_name"}
    # One ``ofac_sanctions_match`` reason — the count of individual hits
    # lives in decline_details, not by repeating the reason code.
    assert result.hard_decline_reasons.count("ofac_sanctions_match") == 1


def test_clean_name_does_not_decline_and_has_no_decline_details(
    clean_deal: ScoreInput,
    ofac_with_fictional_entry: OFACClient,
) -> None:
    """A clean merchant must produce no OFAC reason and no ofac_matches payload."""
    result = score_deal(clean_deal, ofac=ofac_with_fictional_entry)

    assert "ofac_sanctions_match" not in result.hard_decline_reasons
    assert "ofac_matches" not in result.decline_details
    assert result.recommendation in {"approve", "refer"}


def test_audit_log_captures_ofac_match_details(
    clean_deal: ScoreInput,
    ofac_with_fictional_entry: OFACClient,
) -> None:
    """The dashboard / Zoho sync path reads decline_details out of audit_log.

    Mirrors the ``/deals/score`` handler — score then record. Locked in
    here so a future refactor of the endpoint can't silently drop the
    structured payload (which would break the 10-day Initial Report of
    Blocked Property since the matched_name + sdn_uid would only live
    in transient memory).
    """
    deal = clean_deal.model_copy(
        update={"business_name": f"{FICTIONAL_SDN_NAME} Corp"}
    )
    audit = InMemoryAuditLog()

    result = score_deal(deal, ofac=ofac_with_fictional_entry)
    audit.record(
        actor="api",
        action="deal.score",
        subject_type="merchant",
        subject_id=deal.merchant_id,
        details={
            "score": result.score,
            "tier": result.tier,
            "recommendation": result.recommendation,
            "hard_decline_reasons": result.hard_decline_reasons,
            "decline_details": result.decline_details,
            "ofac_consulted": True,
        },
    )

    assert len(audit.entries) == 1
    row = audit.entries[0]
    assert row["action"] == "deal.score"
    assert row["subject_type"] == "merchant"
    payload = row["details"]
    assert payload["recommendation"] == "decline"
    assert "ofac_sanctions_match" in payload["hard_decline_reasons"]
    captured = payload["decline_details"]["ofac_matches"]
    assert captured[0]["matched_name"] == FICTIONAL_SDN_NAME
    assert captured[0]["sdn_uid"] == FICTIONAL_SDN_UID


def test_find_match_returns_sdnmatch_or_none(
    ofac_with_fictional_entry: OFACClient,
) -> None:
    """Client surface contract: find_match returns SDNMatch on hit, None on miss."""
    hit = ofac_with_fictional_entry.find_match(f"{FICTIONAL_SDN_NAME} Holdings")
    assert isinstance(hit, SDNMatch)
    assert hit.matched_name == FICTIONAL_SDN_NAME
    assert hit.sdn_uid == FICTIONAL_SDN_UID

    miss = ofac_with_fictional_entry.find_match("Genuine Painting Co")
    assert miss is None


def test_legacy_cache_without_uid_still_matches_with_empty_sdn_uid(
    clean_deal: ScoreInput,
    tmp_path: Path,
) -> None:
    """Older cache files predate the uid field. Match must still fire."""
    cache = tmp_path / "sdn.json"
    cache.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "primary_name": FICTIONAL_SDN_NAME,
                        "aliases": [],
                        # uid intentionally omitted — legacy cache shape.
                    }
                ],
                "refreshed_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    client = OFACClient(
        cache_path=cache,
        fetcher=_panic_fetcher,
        now=lambda: datetime.now(UTC),
    )
    deal = clean_deal.model_copy(
        update={"business_name": f"{FICTIONAL_SDN_NAME} LLC"}
    )

    result = score_deal(deal, ofac=client)

    assert "ofac_sanctions_match" in result.hard_decline_reasons
    matches = result.decline_details["ofac_matches"]
    # Empty string (not None) so the payload stays JSON-safe and audit_log
    # callers don't have to special-case missing uids.
    assert matches[0]["sdn_uid"] == ""
