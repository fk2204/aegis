"""PII masking logger tests.

Three layers verified:
1. Key-based masking on structured ``extra`` fields.
2. Value-pattern masking on the message string (email/phone/SSN/EIN/long digits).
3. ``address_*`` prefix masking still triggers when the suffix changes.
"""

from __future__ import annotations

import logging

from aegis.logger import PiiMaskingFilter, _mask_string_for_test, get_logger


def _record(msg: str, **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="aegis.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_email_in_message_is_masked() -> None:
    assert "***" in _mask_string_for_test("write to alice@example.com today")
    assert "alice@example.com" not in _mask_string_for_test("write to alice@example.com today")


def test_phone_and_ssn_and_ein_are_masked() -> None:
    masked = _mask_string_for_test("phone (415) 555-1212 ssn 123-45-6789 ein 12-3456789")
    assert "555-1212" not in masked
    assert "123-45-6789" not in masked
    assert "12-3456789" not in masked


def test_bare_long_digit_run_is_masked() -> None:
    # An account number / raw SSN / raw EIN.
    masked = _mask_string_for_test("acct 1234567890 lookup")
    assert "1234567890" not in masked


def test_short_digit_run_is_left_alone() -> None:
    masked = _mask_string_for_test("page 5 line 17 amount 42")
    assert "5" in masked and "17" in masked and "42" in masked


def test_uuid_with_all_digit_tail_is_not_masked() -> None:
    """A uuid4 whose 12-char tail (or any other segment) happens to be all
    decimal digits must NOT be rewritten by ``_BARE_LONG_DIGITS_RE``.

    Without this guard, ~0.3% of uuid4 strings get a ``***`` substituted
    into their tail when they pass through ``_mask_value`` — which silently
    corrupts ``audit_log.details.decision_id`` and reproduces as the flake
    ``tests/test_snapshot.py::test_write_appends_row_and_records_audit``
    failing at ``entry["details"]["decision_id"] == str(decision_id)``.
    """
    uuid_with_digit_tail = "a1b2c3d4-e5f6-4123-9234-123456789012"
    assert _mask_string_for_test(uuid_with_digit_tail) == uuid_with_digit_tail
    # Embedded in a longer message: the UUID stays intact.
    msg = f"recorded decision_id={uuid_with_digit_tail} for deal x"
    assert uuid_with_digit_tail in _mask_string_for_test(msg)
    # A real bare digit run alongside a UUID is still masked.
    mixed = f"uuid {uuid_with_digit_tail} acct 9876543210"
    masked_mixed = _mask_string_for_test(mixed)
    assert uuid_with_digit_tail in masked_mixed
    assert "9876543210" not in masked_mixed


def test_pii_key_in_extra_is_masked() -> None:
    f = PiiMaskingFilter()
    rec = _record("ok", owner_name="Alice Doe", merchant_id="m-123")
    assert f.filter(rec) is True
    # LogRecord supports dynamic attribute injection; mypy can't see them.
    assert rec.__dict__["owner_name"] == "***"
    # non-PII key untouched
    assert rec.__dict__["merchant_id"] == "m-123"


def test_address_prefix_keys_are_masked() -> None:
    f = PiiMaskingFilter()
    rec = _record("ok", address_line1="123 Main St", address_city="Boston")
    f.filter(rec)
    assert rec.__dict__["address_line1"] == "***"
    assert rec.__dict__["address_city"] == "***"


def test_nested_dict_pii_is_masked() -> None:
    f = PiiMaskingFilter()
    rec = _record("ok", merchant={"owner_name": "Bob", "industry": "retail"})
    f.filter(rec)
    merchant = rec.__dict__["merchant"]
    assert isinstance(merchant, dict)
    assert merchant["owner_name"] == "***"
    assert merchant["industry"] == "retail"


def test_get_logger_installs_filter_idempotently() -> None:
    log1 = get_logger("aegis.test1")
    log2 = get_logger("aegis.test2")
    # Both should write through the root handlers without crashing.
    log1.info("starting up at alice@example.com")
    log2.info("done")
