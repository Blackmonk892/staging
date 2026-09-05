"""``GET /healthz`` -- the Cloud Run health check."""

from __future__ import annotations

import pytest
from conftest import Rig

pytestmark = pytest.mark.asyncio


async def test_healthz_ok_on_memory_backend(stg: Rig) -> None:
    resp = await stg.client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["env"] == "staging"


async def test_healthz_needs_no_auth(stg: Rig) -> None:
    # No admin token, no bearer -- still reachable.
    resp = await stg.client.get("/healthz")
    assert resp.status == 200


async def test_healthz_reports_503_when_backend_ping_fails(stg: Rig) -> None:
    async def _boom() -> bool:
        raise RuntimeError("backend down")

    stg.repo.ping = _boom  # type: ignore[method-assign]
    resp = await stg.client.get("/healthz")
    assert resp.status == 503
    assert (await resp.json())["db"] == "down"
