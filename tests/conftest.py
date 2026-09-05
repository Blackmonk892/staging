"""Fixtures for the staging-cloud test suite.

Mirrors ``tests/agent/conftest.py::fake_cloud``: a :class:`MemoryRepo` plus
``build_app`` on an aiohttp test server. Every test drives the real HTTP handlers
over a loopback socket -- no Mongo, no network, fully isolated from
``tests/agent/**``.

``stg`` yields a small :class:`Rig` bundling the test client, the repo (so a test
can seed state or assert on it directly) and the settings (for the admin token).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest_asyncio
from aiohttp import test_utils
from aiohttp.web import Application, Request

from staging_cloud.api import build_app
from staging_cloud.memory_repo import MemoryRepo
from staging_cloud.settings import StagingSettings

ADMIN_TOKEN = "test-admin-token"

Client = test_utils.TestClient[Request, Application]


@dataclass
class Rig:
    """A running staging cloud over an in-process repo, plus its collaborators."""

    client: Client
    repo: MemoryRepo
    settings: StagingSettings

    @property
    def admin_headers(self) -> dict[str, str]:
        """Headers carrying a valid admin token for ``/admin/*`` and ``/``."""
        return {"X-Admin-Token": ADMIN_TOKEN}


def make_settings(**overrides: object) -> StagingSettings:
    """Build :class:`StagingSettings` with test defaults, bypassing the environment."""
    base: dict[str, object] = {
        "mongodb_uri": "memory://",
        "staging_admin_token": ADMIN_TOKEN,
        "credential_ttl_days": 30,
        "pairing_code_ttl_seconds": 3600,
    }
    base.update(overrides)
    return StagingSettings(**base)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def stg() -> AsyncIterator[Rig]:
    """Start ``build_app`` on a loopback socket over a fresh :class:`MemoryRepo`."""
    repo = MemoryRepo()
    settings = make_settings()
    server = test_utils.TestServer(build_app(repo, settings))
    client = test_utils.TestClient(server)
    await client.start_server()
    try:
        yield Rig(client=client, repo=repo, settings=settings)
    finally:
        await client.close()


async def mint_code(
    rig: Rig, *, site_id: str = "site-1", tenant_id: str = "tenant-1", **body: object
) -> str:
    """Mint one pairing code via the admin API and return it."""
    resp = await rig.client.post(
        "/admin/pairing-codes",
        headers=rig.admin_headers,
        json={"site_id": site_id, "tenant_id": tenant_id, **body},
    )
    assert resp.status == 200, await resp.text()
    payload = await resp.json()
    return str(payload["codes"][0])


def fingerprint_payload() -> dict[str, object]:
    """A contract-valid :class:`HardwareFingerprint` body for enrollment requests."""
    return {
        "machine_id": "machine-abc",
        "primary_mac": "00:11:22:33:44:55",
        "os_name": "Linux",
        "os_version": "6.1.0",
        "cpu_count": 8,
        "total_memory_gb": 16.0,
        "declared_max_cameras": 10,
    }


async def enroll(rig: Rig, code: str) -> dict[str, object]:
    """Run a full enrollment against ``rig`` and return the parsed response body."""
    resp = await rig.client.post(
        "/v1/agents/enroll",
        json={"pairing_code": code, "fingerprint": fingerprint_payload()},
    )
    assert resp.status == 200, await resp.text()
    return dict(await resp.json())
