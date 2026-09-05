"""``POST /v1/agents/{id}/heartbeat`` -- auth, status upsert, command delivery."""

from __future__ import annotations

import pytest
from conftest import Rig, enroll, mint_code

pytestmark = pytest.mark.asyncio


async def _enrolled(stg: Rig) -> tuple[str, str]:
    body = await enroll(stg, await mint_code(stg))
    return str(body["agent_id"]), str(body["credential"])


def _hb_body(agent_id: str) -> dict[str, object]:
    return {
        "agent_id": agent_id,
        "agent_version": "1.2.3",
        "uptime_seconds": 42.0,
        "config_version_applied": 0,
        "cameras": [],
        "system": {
            "cpu_percent": 3.0,
            "memory_percent": 40.0,
            "disk_percent": 12.0,
            "disk_free_gb": 100.0,
        },
    }


async def test_heartbeat_ok_and_records_status(stg: Rig) -> None:
    agent_id, cred = await _enrolled(stg)
    resp = await stg.client.post(
        f"/v1/agents/{agent_id}/heartbeat",
        headers={"Authorization": f"Bearer {cred}"},
        json=_hb_body(agent_id),
    )
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == {"server_time", "commands"}
    assert body["commands"] == []

    status = await stg.repo.get_agent_status(agent_id)
    assert status is not None
    assert status["agent_version"] == "1.2.3"
    assert status["camera_count"] == 0


async def test_heartbeat_delivers_queued_command_once(stg: Rig) -> None:
    agent_id, cred = await _enrolled(stg)
    await stg.repo.queue_command(agent_id, kind="force_sync", payload={})

    first = await stg.client.post(
        f"/v1/agents/{agent_id}/heartbeat",
        headers={"Authorization": f"Bearer {cred}"},
        json=_hb_body(agent_id),
    )
    assert [c["kind"] for c in (await first.json())["commands"]] == ["force_sync"]

    second = await stg.client.post(
        f"/v1/agents/{agent_id}/heartbeat",
        headers={"Authorization": f"Bearer {cred}"},
        json=_hb_body(agent_id),
    )
    assert (await second.json())["commands"] == []


async def test_heartbeat_rejects_missing_bearer(stg: Rig) -> None:
    agent_id, _ = await _enrolled(stg)
    resp = await stg.client.post(f"/v1/agents/{agent_id}/heartbeat", json=_hb_body(agent_id))
    assert resp.status == 401


async def test_heartbeat_rejects_revoked_agent(stg: Rig) -> None:
    agent_id, cred = await _enrolled(stg)
    await stg.repo.set_agent_revoked(agent_id, True)
    resp = await stg.client.post(
        f"/v1/agents/{agent_id}/heartbeat",
        headers={"Authorization": f"Bearer {cred}"},
        json=_hb_body(agent_id),
    )
    assert resp.status == 401
