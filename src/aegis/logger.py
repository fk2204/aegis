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
import re
from typing import Final

from aegis.config import get_settings

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
    out = _BARE_LONG_DIGITS_RE.sub(_MASK, out)
    return out


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


_CONFIGURED = False


def configure_logging() -> None:
    """Install the PII masking filter on the root logger. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
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
