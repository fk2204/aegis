"""Tests for ``aegis.web.routers.merchants.compute_merchant_status``.

Covers the precedence order documented in the helper's docstring:
    Error > Review > Parsing > Declined > Ready > No docs

Each test exercises one branch end-to-end and verifies the helper
returns the expected ``MerchantDealStatus`` label (rendered as a chip
by ``merchants.html.j2``).
"""

from __future__ import annotations

from aegis.web.routers.merchants import compute_merchant_status


def test_no_docs_returns_no_docs() -> None:
    # Zero documents — merchant has not uploaded anything yet.
    assert compute_merchant_status([], has_decline_decision=False) == "No docs"
    # An outstanding decline decision shouldn't pull a doc-less merchant
    # into the Declined band — the helper requires actual documents.
    assert compute_merchant_status([], has_decline_decision=True) == "No docs"


def test_error_wins_over_everything() -> None:
    # Error precedence: an ``error`` parse_status outranks pending,
    # manual_review, proceed, and any pending decline.
    assert (
        compute_merchant_status(
            ["proceed", "manual_review", "pending", "error"],
            has_decline_decision=True,
        )
        == "Error"
    )


def test_review_when_manual_review_present_no_error() -> None:
    # Review precedence: manual_review takes over once error is absent.
    # The ``review`` legacy synonym also routes to Review.
    assert (
        compute_merchant_status(
            ["proceed", "manual_review", "pending"],
            has_decline_decision=False,
        )
        == "Review"
    )
    assert (
        compute_merchant_status(["proceed", "review"], has_decline_decision=False) == "Review"
    )


def test_parsing_when_pending_present_no_review_no_error() -> None:
    # Parsing precedence: at least one pending doc and no upstream
    # error / manual_review.
    assert (
        compute_merchant_status(
            ["pending", "proceed"],
            has_decline_decision=False,
        )
        == "Parsing"
    )


def test_declined_when_no_active_doc_signal_but_decline_decision() -> None:
    # Declined precedence: docs exist, none in error / manual_review /
    # pending, no proceed doc to bump the merchant to Ready, AND the
    # latest decision is decline.
    # The only way to hit this with real ParseStatus values is a
    # ``decline`` parse_status (legacy enum value the parser doesn't
    # write today; defensive coverage for older rows).
    assert (
        compute_merchant_status(
            ["decline"],
            has_decline_decision=True,
        )
        == "Declined"
    )


def test_ready_when_proceed_and_no_blocking_states() -> None:
    # Ready precedence: at least one proceed doc, nothing pending,
    # nothing in error / manual_review, AND no decline decision on
    # file. Declined sits ABOVE Ready in the precedence order, so the
    # only path to Ready is "proceed doc + no pending/error/review +
    # no decline decision".
    assert (
        compute_merchant_status(
            ["proceed", "proceed"],
            has_decline_decision=False,
        )
        == "Ready"
    )


def test_declined_beats_ready_when_decline_decision_present() -> None:
    # Declined sits above Ready in the precedence order. A merchant
    # with a clean ``proceed`` doc but a persisted ``decline`` decision
    # is shown as Declined, not Ready. This is the operator-facing
    # signal that the deal already got an answer.
    assert (
        compute_merchant_status(
            ["proceed"],
            has_decline_decision=True,
        )
        == "Declined"
    )
