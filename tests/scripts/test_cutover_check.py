"""Unit tests for ``scripts/cutover_check.py``.

The orchestrator is deliberately a thin layer: each gate already has
its own deterministic exit-code contract. These tests pin:

* the pure-function projection from (exit_code, stdout, stderr) to
  PASS / FAIL, including the tampering "exit 3 is fine" carve-out and
  the Track A "exit 3 is a regression" stricter rule;
* the orchestrator returns 0 when every check passes, 1 when any one
  fails, regardless of which check failed;
* ``--skip`` removes a check from the verdict without crashing;
* ``--skip`` with an unknown name produces a usage-style 2;
* a subprocess that can't even start (FileNotFoundError) renders as
  FAIL with a useful detail, not an uncaught traceback.

No subprocesses are actually exec'd — every call to subprocess.run is
faked via monkeypatch so the test pinpoints the orchestrator logic
without depending on the live tampering / Track A scripts (those have
their own test suites).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from scripts import cutover_check

# ---------------------------------------------------------------------------
# Pure-function projection: _project_outcome + _summarise
# ---------------------------------------------------------------------------


def _spec(
    name: str = "tampering",
    pass_codes: frozenset[int] | None = None,
) -> cutover_check.CheckSpec:
    return cutover_check.CheckSpec(
        name=name,
        label=name.replace("_", " ").title(),
        cmd=["python", "-c", "0"],
        pass_codes=pass_codes if pass_codes is not None else frozenset({0}),
    )


def test_project_outcome_pass_when_exit_in_allowed_set() -> None:
    spec = _spec(pass_codes=frozenset({0, 3}))
    o = cutover_check._project_outcome(spec, exit_code=3, stdout="fires=2\n", stderr="")
    assert o.status == "PASS"
    assert o.exit_code == 3
    assert "fires=2" in o.detail


def test_project_outcome_fail_when_exit_outside_allowed_set() -> None:
    spec = _spec(pass_codes=frozenset({0}))
    o = cutover_check._project_outcome(spec, exit_code=3, stdout="miss=1\n", stderr="")
    assert o.status == "FAIL"
    assert o.exit_code == 3


def test_summarise_prefers_last_meaningful_stdout_line() -> None:
    spec = _spec()
    detail = cutover_check._summarise(
        spec,
        exit_code=0,
        stdout="header\nrunning...\n\nDONE: 5 rows\n",
        stderr="",
    )
    assert detail == "exit=0 · DONE: 5 rows"


def test_summarise_falls_back_to_stderr_when_stdout_empty() -> None:
    spec = _spec()
    detail = cutover_check._summarise(
        spec,
        exit_code=2,
        stdout="",
        stderr="ERROR: bad config\n",
    )
    assert "ERROR: bad config" in detail
    assert detail.startswith("exit=2")


def test_summarise_falls_back_to_bare_exit_when_no_output() -> None:
    spec = _spec()
    detail = cutover_check._summarise(spec, exit_code=1, stdout="", stderr="")
    assert detail == "exit=1"


def test_last_meaningful_line_trims_to_120_chars() -> None:
    long_line = "X" * 200
    result = cutover_check._last_meaningful_line(long_line)
    assert result is not None
    assert len(result) == 120


def test_last_meaningful_line_returns_none_on_empty() -> None:
    assert cutover_check._last_meaningful_line("") is None
    assert cutover_check._last_meaningful_line("\n\n   \n") is None


# ---------------------------------------------------------------------------
# Skip-list parsing
# ---------------------------------------------------------------------------


def test_parse_skip_list_handles_none_and_empty() -> None:
    assert cutover_check._parse_skip_list(None) == set()
    assert cutover_check._parse_skip_list("") == set()


def test_parse_skip_list_strips_whitespace_and_dedupes() -> None:
    assert cutover_check._parse_skip_list(" tampering, track_a , tampering ") == {
        "tampering",
        "track_a",
    }


# ---------------------------------------------------------------------------
# Orchestrator integration via fake subprocess.run
# ---------------------------------------------------------------------------


@dataclass
class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess.

    Only the attributes cutover_check actually reads (returncode,
    stdout, stderr). Using a plain dataclass keeps the test free of
    typing.cast acrobatics.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""


@pytest.fixture
def patch_runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, _FakeCompleted]]:
    """Patch subprocess.run inside cutover_check with a dict of canned
    responses keyed by check name (tampering / track_a / etc.).

    Test bodies populate the dict before driving ``run``. A missing key
    in the dict defaults to a clean exit-0 result.
    """
    responses: dict[str, _FakeCompleted] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        # Find which check this is by scanning the cmd for the script
        # filename (apply_migrations.py / tampering_shadow_review.py / etc.).
        joined = " ".join(cmd)
        if "tampering_shadow_review.py" in joined:
            name = "tampering"
        elif "track_a_historical_lookback.py" in joined:
            name = "track_a"
        elif "apply_migrations.py" in joined:
            name = "pending_migrations"
        elif "db_verify.py" in joined:
            name = "prod_health"
        else:
            raise AssertionError(f"unexpected cmd in cutover_check test: {joined}")
        return responses.get(name, _FakeCompleted(returncode=0))

    monkeypatch.setattr("scripts.cutover_check.subprocess.run", fake_run)
    yield responses


def test_run_all_pass_returns_0(
    patch_runs: dict[str, _FakeCompleted],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cutover_check.run(target="prod", skip=set())
    assert rc == 0
    out = capsys.readouterr().out
    assert "OVERALL: PASS" in out
    assert "FAIL" not in out


def test_run_tampering_exit_3_is_pass(
    patch_runs: dict[str, _FakeCompleted],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """exit 3 from tampering = fires present, operator review needed,
    but the diagnostic itself ran fine. cutover_check treats that as
    PASS so the operator can still ship if they've reviewed."""
    patch_runs["tampering"] = _FakeCompleted(returncode=3, stdout="fires=12\n")
    rc = cutover_check.run(target="prod", skip=set())
    assert rc == 0
    out = capsys.readouterr().out
    assert "OVERALL: PASS" in out
    assert "fires=12" in out


