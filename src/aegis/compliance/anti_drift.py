"""Boot-time anti-drift checks for the compliance surface (mp Phase 3).

Three checks run on startup, in order of failure severity:

1. **states.yaml version log** — emits the loaded matrix version so the
   operator can confirm at-a-glance which regulation table is in use.
   Fail-closed on missing / invalid YAML is handled by
   ``aegis.compliance.state_matrix.load_matrix`` itself; this module
   surfaces the version to the structured log.

2. **Per-state template SHA256 log** — for each Tier 1 state that
   declares a ``template_path`` in states.yaml, compute the SHA256 of
   the template file and emit it. A divergence between the template's
   hash here and the snapshot test hash means the prescribed-form
   template drifted without the snapshot test catching it — a serious
   compliance audit signal.

3. **Overdue review warning** — read each
   ``docs/compliance/states/{ST}/07_audit_meta.yaml`` file and warn (do
   not fail) if ``next_review_due`` is in the past. Overdue reviews are
   a quarterly-rhythm concern, not a boot-blocking one.

The state_matrix dependency is imported lazily so this module works
even when Agent 1A's work has not yet landed on the same branch tree.
Once 1A's branch merges, the matrix-version + template-SHA path lights
up automatically. The yaml dependency is also lazy for the same
reason — pyyaml is added by 1A's pyproject.toml change.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Final

from aegis.logger import get_logger

_log = get_logger(__name__)


# Repo root resolved from this file's location: src/aegis/compliance/anti_drift.py
# parents[3] = repo root.
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
STATES_DIR: Final[Path] = REPO_ROOT / "docs" / "compliance" / "states"


def run_boot_checks() -> None:
    """Run all three boot-time anti-drift checks.

    Idempotent. Safe to call once at app startup.
    """
    _log_matrix_version_and_templates()
    _warn_overdue_reviews()


def _log_matrix_version_and_templates() -> None:
    """Log states.yaml version + per-template SHA256.

    Skips silently when ``aegis.compliance.state_matrix`` is not yet on
    this branch (transitional state before Agent 1A merges).
    """
    try:
        # state_matrix is built by master plan Phase 1. Lazy import keeps
        # this module loadable even when state_matrix is absent; runtime
        # behavior is unchanged either way.
        import aegis.compliance.state_matrix as _state_matrix
    except ImportError:
        _log.info(
            "compliance.anti_drift.matrix_not_available",
            extra={"reason": "state_matrix module not yet on this branch"},
        )
        return

    matrix = _state_matrix.load_matrix()
    _log.info(
        "compliance.anti_drift.matrix_loaded",
        extra={"matrix_version": matrix.version},
    )

    for code, reg in sorted(matrix.states.items()):
        if not isinstance(reg, _state_matrix.Tier1Regulation):
            continue
        if reg.cfdl.template_path is None:
            continue
        template = REPO_ROOT / reg.cfdl.template_path
        if not template.exists():
            _log.warning(
                "compliance.anti_drift.template_missing",
                extra={
                    "state_code": code,
                    "template_path": str(reg.cfdl.template_path),
                },
            )
            continue
        digest = hashlib.sha256(template.read_bytes()).hexdigest()
        _log.info(
            "compliance.anti_drift.template_sha256",
            extra={
                "state_code": code,
                "template_path": str(reg.cfdl.template_path),
                "sha256": digest,
            },
        )


def _warn_overdue_reviews() -> None:
    """Warn when any per-state 07_audit_meta.yaml is past next_review_due.

    Reads every ``docs/compliance/states/{ST}/07_audit_meta.yaml`` that
    exists. Skips files that don't parse or have no ``next_review_due``
    field (placeholders). Never raises — overdue reviews are warnings,
    not boot-blockers.

    Imports ``yaml`` lazily so this module is importable even before
    Agent 1A's branch (which adds pyyaml to pyproject.toml) merges. When
    pyyaml is missing, the function logs and returns; it does not fail
    the boot.
    """
    if not STATES_DIR.is_dir():
        # Folder structure absent: pre-skeleton-commit state.
        return

    try:
        import yaml
    except ImportError:
        _log.info(
            "compliance.anti_drift.yaml_not_available",
            extra={"reason": "pyyaml not installed; skipping overdue-review scan"},
        )
        return

    today = date.today()
    for state_dir in sorted(STATES_DIR.iterdir()):
        if not state_dir.is_dir():
            continue
        meta_path = state_dir / "07_audit_meta.yaml"
        if not meta_path.is_file():
            continue
        try:
            raw = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            _log.warning(
                "compliance.anti_drift.audit_meta_parse_failed",
                extra={"state_code": state_dir.name, "error": str(exc)},
            )
            continue
        if not isinstance(raw, dict):
            continue
        due = raw.get("next_review_due")
        if due is None:
            continue
        if not isinstance(due, date):
            # YAML can also parse as string when malformed; skip rather
            # than guess.
            continue
        if due < today:
            _log.warning(
                "compliance.anti_drift.review_overdue",
                extra={
                    "state_code": state_dir.name,
                    "next_review_due": due.isoformat(),
                    "days_overdue": (today - due).days,
                },
            )


__all__ = ["REPO_ROOT", "STATES_DIR", "run_boot_checks"]
