"""Tests for ``aegis.api.boot_guards`` — uvicorn loopback bind guard.

Chunk A of the PDF retention redesign — see
``docs/PDF_RETENTION_DESIGN.md`` §3. The loopback bind on uvicorn is
now the load-bearing origin control for every CF-authenticated route
(the new ``/api/documents/{id}/original`` route in particular). A
future systemd-unit edit that silently flips ``--host`` to
``0.0.0.0`` would reopen the PDF-exfil hole; this boot guard is the
regression guard.

Tests patch ``sys.argv`` to simulate the various launch shapes.
"""
from __future__ import annotations

import sys

import pytest

from aegis.api.boot_guards import (
    UvicornBindError,
    assert_uvicorn_loopback_bind,
)


def _patch_argv(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    monkeypatch.setattr(sys, "argv", argv)


def test_passes_for_loopback_127001(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_argv(monkeypatch, [
        "/opt/aegis/.venv/bin/uvicorn",
        "aegis.api.app:app",
        "--host", "127.0.0.1",
        "--port", "5555",
    ])
    assert_uvicorn_loopback_bind()  # no raise


def test_passes_for_loopback_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_argv(monkeypatch, [
        "/opt/aegis/.venv/bin/uvicorn",
        "aegis.api.app:app",
        "--host", "::1",
    ])
    assert_uvicorn_loopback_bind()


def test_passes_for_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_argv(monkeypatch, [
        "/opt/aegis/.venv/bin/uvicorn",
        "aegis.api.app:app",
        "--host", "localhost",
    ])
    assert_uvicorn_loopback_bind()


def test_passes_for_host_equals_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """uvicorn also accepts ``--host=127.0.0.1`` (single-token form)."""
    _patch_argv(monkeypatch, [
        "/opt/aegis/.venv/bin/uvicorn",
        "aegis.api.app:app",
        "--host=127.0.0.1",
    ])
    assert_uvicorn_loopback_bind()


def test_refuses_0_0_0_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact regression we're guarding against — somebody changes
    --host to 0.0.0.0 in the systemd unit. The guard MUST refuse to
    boot."""
    _patch_argv(monkeypatch, [
        "/opt/aegis/.venv/bin/uvicorn",
        "aegis.api.app:app",
        "--host", "0.0.0.0",  # noqa: S104  # test asserts REFUSAL of this binding
    ])
    with pytest.raises(UvicornBindError) as exc:
        assert_uvicorn_loopback_bind()
    assert "0.0.0.0" in str(exc.value)  # noqa: S104  # asserting the rejection message names the offender


def test_refuses_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binding to the box's actual public IP would also bypass the CF
    tunnel — refuse."""
    _patch_argv(monkeypatch, [
        "/opt/aegis/.venv/bin/uvicorn",
        "aegis.api.app:app",
        "--host", "5.161.51.105",
    ])
    with pytest.raises(UvicornBindError):
        assert_uvicorn_loopback_bind()


def test_refuses_when_host_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``--host`` flag → refuse. Uvicorn 0.32+ defaults to
    127.0.0.1, but earlier versions defaulted to 0.0.0.0; explicit
    is the safe contract."""
    _patch_argv(monkeypatch, [
        "/opt/aegis/.venv/bin/uvicorn",
        "aegis.api.app:app",
        "--port", "5555",
    ])
    with pytest.raises(UvicornBindError) as exc:
        assert_uvicorn_loopback_bind()
    assert "without explicit --host" in str(exc.value)


def test_skips_when_not_under_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pytest, scripts, REPL → ``sys.argv[0]`` is not ``uvicorn``;
    the guard is a no-op. Otherwise the whole test suite would refuse
    to run."""
    _patch_argv(monkeypatch, ["pytest", "tests/"])
    assert_uvicorn_loopback_bind()


def test_skips_for_python_module_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``python -m foo`` style — sys.argv[0] is the module name, not
    uvicorn. No-op."""
    _patch_argv(monkeypatch, ["/usr/bin/python", "-m", "aegis.workers"])
    assert_uvicorn_loopback_bind()


def test_handles_windows_path_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive: a Windows-style path with backslashes should still
    detect the uvicorn basename."""
    _patch_argv(monkeypatch, [
        r"C:\Users\fkozi\aegis\.venv\Scripts\uvicorn.exe",
        "aegis.api.app:app",
        "--host", "127.0.0.1",
    ])
    assert_uvicorn_loopback_bind()


def test_handles_empty_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sys.argv`` should never be empty in practice, but the guard
    must not crash if it is."""
    _patch_argv(monkeypatch, [])
    assert_uvicorn_loopback_bind()
