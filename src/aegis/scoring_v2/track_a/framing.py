"""Per-branch rationale copy for Track A verdicts.

Lives as code (not template) for the same reasons Track B/C framing
does: changes are code-reviewable and consistent across the dossier,
API, and PDF surfaces.
"""

from __future__ import annotations


def frame_strong_metadata(metadata_score: int) -> str:
    return (
        f"FAIL (strong_metadata) — metadata_score={metadata_score} >= 50. "
        "Hard editor / forged author / structural anomaly. Auto-decline-"
        "eligible at Step 2; informational today."
    )


def frame_drift_plus_editor(editor_flag: str, drift_count: int, metadata_score: int) -> str:
    short_editor = editor_flag.replace("editor_detected: ", "")
    # F1a guard: IntegrityVerdict.rationale is max_length=320 (models.py).
    # Static skeleton here is ~247 chars; long vendor / version strings
    # (anything beyond ~60 chars of short_editor) push the rationale past
    # the Pydantic limit, which catches as a ValidationError and silently
    # downgrades the Track A verdict to None. Truncate to keep the vendor
    # name visible (the actionable bit) and drop the version/build suffix.
    if len(short_editor) > 60:
        short_editor = short_editor[:57] + "..."
    return (
        f"FAIL (drift_plus_editor) — editor metadata ({short_editor}) + "
        f"{drift_count} reconciliation failure(s) corroborate. The "
        "competent-fabrication signature: drift alone could be OCR, "
        "editor alone could be Preview-export, but both together is the "
        "tampering pattern (metadata_score="
        f"{metadata_score})."
    )


def frame_medium_corroborated(metadata_score: int, drift_count: int) -> str:
    return (
        f"REVIEW (medium_corroborated) — metadata_score={metadata_score} "
        f"(medium, 25-49) + {drift_count} reconciliation failure(s). "
        "Math/structural corroboration of medium-metadata signal — not "
        "strong enough alone to fail, but the combination warrants "
        "underwriter review."
    )


def frame_drift_alone(drift_count: int, metadata_score: int) -> str:
    return (
        f"REVIEW (drift_alone) — {drift_count} reconciliation failure(s) "
        f"with no editor metadata (metadata_score={metadata_score}). "
        "Could be genuine OCR or parser miss; could be drift. Underwriter "
        "review distinguishes the two."
    )


__all__ = [
    "frame_drift_alone",
    "frame_drift_plus_editor",
    "frame_medium_corroborated",
    "frame_strong_metadata",
]
