"""Structured logging with PII masking.

Two layers of defense:

1. **Key-based masking.** Any structured-log field whose key matches a known
   PII name is replaced with ``"***"`` before formatting. This catches the
   common case where merchant data flows through ``logger.info("...", extra={...})``.
2. **Value-pattern masking.** Free-text log messages are scanned for the
   shapes of email, phone, SSN, EIN, and bank account numbers. Matches are
   replaced in-place. This catches the case where PII is concatenated into
   a message string (which should not happen, but the logger refuses to
   trust callers).

Per CLAUDE.md the masked keys are:
    business_name, dba, owner_name, phone, email,
    address_*, bank_name, account_holder, account_last4,
    ssn, ssn_last4, ein, tax_id, owner_dob, transaction_description.

Use ``get_logger(__name__)`` everywhere. Never call ``logging.getLogger``
directly — that bypasses the masking filter.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Final

from aegis.config import get_settings

# Syslog priority codes (RFC 5424). systemd's SyslogLevelPrefix=true
# parses ``<N>`` at the start of each stderr line and uses N to
# classify the journal entry's priority. Without the prefix, Python
# ERROR/CRITICAL stderr lines land at systemd's default priority
# (notice / info depending on systemd config), and ``journalctl -p err``
# misses them — the blind-spot this module closes.
#
# Unmapped levels (e.g. ``logging.log(25, ...)`` for a custom NOTICE
# level) fall back to ERROR (3), NOT INFO (6). Rationale: this whole
# fix exists because INFO-default classification HID real ERROR
# lines. A custom level showing up too loud is recoverable noise; a
# custom level silently downgraded is invisible to ``-p err``, which
# is the exact failure mode we are closing.
_SYSLOG_PRIORITY_BY_LEVEL: Final[dict[int, int]] = {
    logging.CRITICAL: 2,  # crit
    logging.ERROR:    3,  # err
    logging.WARNING:  4,  # warning
    logging.INFO:     6,  # info
    logging.DEBUG:    7,  # debug
}
_UNMAPPED_PRIORITY_FALLBACK: Final[int] = 3  # err — fail loud, not invisible

_PII_KEYS: Final[frozenset[str]] = frozenset(
    {
        "business_name",
        "dba",
        "owner_name",
        "phone",
        "email",
        "bank_name",
        "account_holder",
        "account_last4",
        "ssn",
        "ssn_last4",
        "ein",
        "tax_id",
        "owner_dob",
        "transaction_description",
        "description",  # parser/Transaction.description is PII
    }
)

_PII_KEY_PREFIXES: Final[tuple[str, ...]] = ("address_",)

_MASK: Final[str] = "***"

# Value-pattern matchers. Each regex covers a single PII shape; the
# replacement keeps any leading/trailing punctuation captured around the
# match so logs remain readable.

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(
    r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EIN_RE = re.compile(r"\b\d{2}-\d{7}\b")
# Bare 9-16 digit runs catch raw account/SSN/EIN without separators.
_BARE_LONG_DIGITS_RE = re.compile(r"\b\d{9,16}\b")
# UUID shape (8-4-4-4-12 hex). Used to *protect* UUID substrings from the
# bare-long-digits mask: a uuid4 whose 12-char tail (or any other segment)
# happens to be all decimal digits would otherwise get its tail rewritten
# to ``***``. That mangles audit_log.details.decision_id — the link
# between an audit row and the decision row it pairs with — every ~0.3% of
# uuid4 values (intermittent test flake; silent production audit-trail
# corruption).
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _mask_value(value: object) -> object:
    """Recursively mask PII in dicts, lists, and strings; pass-through for scalars."""
    if isinstance(value, str):
        return _mask_string(value)
    if isinstance(value, dict):
        return {k: (_MASK if _is_pii_key(k) else _mask_value(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_mask_value(v) for v in value)
    return value


def _is_pii_key(key: str) -> bool:
    k = key.lower()
    if k in _PII_KEYS:
        return True
    return any(k.startswith(p) for p in _PII_KEY_PREFIXES)


def _mask_string(text: str) -> str:
    out = _EMAIL_RE.sub(_MASK, text)
    out = _SSN_RE.sub(_MASK, out)
    out = _EIN_RE.sub(_MASK, out)
    out = _PHONE_RE.sub(_MASK, out)
    out = _mask_bare_long_digits(out)
    return out


def _mask_bare_long_digits(text: str) -> str:
    """Apply ``_BARE_LONG_DIGITS_RE`` but skip runs inside a UUID.

    UUID hex segments are not PII — they are opaque identifiers. The
    bare-digit mask exists to catch raw account/SSN/EIN runs, not to
    rewrite uuid4 strings whose segments happen to be all decimal
    digits. Without this guard, ~0.3 % of uuid4 values get a ``***``
    substituted into their tail, which silently corrupts audit_log
    details (e.g. ``decision_id``).
    """
    uuid_spans = [m.span() for m in _UUID_RE.finditer(text)]
    if not uuid_spans:
        return _BARE_LONG_DIGITS_RE.sub(_MASK, text)

    def _replace(m: re.Match[str]) -> str:
        start, end = m.span()
        for u_start, u_end in uuid_spans:
            if start >= u_start and end <= u_end:
                return m.group(0)
        return _MASK

    return _BARE_LONG_DIGITS_RE.sub(_replace, text)


class PiiMaskingFilter(logging.Filter):
    """Masks PII in both the formatted message and any structured ``extra`` fields."""

    # Standard LogRecord attributes — never overwrite these.
    _RESERVED: Final[frozenset[str]] = frozenset(
        {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message", "asctime", "taskName",
        }
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            try:
                rendered = record.getMessage()
            except Exception:
                rendered = str(record.msg)
            record.msg = _mask_string(rendered)
            record.args = ()  # rendered already, don't double-substitute

        for attr, value in list(record.__dict__.items()):
            if attr in self._RESERVED or attr.startswith("_"):
                continue
            if _is_pii_key(attr):
                record.__dict__[attr] = _MASK
            else:
                record.__dict__[attr] = _mask_value(value)
        return True


class _JournalPriorityFormatter(logging.Formatter):
    """Wraps the stdlib Formatter and prepends the syslog priority
    prefix ``<N>`` so systemd's journal classifies the entry at the
    right priority.

    Used ONLY when stderr is connected to systemd-journald (detection
    is one-shot via the ``JOURNAL_STREAM`` env var, evaluated in
    ``_select_stream_formatter`` at logger setup — not per-line, not
    at module import). Paired with ``SyslogLevelPrefix=true`` in the
    systemd unit, which strips the prefix and uses it as the priority.

    Without this formatter + the systemd unit flag, every Python
    ERROR/CRITICAL line emitted via stderr lands at systemd's default
    priority and ``journalctl -p err`` returns ``-- No entries --``
    even when real errors fired. This blind-spot hid AEGIS errors
    across the whole codebase until the chunk-A bucket-absent verify
    surfaced it.
    """

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        priority = _SYSLOG_PRIORITY_BY_LEVEL.get(
            record.levelno, _UNMAPPED_PRIORITY_FALLBACK
        )
        return f"<{priority}>{line}"


def _select_stream_formatter() -> logging.Formatter:
    """Pick the right stderr Formatter based on launch context.

    * JOURNAL_STREAM in env (systemd has connected stderr to journald)
      → ``_JournalPriorityFormatter`` so ``journalctl -p err`` filters
      correctly.
    * JOURNAL_STREAM absent (Windows dev box, CI, plain terminal,
      pytest, REPL) → stdlib ``logging.Formatter``. Behavior on those
      paths is byte-for-byte unchanged from before this chunk —
      proven by ``tests/test_logger_journal_priority.py``.

    Detection happens once at logger setup, not per-line.
    """
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%S%z"
    if "JOURNAL_STREAM" in os.environ:
        return _JournalPriorityFormatter(fmt=fmt, datefmt=datefmt)
    return logging.Formatter(fmt=fmt, datefmt=datefmt)


_CONFIGURED = False


def configure_logging() -> None:
    """Install the PII masking filter on the root logger. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler()
    handler.setFormatter(_select_stream_formatter())
    handler.addFilter(PiiMaskingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger guaranteed to apply PII masking.

    Idempotent: callers may freely call this from any module. The first
    call installs the masking filter on the root logger; subsequent calls
    return the same configured logger by name.
    """
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)


def _mask_string_for_test(text: str) -> str:
    """Test-only re-export of the string masker (avoids leaking the helper)."""
    return _mask_string(text)


__all__ = [
    "PiiMaskingFilter",
    "configure_logging",
    "get_logger",
]