def test_run_track_a_exit_3_is_fail(
    patch_runs: dict[str, _FakeCompleted],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Track A exit 3 = at least one regression. That's a hard cutover
    blocker; orchestrator must FAIL."""
    patch_runs["track_a"] = _FakeCompleted(
        returncode=3,
        stdout="misses=2\n",
    )
    rc = cutover_check.run(target="prod", skip=set())
    assert rc == 1
    out = capsys.readouterr().out
    assert "OVERALL: FAIL" in out
    assert "track_a" in out
    assert "misses=2" in out


def test_run_pending_migrations_nonzero_is_fail(
    patch_runs: dict[str, _FakeCompleted],
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_runs["pending_migrations"] = _FakeCompleted(
        returncode=3,
        stderr="DRIFT: 014_foo.sql sha256 mismatch\n",
    )
    rc = cutover_check.run(target="prod", skip=set())
    assert rc == 1
    out = capsys.readouterr().out
    assert "OVERALL: FAIL" in out
    assert "pending_migrations" in out
    assert "sha256 mismatch" in out


def test_run_prod_health_nonzero_is_fail(
    patch_runs: dict[str, _FakeCompleted],
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_runs["prod_health"] = _FakeCompleted(
        returncode=1,
        stdout="OVERALL: FAIL\n",
    )
    rc = cutover_check.run(target="prod", skip=set())
    assert rc == 1
    out = capsys.readouterr().out
    assert "OVERALL: FAIL" in out
    assert "prod_health" in out


def test_run_multiple_failures_listed_individually(
    patch_runs: dict[str, _FakeCompleted],
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_runs["track_a"] = _FakeCompleted(returncode=3, stdout="misses=5\n")
    patch_runs["prod_health"] = _FakeCompleted(returncode=1, stdout="db unreachable\n")
    rc = cutover_check.run(target="prod", skip=set())
    assert rc == 1
    out = capsys.readouterr().out
    assert "2 of 4 checks failed" in out
    # Both failures are surfaced individually so the operator sees both
    # in the summary without scrolling.
    assert "track_a" in out
    assert "prod_health" in out


def test_run_skip_removes_check_from_verdict(
    patch_runs: dict[str, _FakeCompleted],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A skipped check renders as SKIP and does NOT count toward FAIL
    or PASS volume. The overall outcome is PASS so long as nothing
    actively failed."""
    # If prod_health WOULD have failed, but operator skips it, we still
    # PASS. Operator's responsibility to know what they skipped.
    patch_runs["prod_health"] = _FakeCompleted(returncode=1, stdout="db unreachable\n")
    rc = cutover_check.run(target="prod", skip={"prod_health"})
    assert rc == 0
    out = capsys.readouterr().out
    assert "OVERALL: PASS" in out
    assert "1 skipped" in out
    assert "SKIP" in out


def test_run_unknown_skip_name_returns_usage_error(
    patch_runs: dict[str, _FakeCompleted],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cutover_check.run(target="prod", skip={"made_up_check"})
    assert rc == cutover_check.EXIT_USAGE
    err = capsys.readouterr().err
    assert "unknown --skip name" in err
    assert "made_up_check" in err


def test_run_subprocess_filenotfound_renders_as_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If the underlying script is missing (broken install), the
    orchestrator must not crash with an uncaught FileNotFoundError —
    it must surface FAIL with a clear detail."""

    def boom(cmd: list[str], **_: Any) -> _FakeCompleted:
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr("scripts.cutover_check.subprocess.run", boom)

    rc = cutover_check.run(target="prod", skip=set())
    assert rc == 1
    out = capsys.readouterr().out
    assert "OVERALL: FAIL" in out
    assert "command not found" in out


# ---------------------------------------------------------------------------
# main() argparse glue
# ---------------------------------------------------------------------------


def test_main_requires_target() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cutover_check.main([])
    assert exc_info.value.code == 2


def test_main_rejects_unknown_target() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cutover_check.main(["--target", "qa"])
    assert exc_info.value.code == 2


def test_main_drives_run_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(*, target: str, skip: set[str]) -> int:
        captured["target"] = target
        captured["skip"] = skip
        return 0

    monkeypatch.setattr(cutover_check, "run", fake_run)
    assert cutover_check.main(["--target", "staging", "--skip", "tampering,track_a"]) == 0
    assert captured == {"target": "staging", "skip": {"tampering", "track_a"}}


# ---------------------------------------------------------------------------
# Spec wiring
# ---------------------------------------------------------------------------


def test_build_specs_includes_all_four_checks() -> None:
    specs = cutover_check._build_specs("prod")
    names = [s.name for s in specs]
    assert names == ["tampering", "track_a", "pending_migrations", "prod_health"]


def test_build_specs_threads_target_through_to_migration_and_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    specs = cutover_check._build_specs("staging")
    mig = next(s for s in specs if s.name == "pending_migrations")
    health = next(s for s in specs if s.name == "prod_health")
    assert "--target" in mig.cmd
    assert "staging" in mig.cmd
    assert "--target" in health.cmd
    assert "staging" in health.cmd
    # The two diagnostic scripts that consume DATABASE_URL directly do
    # NOT take a --target flag; ensure we didn't add a spurious one.
    tampering = next(s for s in specs if s.name == "tampering")
    track_a = next(s for s in specs if s.name == "track_a")
    assert "--target" not in tampering.cmd
    assert "--target" not in track_a.cmd
    # Silence the unused fixture warning while preserving the
    # signature shape used by sibling tests in the file.
    _ = monkeypatch


def test_build_specs_tampering_accepts_exit_3() -> None:
    specs = cutover_check._build_specs("prod")
    tampering = next(s for s in specs if s.name == "tampering")
    assert 3 in tampering.pass_codes


def test_build_specs_track_a_rejects_exit_3() -> None:
    specs = cutover_check._build_specs("prod")
    track_a = next(s for s in specs if s.name == "track_a")
    assert 3 not in track_a.pass_codes


def test_build_specs_track_a_passes_skip_orphans_flag() -> None:
    """The cutover gate must call the lookback with --skip-orphans so
    a single orphan document (merchant_id IS NULL) can't FAIL the
    whole gate. Removing this flag would re-introduce the false
    positive that motivated commit f3edc4a-ish (see 2026-06-15)."""
    specs = cutover_check._build_specs("prod")
    track_a = next(s for s in specs if s.name == "track_a")
    assert "--skip-orphans" in track_a.cmd


# ---------------------------------------------------------------------------
# Smoke: subprocess.run uses argv list (no shell), prevents injection
# ---------------------------------------------------------------------------


def test_subprocess_run_uses_argv_list_not_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense-in-depth: the orchestrator must never call subprocess.run
    with shell=True. Argument injection from a hostile env var that
    leaked into a script path would otherwise become RCE."""
    seen_kwargs: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeCompleted:
        seen_kwargs.append(kwargs)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr("scripts.cutover_check.subprocess.run", fake_run)
    cutover_check.run(target="prod", skip=set())
    assert seen_kwargs, "subprocess.run should have been called at least once"
    for kw in seen_kwargs:
        assert kw.get("shell") is not True
        assert kw.get("capture_output") is True
