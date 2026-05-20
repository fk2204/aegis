"""Operational reliability primitives (mp Phase 11).

Modules in this package own production observability — alerting,
cost accounting, and rate-limit verification. They share three
properties:

  * **Fail open in the alerting direction.** Failure to notify never
    blocks application progress; the application logs the alert
    locally and continues. The opposite (silently dropping alerts) is
    the operational antipattern this package exists to prevent.
  * **Audit-log every meaningful event.** Every alert dispatched is
    accompanied by an ``audit_log`` row (action prefix ``ops.alert.``)
    so the operator can reconstruct the alert stream from the durable
    record even when the external alerting provider was down.
  * **Configurable via env, default to no-op.** Tests and offline
    development run with alerting disabled; production wires real
    Healthchecks.io UUIDs and ntfy topics via ``/etc/aegis/aegis.env``.
"""

from __future__ import annotations
