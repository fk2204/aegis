"""Plan 5.1 — pin the parse-time tampering-flag persistence.

The pipeline at ``src/aegis/parser/pipeline.py`` now appends a
visibility flag to ``all_flags`` when the tampering composition fires.
This file pins the helper's contract and the boundary cases the
operator cares about: shadow vs live prefix, fires vs doesn't-fire,
branch name forwarded verbatim.

The pipeline-level wiring (the helper actually being called) is
covered indirectly by the existing ``test_pipeline_e2e.py`` corpus
runs. Direct exercise of the helper here gives boundary coverage
without rebuilding a full bank-statement fixture per case — same
pattern the R1.7 ADB tests use.
"""

from __future__ import annotations

import pytest

from aegis.parser.pipeline import _tampering_persistence_flag
from aegis.parser.tampering import TamperingEvaluation


def _eval(
    *,
    fires: bool,
    branch: str = "none",
    metadata_score: int = 0,
    math_score: int = 0,
    contributing: tuple[str, ...] = (),
    rationale: str = "test",
) -> TamperingEvaluation:
    return TamperingEvaluation(
        fires=fires,
        branch=branch,  # type: ignore[arg-type]
        metadata_score=metadata_score,
        math_score=math_score,
        contributing_failures=list(contributing),
        rationale=rationale,
    )


# ----------------------------------------------------------------------
# Fires-vs-doesn't
# ----------------------------------------------------------------------


def test_returns_none_when_evaluation_does_not_fire() -> None:
    """An evaluation with ``fires=False`` produces no flag — caller
    appends nothing to all_flags."""
    assert _tampering_persistence_flag(_eval(fires=False), "shadow") is None
    assert _tampering_persistence_flag(_eval(fires=False), "live") is None


def test_returns_flag_when_evaluation_fires() -> None:
    """Firing evaluation produces a non-None flag string regardless of mode."""
    shadow = _tampering_persistence_flag(
        _eval(fires=True, branch="strong_metadata"), "shadow"
    )
    live = _tampering_persistence_flag(
        _eval(fires=True, branch="strong_metadata"), "live"
    )
    assert shadow is not None
    assert live is not None


# ----------------------------------------------------------------------
# Prefix is mode-dependent
# ----------------------------------------------------------------------


def test_shadow_mode_uses_shadow_prefix() -> None:
    """In the default mode the flag carries ``[SHADOW]`` — the
    close-queue's gating-reason label intentionally ignores SHADOW
    prefixes so this doesn't drive operator-facing decline framing."""
    flag = _tampering_persistence_flag(
        _eval(fires=True, branch="strong_metadata"), "shadow"
    )
    assert flag is not None
    assert flag.startswith("[SHADOW] ")
    assert "bank_statement_tampering_confirmed:strong_metadata" in flag


def test_live_mode_uses_meta_prefix() -> None:
    """In live mode the flag is META so the close-queue surfaces it
    as ``editor metadata`` — aligns with the live-mode score-time
    hard-decline framing."""
    flag = _tampering_persistence_flag(
        _eval(fires=True, branch="strong_metadata"), "live"
    )
    assert flag is not None
    assert flag.startswith("[META] ")
    assert "bank_statement_tampering_confirmed:strong_metadata" in flag


def test_unknown_mode_falls_back_to_shadow() -> None:
    """Defensive: an unexpected mode value should not crash and should
    default to the conservative SHADOW prefix (signal visible but
    won't drive operator-facing labels). Future-proof against a
    config flip that introduces additional modes."""
    flag = _tampering_persistence_flag(
        _eval(fires=True, branch="strong_metadata"), "future_mode"
    )
    assert flag is not None
    assert flag.startswith("[SHADOW] ")


# ----------------------------------------------------------------------
# Branch is forwarded verbatim — operator-distinguishable
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "branch", ["strong_metadata", "medium_corroborated"]
)
def test_branch_name_appears_in_flag(branch: str) -> None:
    """The branch name is appended after the reason so the operator
    can tell strong-metadata fires (auto-decline candidate) from
    medium-corroborated (operator-confirm) without re-running the
    composition."""
    flag = _tampering_persistence_flag(_eval(fires=True, branch=branch), "shadow")
    assert flag is not None
    assert f":{branch}" in flag


# ----------------------------------------------------------------------
# Stability — flag format is operator-facing, lock it
# ----------------------------------------------------------------------


def test_flag_format_is_stable() -> None:
    """The close-queue gating-reason classifier matches ``[CAT]`` flag
    prefixes exactly. A reformatted flag could silently break the
    categorization. Lock the literal format for both modes.
    """
    shadow = _tampering_persistence_flag(
        _eval(fires=True, branch="strong_metadata"), "shadow"
    )
    live = _tampering_persistence_flag(
        _eval(fires=True, branch="medium_corroborated"), "live"
    )
    assert shadow == "[SHADOW] bank_statement_tampering_confirmed:strong_metadata"
    assert live == "[META] bank_statement_tampering_confirmed:medium_corroborated"
