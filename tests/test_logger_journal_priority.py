"""Tests for ``aegis.logger`` journal-priority-prefix routing.

Chunk that closes the journal severity-routing blind spot surfaced
during chunk-A deploy verification: ``journalctl -p err`` had been
structurally unable to show ANY Python ERROR/CRITICAL across all of
AEGIS because the StreamHandler wrote plain stderr lines without a
``<N>`` priority prefix.

The fix detects ``JOURNAL_STREAM`` (set by systemd when stderr is
connected to journald) once at logger setup, and uses
``_JournalPriorityFormatter`` to prepend the priority. The non-journal
path stays on the stdlib Formatter — Windows/CI/REPL output is
byte-for-byte unchanged.

Tests cover:

  * level → priority mapping (parametrized across all standard levels)
  * unmapped level → ERROR (fail-loud contract, NOT silently INFO)
  * formatter selection branches on JOURNAL_STREAM presence/absence
  * the no-prefix path is provably untouched (returned formatter type
    is exactly ``logging.Formatter``, not a subclass)
  * PII masking still runs in front of either formatter
"""
from __future__ import annotations

import logging

import pytest

from aegis.logger import (
    PiiMaskingFilter,
    _JournalPriorityFormatter,
    _select_stream_formatter,
)

# ---------------------------------------------------------------------------
# Level → priority mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "level,expected_prefix",
    [
        (logging.CRITICAL, "<2>"),
        (logging.ERROR,    "<3>"),
        (logging.WARNING,  "<4>"),
        (logging.INFO,     "<6>"),
        (logging.DEBUG,    "<7>"),
    ],
)
def test_priority_prefix_for_standard_levels(
    level: int, expected_prefix: str
) -> None:
    """Each standard Python level maps to the expected syslog priority
    so ``journalctl -p err`` discriminates correctly."""
    formatter = _JournalPriorityFormatter(fmt="%(message)s")
    record = logging.LogRecord(
        name="test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )

    output = formatter.format(record)

    assert output.startswith(expected_prefix), (
        f"expected line to start with {expected_prefix!r}, got {output!r}"
    )


def test_unmapped_level_falls_back_to_error_priority() -> None:
    """A custom level (here: 25, the NOTICE-ish slot between INFO and
    WARNING) maps to ``<3>`` (ERROR), NOT ``<6>`` (INFO).

    Operator-required contract: a custom level emitted via
    ``logging.log(25, ...)`` MUST fail loud. Defaulting to INFO would
    re-create the blind spot we are closing — an unfamiliar level
    silently classified as INFO and missed by ``journalctl -p err``.
    A too-loud line is recoverable; a hidden one is not.
    """
    formatter = _JournalPriorityFormatter(fmt="%(message)s")
    record = logging.LogRecord(
        name="test",
        level=25,  # NOTICE-ish; not in _SYSLOG_PRIORITY_BY_LEVEL
        pathname=__file__,
        lineno=1,
        msg="custom level emission",
        args=(),
        exc_info=None,
    )

    output = formatter.format(record)

    assert output.startswith("<3>"), (
        f"unmapped level 25 must fall back to ERROR (<3>), got {output!r}"
    )
    # Sanity: must NOT silently downgrade to INFO
    assert not output.startswith("<6>")


# ---------------------------------------------------------------------------
# Prefix is exactly the underlying-format byte-for-byte plus <N>
# ---------------------------------------------------------------------------


def test_journal_formatter_output_equals_plain_formatter_plus_prefix() -> None:
    """The body of a journal-mode line MUST be byte-for-byte identical
    to the plain-formatter output, with only the ``<N>`` prefix added.
    Without this, log search / grep recipes that assume a particular
    line shape would break on journal-mode hosts.
    """
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S%z"
    plain = logging.Formatter(fmt=fmt, datefmt=datefmt)
    journal = _JournalPriorityFormatter(fmt=fmt, datefmt=datefmt)

    record = logging.LogRecord(
        name="aegis.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="rendered body",
        args=(),
        exc_info=None,
    )

    plain_output = plain.format(record)
    journal_output = journal.format(record)

    assert journal_output == f"<3>{plain_output}"


# ---------------------------------------------------------------------------
# _select_stream_formatter — branches on JOURNAL_STREAM
# ---------------------------------------------------------------------------


def test_select_returns_journal_formatter_when_journal_stream_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``JOURNAL_STREAM=device:inode`` (the shape systemd sets) →
    journal-priority formatter installed on the root handler."""
    monkeypatch.setenv("JOURNAL_STREAM", "8:12345")

    formatter = _select_stream_formatter()

    assert isinstance(formatter, _JournalPriorityFormatter)


def test_select_returns_plain_formatter_when_journal_stream_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``JOURNAL_STREAM`` (Windows dev box, CI, plain
    terminal, pytest, REPL) → the stdlib ``logging.Formatter``
    EXACTLY. Not a subclass, not a wrapper. The non-journal path
    must be byte-for-byte unchanged from before this chunk.
    """
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)

    formatter = _select_stream_formatter()

    # Exact-type check (not isinstance) — _JournalPriorityFormatter
    # extends Formatter, so isinstance() would silently pass for the
    # subclass too. The contract is "stdlib Formatter, not the
    # subclass" so we check type() identity.
    assert type(formatter) is logging.Formatter, (
        f"non-journal mode must return the bare stdlib Formatter; "
        f"got {type(formatter).__name__}"
    )


# ---------------------------------------------------------------------------
# PII masking still runs in front of either formatter
# ---------------------------------------------------------------------------


def test_pii_masking_still_applies_under_journal_formatter() -> None:
    """The PII filter is attached to the handler (runs BEFORE the
    formatter). Switching formatters does not change PII handling.
    Locked down so a future refactor doesn't accidentally drop the
    filter when adjusting the formatter wiring."""
    pii_filter = PiiMaskingFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="user email is alice@example.com",
        args=(),
        exc_info=None,
    )

    # Filter mutates the record in place
    pii_filter.filter(record)

    formatter = _JournalPriorityFormatter(fmt="%(message)s")
    output = formatter.format(record)

    assert "alice@example.com" not in output
    assert "***" in output
    # Prefix still present even after masking
    assert output.startswith("<3>")


def test_pii_masking_still_applies_under_plain_formatter() -> None:
    """Same coverage for the non-journal path."""
    pii_filter = PiiMaskingFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="user email is bob@example.com",
        args=(),
        exc_info=None,
    )

    pii_filter.filter(record)

    formatter = logging.Formatter(fmt="%(message)s")
    output = formatter.format(record)

    assert "bob@example.com" not in output
    assert "***" in output
    # No prefix on the non-journal path
    assert not output.startswith("<")
