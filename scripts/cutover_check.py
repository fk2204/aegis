"""One-command cutover readiness check.

Runs the four pre-cutover gates the operator otherwise has to invoke
manually:

  1. Tampering shadow-rule review — surfaces any tampering-rule fires
     that need operator triage before the shadow→live flip.
     ``scripts/tampering_shadow_review.py`` — exit 0 (no fires) or 3
     (fires present, operator review required) both count as PASS for
     "the diagnostic ran"; only 1 (runtime error) FAILS. The number of
     fires is surfaced in the summary so the operator sees the volume
     without re-reading stdout.
  2. Track A historical lookback — confirms Track A's integrity verdict
     catches every legacy ``fraud_score_critical`` hard-decline.
     ``scripts/track_a_historical_lookback.py`` — exit 0 PASS, exit 3
     ("regressions present — Track A misses what legacy catches")
     FAILS, exit 1 (runtime error) FAILS. This one is a hard cutover
     blocker: Track A must not regress.
  3. Pending migrations check — runs ``apply_migrations.py --dry-run``
     against the chosen target. Exit 0 PASS regardless of pending
     count (the count itself is just informational — the operator
     decides whether to apply); exit != 0 FAILS.
  4. Prod health probe — db_verify.py CHECK=prod-health TARGET=<target>.
     Confirms the DB is reachable AND has recent audit_log activity
     (one row in the last hour). Exit 0 PASS, anything else FAILS.

Output: per-check status line during the run, then a final summary
block. Exit 0 only when EVERY check passes. The operator can paste
the entire output into a cutover log and have an auditable trail.

This script is deliberately a thin orchestrator: each gate already has
its own deterministic exit-code contract, and the orchestrator just
fans them out + aggregates. Bug-finding stays in the individual
scripts; cutover-check stays a stable pass/fail surface.

Usage::

    make cutover-check TARGET=prod
    uv run python scripts/cutover_check.py --target prod
    uv run python scripts/cutover_check.py --target prod --skip prod-health

``--skip`` accepts a comma-separated list of check names
(``tampering``, ``track_a``, ``pending_migrations``, ``prod_health``).
Skipped checks render as ``SKIP`` and do NOT participate in the final
verdict. Use sparingly — a green cutover-check that skipped half its
gates means nothing.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

EXIT_OK: Final[int] = 0
EXIT_FAIL: Final[int] = 1
EXIT_USAGE: Final[int] = 2

# Exit codes that count as PASS for each individual check. Anything outside
# the allowed set FAILS the check. tampering_shadow_review treats exit 3 as
# "fires present, operator review required" — that's not a runtime failure,
# just a signal the operator should look. We treat both 0 and 3 as pass.
_TAMPERING_PASS_CODES: Final[frozenset[int]] = frozenset({0, 3})

# track_a_historical_lookback exit 3 means at least one regression (Track A
# misses what legacy would decline). That IS a cutover blocker — flip back
# to PASS only on 0.
_TRACK_A_PASS_CODES: Final[frozenset[int]] = frozenset({0})

# apply_migrations.py --dry-run returns 0 on success regardless of pending
# count. Anything else (2 config, 3 drift, 4 lock, 1 misc) is a hard fail.
_MIGRATIONS_PASS_CODES: Final[frozenset[int]] = frozenset({0})

# db_verify returns 0 on success.
_PROD_HEALTH_PASS_CODES: Final[frozenset[int]] = frozenset({0})


@dataclass(frozen=True)
class CheckOutcome:
    """One check's verdict + capture.

    ``status`` is one of ``"PASS"`` / ``"FAIL"`` / ``"SKIP"``. ``detail``
    is a one-line operator-readable summary surfaced in the final block.
    ``exit_code`` is ``None`` for SKIP, otherwise the subprocess exit
    code (so the operator can grep for it).
    """

    name: str
    status: str
    detail: str
    exit_code: int | None


@dataclass(frozen=True)
class CheckSpec:
    """One step in the cutover sequence.

    ``cmd`` is a list of argv-style tokens; ``pass_codes`` is the set
    of subprocess exit codes that count as PASS. ``detail_from_output``
    is an optional callable that produces a one-line operator summary
    from (exit_code, stdout, stderr); defaults to a generic
    ``"exit=<n>"`` rendering.
    """

    name: str
    label: str
    cmd: list[str]
    pass_codes: frozenset[int]


def _python_argv(*args: str) -> list[str]:
    """Build a list of argv tokens that invokes Python at the same
    interpreter the cutover_check is running under. Keeps cross-platform
    behavior (PowerShell vs bash, .venv vs system) stable: every check
    runs through ``sys.executable``."""
    return [sys.executable, *args]


def _build_specs(target: str) -> list[CheckSpec]:
    """Construct the four standard check specs for a given target.

    Per-target wiring: every gate that takes a target receives it
    verbatim. The tampering review and Track A lookback consume the
    same DB the application uses (DATABASE_URL); they don't take a
    --target flag, but they DO need a working DSN — same prerequisite
    as the other two checks.
    """
    return [
        CheckSpec(
            name="tampering",
            label="Tampering shadow review",
            cmd=_python_argv(
                str(REPO_ROOT / "scripts" / "tampering_shadow_review.py"),
            ),
            pass_codes=_TAMPERING_PASS_CODES,
        ),
        CheckSpec(
            name="track_a",
            label="Track A historical lookback",
            cmd=_python_argv(
                str(REPO_ROOT / "scripts" / "track_a_historical_lookback.py"),
                # Orphaned documents (merchant_id IS NULL) have no
                # merchant context for Track A to reason about — they
                # would surface as false-positive misses and FAIL the
                # cutover gate for noise rather than a real regression.
                # Future inserts will be blocked at the schema level
                # (migration 059); this flag handles legacy orphans.
                "--skip-orphans",
            ),
            pass_codes=_TRACK_A_PASS_CODES,
        ),
        CheckSpec(
            name="pending_migrations",
            label="Pending migrations (dry-run)",
            cmd=_python_argv(
                str(REPO_ROOT / "scripts" / "apply_migrations.py"),
                "--target",
                target,
                "--dry-run",
            ),
            pass_codes=_MIGRATIONS_PASS_CODES,
        ),
        CheckSpec(
            name="prod_health",
            label="Prod health probe",
            cmd=_python_argv(
                str(REPO_ROOT / "scripts" / "db_verify.py"),
                "--target",
                target,
                "--check",
                "prod-health",
            ),
            pass_codes=_PROD_HEALTH_PASS_CODES,
        ),
    ]


def _load_dotenv() -> dict[str, str]:
    """Read repo-root .env and .env.local into a dict, NOT into os.environ.

    Returned dict is later merged with ``os.environ`` and handed to every
    subprocess via ``env=``. We do not modify the parent process env so
    a unit test that monkeypatches subprocess.run doesn't have to
    teardown side-effects on the test runner's environment.

    Mirrors db_verify._load_dotenv_local: shell-style ``KEY=value`` lines,
    ``#`` comments ignored, surrounding quotes stripped, lines already
    present in os.environ are NOT shadowed.
    """
    loaded: dict[str, str] = {}
    for path in (REPO_ROOT / ".env", REPO_ROOT / ".env.local"):
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in os.environ:
                continue
            loaded.setdefault(key, value)
    return loaded


def _subprocess_env() -> dict[str, str]:
    """Build the env dict subprocesses inherit.

    Starts from ``os.environ`` so PATH / SystemRoot etc. propagate, layers
    on .env values. This makes cutover_check work the same whether
    invoked via ``make cutover-check`` (Makefile already runs through
    ``uv run`` so env is loaded) or directly via
    ``python scripts/cutover_check.py`` from a plain shell.
    """
    env = dict(os.environ)
    for key, value in _load_dotenv().items():
        env.setdefault(key, value)
    return env


def _run_check(spec: CheckSpec) -> CheckOutcome:
    """Run one check subprocess and project the outcome.

    Captures stdout + stderr but does NOT echo them line-by-line — the
    final summary surfaces a one-liner. The operator wanting deep-dive
    output runs the underlying script directly.
    """
    try:
        completed = subprocess.run(
            spec.cmd,
            cwd=REPO_ROOT,
            env=_subprocess_env(),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        # Mis-pathed script or missing interpreter. Hard fail — the
        # operator needs to fix the install before re-running.
        return CheckOutcome(
            name=spec.name,
            status="FAIL",
            detail=f"command not found: {exc}",
            exit_code=None,
        )
    except OSError as exc:
        return CheckOutcome(
            name=spec.name,
            status="FAIL",
            detail=f"OS error running {shlex.join(spec.cmd)}: {exc}",
            exit_code=None,
        )

    return _project_outcome(spec, completed.returncode, completed.stdout, completed.stderr)


def _project_outcome(
    spec: CheckSpec,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> CheckOutcome:
    """Build the CheckOutcome from a finished subprocess.

    Pure function — no I/O. Lets us unit-test the pass/fail projection
    without exec'ing any of the underlying scripts.
    """
    if exit_code in spec.pass_codes:
        status = "PASS"
    else:
        status = "FAIL"
    detail = _summarise(spec, exit_code, stdout, stderr)
    return CheckOutcome(name=spec.name, status=status, detail=detail, exit_code=exit_code)


def _summarise(spec: CheckSpec, exit_code: int, stdout: str, stderr: str) -> str:
    """Produce a one-line operator summary for the outcome.

    Prefers the last non-blank line of stdout when present (every wired
    check ends with a summary line by design — db_verify prints
    ``OVERALL: PASS|FAIL``, apply_migrations prints
    ``Target: ... pending=N``, the tampering/Track A scripts both
    print row-count summaries). Falls back to stderr's last line for
    failures with no stdout. Final fallback: bare ``exit=<n>``.
    """
    last_stdout = _last_meaningful_line(stdout)
    if last_stdout is not None:
        return f"exit={exit_code} · {last_stdout}"
    last_stderr = _last_meaningful_line(stderr)
    if last_stderr is not None:
        return f"exit={exit_code} · {last_stderr}"
    return f"exit={exit_code}"


def _last_meaningful_line(text: str) -> str | None:
    """Return the last non-blank line of ``text`` trimmed to ~120 chars.

    Crash tracebacks tend to be long; the trim keeps the summary block
    one screenful even on a failed check. Operators wanting the full
    output run the script directly.
    """
    for raw in reversed(text.splitlines()):
        line = raw.strip()
        if line:
            return line[:120]
    return None


def _parse_skip_list(value: str | None) -> set[str]:
    """Comma-separated list of check names to skip. Empty string and
    ``None`` both produce the empty set."""
    if not value:
        return set()
    return {tok.strip() for tok in value.split(",") if tok.strip()}


def _print_check_line(outcome: CheckOutcome, label: str) -> None:
    """One-line per-check progress as the run unfolds. Operators want
    incremental feedback — a 60-second wait with no output looks dead."""
    print(f"  [{outcome.status:<4}] {label} — {outcome.detail}", flush=True)


def _print_summary(outcomes: list[CheckOutcome], specs: list[CheckSpec]) -> None:
    """Final verdict block: per-check status + overall PASS/FAIL."""
    label_by_name = {s.name: s.label for s in specs}
    print()
    print("=" * 70)
    print("CUTOVER CHECK SUMMARY")
    print("=" * 70)
    for o in outcomes:
        label = label_by_name.get(o.name, o.name)
        print(f"  {o.status:<4}  {label}")
    print("-" * 70)
    fails = [o for o in outcomes if o.status == "FAIL"]
    skips = [o for o in outcomes if o.status == "SKIP"]
    if fails:
        print(f"OVERALL: FAIL ({len(fails)} of {len(outcomes)} checks failed)")
        for o in fails:
            print(f"         · {o.name}: {o.detail}")
    elif skips:
        # Surface the skip volume so operators can't accidentally cite a
        # half-skipped green run as cutover readiness.
        print(
            f"OVERALL: PASS ({len(outcomes) - len(skips)}/{len(outcomes)} ran; "
            f"{len(skips)} skipped)"
        )
    else:
        print(f"OVERALL: PASS ({len(outcomes)}/{len(outcomes)} checks)")
    print("=" * 70)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        choices=("dev", "staging", "prod"),
        required=True,
        help="Which environment to gate.",
    )
    parser.add_argument(
        "--skip",
        default=None,
        help=(
            "Comma-separated list of check names to skip "
            "(tampering, track_a, pending_migrations, prod_health). "
            "Use sparingly."
        ),
    )
    args = parser.parse_args(argv)
    return run(target=args.target, skip=_parse_skip_list(args.skip))


def run(*, target: str, skip: set[str]) -> int:
    """Programmatic entry point — drives ``run`` without an argv parse.

    Kept separate from ``main`` so tests can drive the orchestrator
    directly with already-resolved arguments.
    """
    specs = _build_specs(target)
    valid_names = {s.name for s in specs}
    unknown = skip - valid_names
    if unknown:
        print(
            f"ERROR: unknown --skip name(s): {sorted(unknown)}. Valid names: {sorted(valid_names)}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    print(f"Running cutover check (target={target})...", flush=True)
    if skip:
        print(f"  Skipping: {sorted(skip)}", flush=True)

    outcomes: list[CheckOutcome] = []
    for spec in specs:
        if spec.name in skip:
            outcome = CheckOutcome(
                name=spec.name,
                status="SKIP",
                detail="skipped via --skip",
                exit_code=None,
            )
        else:
            outcome = _run_check(spec)
        outcomes.append(outcome)
        _print_check_line(outcome, spec.label)

    _print_summary(outcomes, specs)
    return EXIT_OK if all(o.status != "FAIL" for o in outcomes) else EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
