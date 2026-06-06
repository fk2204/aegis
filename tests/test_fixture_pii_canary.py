"""Scans every committed fixture for PII patterns and fails if any leaks.

Purpose
=======

This is the forward-protection against the 2026-06-05 mistake where
``ae62df2`` shipped a transactions fixture with named individuals'
names intact. A redaction pass ran late, the regex missed the "Zelle
payment to" rows, and the leaked names got pushed to GitHub.

This test runs in CI on every commit. It walks every ``.json`` file
under ``tests/**/fixtures/`` and asserts NO known PII pattern slipped
through. Concretely:

* ``Zelle payment from|to <NAME>`` — if ``<NAME>`` is anything other
  than a ``REDACTED_PARTY_N`` placeholder, the test fails with the
  offending file path + line and a pointer to the sanitizer.
* ``VU DEVELOPMENT[ CO]`` — the specific merchant whose foundation
  fixture was captured. If the literal name reappears in a future
  capture, the test fails.

The canary is intentionally narrower than a generic "any uppercase
sequence" heuristic — it targets the patterns we KNOW leaked. Broader
PII detection (full email regex, phone regex, etc.) can be added as
the fixture inventory grows; the priority is "the known failure mode
cannot ship".

If this test fails, the fix is to run the captured payload through
``tests._fixture_sanitize.sanitize_fixture_payload`` BEFORE writing
the fixture to disk. The capture script template in
``scripts/audit/`` shows the pattern.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_FIXTURES_GLOB = "tests/**/fixtures/*.json"


def _iter_fixture_paths() -> list[Path]:
    return sorted(_REPO_ROOT.glob(_FIXTURES_GLOB))


def _walk_strings(payload: object) -> list[str]:
    """Yield every string value in a nested JSON-like payload."""
    out: list[str] = []
    stack: list[object] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return out


# ---------------------------------------------------------------------
# The canary itself
# ---------------------------------------------------------------------


def test_known_pii_canary_passes() -> None:
    """If this fails, a fixture is leaking known PII."""
    from tests._fixture_sanitize import assert_no_pii_in_descriptions

    failures: list[tuple[Path, str]] = []
    for path in _iter_fixture_paths():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Not a JSON object fixture (could be a binary holder). Skip.
            continue
        if not isinstance(payload, dict):
            continue
        try:
            assert_no_pii_in_descriptions(payload)
        except AssertionError as exc:
            failures.append((path.relative_to(_REPO_ROOT), str(exc)))

    if failures:
        msg = "\n".join(
            f"\n=== {p} ===\n{detail}" for p, detail in failures
        )
        pytest.fail(
            "PII canary tripped on committed fixture(s). Re-run capture "
            "through tests._fixture_sanitize.sanitize_fixture_payload "
            "before writing to disk:" + msg
        )


def test_zelle_named_rows_are_all_placeholders_in_every_fixture() -> None:
    """Belt-and-suspenders check: scan every string in every fixture
    for the literal "Zelle payment from " / "Zelle payment to " prefix,
    and require what follows to start with ``REDACTED_PARTY_``."""
    import re

    zelle_re = re.compile(
        r"\bZelle payment (?:from|to)\s+(?P<rest>.{0,80})", re.IGNORECASE
    )
    failures: list[tuple[Path, str]] = []
    for path in _iter_fixture_paths():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for s in _walk_strings(payload):
            for m in zelle_re.finditer(s):
                rest = m.group("rest").strip()
                if rest.startswith("REDACTED_PARTY_"):
                    continue
                failures.append((path.relative_to(_REPO_ROOT), s[:120]))
                break

    if failures:
        msg = "\n".join(f"  {p}  {s!r}" for p, s in failures)
        pytest.fail("Zelle named-party leak in fixture(s):\n" + msg)


def test_known_merchant_name_not_present_in_fixtures() -> None:
    """The literal "VU DEVELOPMENT" string is the captured merchant's
    name. It should never appear in a fixture — any reference should
    be replaced with ``REDACTED_MERCHANT_CO`` by the sanitizer."""
    failures: list[Path] = []
    for path in _iter_fixture_paths():
        text = path.read_text(encoding="utf-8")
        if "VU DEVELOPMENT" in text.upper():
            failures.append(path.relative_to(_REPO_ROOT))

    if failures:
        msg = "\n".join(f"  {p}" for p in failures)
        pytest.fail(
            "Known merchant name 'VU DEVELOPMENT' present in fixture(s) — "
            "replace with REDACTED_MERCHANT_CO via the sanitizer:\n" + msg
        )
