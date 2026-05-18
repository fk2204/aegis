"""Phase 10 prep stubs — verify reserved routes / tasks 501 (mp Phase 10).

These stubs are placeholders; the bodies land in 2D-main. The tests
here exist to:

1. Lock the reserved URL / task name so 2D-main has a target.
2. Make sure a probing operator sees a clear 501, not a 500 or a
   silent success that looks like Phase 10 shipped.
3. Catch the day someone deletes the stubs by accident before 2D-main
   has filled them in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.api.deps import reset_dependency_caches
from aegis.workers import process_funder_reply


@pytest.fixture
def client() -> Iterator[TestClient]:
    reset_dependency_caches()
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_dependency_caches()


def test_decision_override_stub_returns_501(client: TestClient) -> None:
    """The /ui surface is fronted by Cloudflare Access in production, not
    require_bearer — same as the rest of web/router.py. So in TestClient
    the route is reachable; what matters is that the stub clearly
    advertises "not yet wired" via 501 rather than a misleading 500 or
    success."""
    resp = client.post(f"/ui/decisions/{uuid4()}/override")
    assert resp.status_code == 501
    assert resp.json()["detail"] == "override_capture_not_yet_wired"


def test_process_funder_reply_task_raises_not_implemented() -> None:
    """The task is registered with arq so the worker config is shaped
    correctly. Calling it directly must raise — silent success would be
    a regulator-defense gap once the capture surface goes live."""
    with pytest.raises(NotImplementedError):
        asyncio.run(process_funder_reply({}, "{}"))


def test_process_funder_reply_is_registered_with_arq() -> None:
    """Lock the WorkerSettings.functions tuple shape so 2D-main has a
    stable target. If this fails after a refactor, the WorkerSettings
    config changed shape — fix the test, don't drop the assertion."""
    from aegis.workers import WorkerSettings, parse_document

    assert parse_document in WorkerSettings.functions
    assert process_funder_reply in WorkerSettings.functions
