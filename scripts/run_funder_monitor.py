"""Windows-side wrapper for the weekly funder folder monitor.

Configured as a Task Scheduler job that fires every Monday 09:00 local
time (see ``deploy/windows/funder_monitor_task.xml``). Sets
``AEGIS_FUNDER_MONITOR_PATH`` to the OneDrive-synced guidelines folder,
loads the local ``.env`` so Bedrock + Supabase creds resolve, then
invokes the same ``run_funder_monitor_pass`` the arq cron used to call
on linux — which was removed from ``WorkerSettings`` because the prod
box has no OneDrive sync.

Logs land at ``C:\\Users\\fkozi\\aegis\\logs\\funder_monitor.log``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Env setup MUST run before any aegis.* import — aegis.config reads env on
# first Settings instantiation and caches the result.
# ---------------------------------------------------------------------------

_FUNDER_FOLDER = r"C:\Users\fkozi\OneDrive\Radna površina\COMMERA FUNDING\Funders"
os.environ.setdefault("AEGIS_FUNDER_MONITOR_PATH", _FUNDER_FOLDER)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOTENV = _REPO_ROOT / ".env"
if _DOTENV.is_file():
    with open(_DOTENV, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            # ``setdefault`` so an externally-injected value (e.g. a
            # Task Scheduler env override) still wins.
            os.environ.setdefault(key.strip(), value.strip())

# ---------------------------------------------------------------------------
# Logging — log file lands inside the repo so the operator can tail it
# without hunting for an OS-specific journal location.
# ---------------------------------------------------------------------------

_LOG_DIR = _REPO_ROOT / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_PATH = _LOG_DIR / "funder_monitor.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
_log = logging.getLogger("run_funder_monitor")


def main() -> int:
    """Run one monitor pass synchronously. Returns 0 on success.

    Errors are caught and logged so the scheduled task never reports
    failure for transient network / Bedrock issues — the audit row from
    inside the pass is the durable signal.
    """
    # Lazy imports so the .env load above is in effect by the time
    # aegis.config.Settings is instantiated.
    from aegis.api.deps import get_audit, get_funder_repository, get_llm
    from aegis.funders.monitor import run_funder_monitor_pass

    folder = os.environ.get("AEGIS_FUNDER_MONITOR_PATH")
    _log.info("starting funder monitor pass folder=%s", folder)

    try:
        audit = get_audit()
        funders = get_funder_repository()
        llm = get_llm()
    except Exception:
        _log.exception("dependency wiring failed; aborting")
        return 1

    try:
        summary = run_funder_monitor_pass(
            folder_path=folder,
            funders_repo=funders,
            llm=llm,
            audit=audit,
        )
    except Exception:
        _log.exception("funder monitor pass raised; check audit log")
        return 1

    _log.info(
        "funder monitor done files_seen=%s unchanged=%s updated=%s failed=%s",
        summary["files_seen"],
        summary["unchanged"],
        summary["updated"],
        summary["failed"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
