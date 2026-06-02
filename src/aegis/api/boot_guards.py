"""Boot-time assertions that gate the FastAPI lifespan.

Chunk A of the PDF retention redesign — see
``docs/PDF_RETENTION_DESIGN.md`` §3. The CF tunnel + loopback bind
together form the only external network control gating the new view
route (and every other CF-authenticated route). A future
``systemd``-unit edit that silently flips ``--host`` to ``0.0.0.0``
must NOT pass startup unchallenged. ``assert_uvicorn_loopback_bind``
is the regression guard.

Other boot guards (crypto config, bucket privacy) live near their
concern and are wired into the same lifespan call site in
``aegis.api.app``.
"""
from __future__ import annotations

import sys
from typing import Final

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({
    "127.0.0.1",
    "::1",
    "localhost",
})


class UvicornBindError(RuntimeError):
    """uvicorn was launched without ``--host``, or with a non-loopback
    host. Refuses to boot — see design doc §3.
    """


def assert_uvicorn_loopback_bind() -> None:
    """Refuse to boot if uvicorn was launched with a non-loopback host.

    No-op when not running under uvicorn (e.g. pytest, scripts,
    interactive REPL) so the test suite + offline runs aren't blocked.
    Detection is by ``sys.argv[0]`` basename — uvicorn's entry script
    is named ``uvicorn`` and runs the FastAPI app in the same Python
    process.

    Failure modes:
      * ``--host`` not present → refuse. Uvicorn 0.32+ defaults to
        ``127.0.0.1``, but earlier versions defaulted to ``0.0.0.0``.
        Explicit is safer than implicit; the systemd unit MUST pass
        ``--host 127.0.0.1``.
      * ``--host`` present with non-loopback value (anything not in
        ``_LOOPBACK_HOSTS``) → refuse.
    """
    argv = list(sys.argv)
    if not _is_uvicorn_invocation(argv):
        return

    host = _extract_host_arg(argv)
    if host is None:
        raise UvicornBindError(
            "uvicorn launched without explicit --host; refusing to boot. "
            "The PDF retention design (docs/PDF_RETENTION_DESIGN.md §3) "
            "requires loopback-only binding behind cloudflared tunnel. "
            "Pass --host 127.0.0.1 in the systemd unit "
            "(deploy/aegis-web.service)."
        )

    if host not in _LOOPBACK_HOSTS:
        raise UvicornBindError(
            f"uvicorn bound to non-loopback host {host!r}; refusing to boot. "
            f"Allowed: {sorted(_LOOPBACK_HOSTS)}. "
            "The PDF retention design (docs/PDF_RETENTION_DESIGN.md §3) "
            "requires loopback-only binding behind cloudflared tunnel — "
            "the load-bearing origin control gating every CF-authenticated "
            "route, including the new /api/documents/{id}/original route."
        )


def _is_uvicorn_invocation(argv: list[str]) -> bool:
    """True when ``sys.argv[0]`` looks like the uvicorn entry script.

    Strips both POSIX (``/``) and Windows (``\\``) path separators so
    a full path like ``/opt/aegis/.venv/bin/uvicorn`` is detected.
    """
    if not argv:
        return False
    prog = argv[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    # Trim the trailing ".exe" or similar that Windows / pip might add
    if prog.endswith(".exe"):
        prog = prog[:-4]
    return prog == "uvicorn"


def _extract_host_arg(argv: list[str]) -> str | None:
    """Pull the ``--host`` value out of argv. Supports both ``--host X``
    and ``--host=X`` forms. Returns ``None`` if no ``--host`` is
    present at all.
    """
    for i, arg in enumerate(argv):
        if arg == "--host" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--host="):
            return arg.split("=", 1)[1]
    return None


__all__ = [
    "UvicornBindError",
    "assert_uvicorn_loopback_bind",
]
