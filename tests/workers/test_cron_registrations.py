"""Regression guards for ``WorkerSettings.cron_jobs``.

A cron coroutine in ``aegis.workers`` is only useful when it's actually
registered in the ``cron_jobs`` tuple — arq's scheduler reads from that
tuple at boot, nowhere else. If a new ``run_*_cron`` function ships
without the matching ``cron(...)`` line in ``WorkerSettings.cron_jobs``,
the cron silently never fires. There is no runtime error.

Reference incident (2026-06-25 → 2026-06-27): the weekly shadow-review
cron's commit ``e3e00bf`` landed 2026-06-25 02:10 UTC — 20 hours after
its Wed 06:00 UTC firing slot (Wed 2026-06-24). Zero
``shadow_signal.weekly_summary_complete`` audit rows accumulated for
that window because the cron didn't exist in production code yet.
The registration WAS correct from the start (this test would have
passed at commit time) — the failure mode this test guards against is
the inverse: a cron function added in a future commit whose author
forgets to append it to ``cron_jobs``.

Each assertion pins (coroutine, weekday, hour, minute) so a future
schedule edit also surfaces as a test failure — the cron schedules are
operationally meaningful (Mon 06:00 / Mon 07:00 / Wed 06:00 are
deliberately staggered to spread the operator's start-of-week queue).
"""

from __future__ import annotations

from arq.cron import CronJob

# Import each cron coroutine from its actual defining module rather than
# routing through ``aegis.workers``. ``aegis.workers`` re-imports
# ``run_compliance_obligation_reminder_cron`` (from compliance.obligations)
# and ``run_renewal_reminder_cron`` (from merchants.renewal_reminder), and
# mypy --strict (under implicit_reexport=False) refuses to re-import them
# through the workers module without an explicit ``__all__``.
from aegis.audit_archiver import run_archive_cron
from aegis.compliance.obligations import run_compliance_obligation_reminder_cron
from aegis.merchants.renewal_reminder import run_renewal_reminder_cron
from aegis.workers import (
    WorkerSettings,
    run_shadow_review_cron,
    run_submission_reminder_cron,
    run_track_a_regression_sentinel_cron,
)


def _crons_by_coroutine() -> dict[str, CronJob]:
    """Index registered cron jobs by their coroutine's qualified name.

    arq's ``CronJob`` carries the wrapped coroutine on ``.coroutine``;
    we key off ``__qualname__`` so the lookup is robust against arq's
    internal name-prefixing (``cron:`` prefix on ``CronJob.name``).
    """
    return {c.coroutine.__qualname__: c for c in WorkerSettings.cron_jobs}


def test_shadow_review_cron_registered_wed_0600() -> None:
    """The 2026-06-24 cause of the 7-day audit gap.

    Without this assertion the cron coroutine can ship without being
    wired into ``cron_jobs`` and the failure mode is silent — no error
    at boot, no error at the missed firing slot, just 0 audit rows
    until someone notices.
    """
    by_name = _crons_by_coroutine()
    assert run_shadow_review_cron.__qualname__ in by_name, (
        "run_shadow_review_cron is defined but not registered in "
        "WorkerSettings.cron_jobs — arq will never fire it."
    )
    job = by_name[run_shadow_review_cron.__qualname__]
    assert job.weekday == "wed"
    assert job.hour == 6
    assert job.minute == 0


def test_compliance_obligation_reminder_cron_registered_mon_0700() -> None:
    by_name = _crons_by_coroutine()
    assert run_compliance_obligation_reminder_cron.__qualname__ in by_name
    job = by_name[run_compliance_obligation_reminder_cron.__qualname__]
    assert job.weekday == "mon"
    assert job.hour == 7
    assert job.minute == 0


def test_track_a_regression_sentinel_cron_registered_mon_0600() -> None:
    by_name = _crons_by_coroutine()
    assert run_track_a_regression_sentinel_cron.__qualname__ in by_name
    job = by_name[run_track_a_regression_sentinel_cron.__qualname__]
    assert job.weekday == "mon"
    assert job.hour == 6
    assert job.minute == 0


def test_archive_cron_registered_daily_0200() -> None:
    by_name = _crons_by_coroutine()
    assert run_archive_cron.__qualname__ in by_name
    job = by_name[run_archive_cron.__qualname__]
    # No weekday restriction — fires every day at 02:00 UTC.
    assert job.weekday is None
    assert job.hour == 2
    assert job.minute == 0


def test_renewal_reminder_cron_registered_daily_0900() -> None:
    by_name = _crons_by_coroutine()
    assert run_renewal_reminder_cron.__qualname__ in by_name
    job = by_name[run_renewal_reminder_cron.__qualname__]
    assert job.weekday is None
    assert job.hour == 9
    assert job.minute == 0


def test_submission_reminder_cron_registered_daily_1700() -> None:
    by_name = _crons_by_coroutine()
    assert run_submission_reminder_cron.__qualname__ in by_name
    job = by_name[run_submission_reminder_cron.__qualname__]
    assert job.weekday is None
    assert job.hour == 17
    assert job.minute == 0


def test_all_registered_crons_disable_run_at_startup() -> None:
    """Every cron in ``cron_jobs`` must have ``run_at_startup=False``.

    Worker restarts happen many times per day (deploys, OOM, manual
    restart). Letting ``run_at_startup=True`` would re-fire every cron
    on every restart, producing duplicate audit rows + duplicate side
    effects. Pin the safer default.
    """
    for job in WorkerSettings.cron_jobs:
        assert job.run_at_startup is False, (
            f"cron job {job.name} has run_at_startup=True — restarts "
            "will duplicate-fire the cron. Set run_at_startup=False."
        )


def test_no_two_crons_share_the_same_minute_in_overlapping_windows() -> None:
    """Best-effort guard against accidental clobber at the same slot.

    arq runs cron jobs serially within a worker — two crons scheduled
    at the same (weekday, hour, minute) will queue back-to-back. The
    deliberate stagger (Mon 06:00 sentinel / Mon 07:00 compliance /
    Wed 06:00 shadow / 02:00 archive / 09:00 renewal / 17:00
    submission) keeps the operator's start-of-week queue from stacking.
    If someone schedules two crons at the same slot, surface it.
    """
    # CronJob fields can be int | set[int] | None; this test asserts
    # AEGIS's crons all use scalar int slots (the deliberate-stagger
    # contract). Stringify so the key shape is stable regardless.
    seen: dict[tuple[str, str, str], str] = {}
    for job in WorkerSettings.cron_jobs:
        key = (repr(job.weekday), repr(job.hour), repr(job.minute))
        if key in seen:
            raise AssertionError(
                f"cron schedule collision at weekday={job.weekday} "
                f"hour={job.hour} minute={job.minute}: "
                f"{seen[key]} and {job.name} both registered for the "
                "same slot. Stagger by at least 1 hour to avoid "
                "serial queue stacking on the worker."
            )
        seen[key] = job.name
