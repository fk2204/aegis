"""Regression guards for ``WorkerSettings`` runtime configuration.

``WorkerSettings.max_jobs`` is populated at module import from
``Settings.aegis_worker_max_concurrent`` (see
``aegis.workers._populate_worker_attributes``). Pinning the env-driven
default to 3 keeps arq parallelism under the 4GB Hetzner box's RAM
ceiling — Bedrock vision parses cost ~600-900MB resident each at
~150-180s, and the worker shares the box with aegis-web + Redis. arq's
own default of 10 would overwhelm the box on a 3+ concurrent vision
burst.

If a future box upgrade lifts the constraint, raise the env override
(``AEGIS_WORKER_MAX_CONCURRENT=N``) rather than changing the default —
the default is the safe-for-current-hardware baseline.
"""

from __future__ import annotations

from aegis.config import Settings


def test_default_worker_max_concurrent_is_three() -> None:
    """The env-driven default backing ``WorkerSettings.max_jobs`` is 3.

    Changing this default is operationally significant — it bounds
    parallelism on the prod box. The Field constraint
    (``ge=1, le=32``) is a safety net, but the default is the
    load-bearing number.
    """
    settings = Settings()
    assert settings.aegis_worker_max_concurrent == 3


def test_worker_max_concurrent_field_bounds_unchanged() -> None:
    """The Field constraint ``ge=1, le=32`` stays in place.

    1 is the safe-minimum lower bound (the worker must be able to
    process at least one job at a time). 32 is a high ceiling that
    still leaves arq's own contention model coherent.
    """
    info = Settings.model_fields["aegis_worker_max_concurrent"]
    metadata = list(info.metadata)
    # Pydantic encodes Field's ge/le as annotation metadata; the exact
    # type comes from annotated_types. Match by attribute name to stay
    # robust against import-shape drift.
    ges = [m for m in metadata if hasattr(m, "ge")]
    les = [m for m in metadata if hasattr(m, "le")]
    assert ges and ges[0].ge == 1
    assert les and les[0].le == 32
