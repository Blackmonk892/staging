"""``GET /v1/agents/{id}/config`` + ``POST /admin/agents/{id}/config`` -- monotonic version."""

from __future__ import annotations

import pytest
from conftest import Rig, enroll, mint_code

pytestmark = pytest.mark.asyncio


async def _enrolled(stg: Rig) -> tuple[str, str]:
    body = await enroll(stg, await mint_code(stg))
    return str(body["agent_id"]), str(body["credential"])


def _camera(cam_id: str) -> dict[str, object]:
    return {"camera_id": cam_id, "rtsp_url": f"rtsp://cam/{cam_id}"}


async def test_config_defaults_to_version_zero(stg: Rig) -> None:
    agent_id, cred = await _enrolled(stg)
    resp = await stg.client.get(
        f"/v1/agents/{agent_id}/config", headers={"Authorization": f"Bearer {cred}"}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["config_version"] == 0
    assert body["cameras"] == []
    assert "server_time" in body


async def test_admin_set_config_bumps_version_monotonically(stg: Rig) -> None:
    agent_id, cred = await _enrolled(stg)
    for expected in (1, 2, 3):
        put = await stg.client.post(
            f"/admin/agents/{agent_id}/config",
            headers=stg.admin_headers,
            json={"cameras": [_camera(f"c{expected}")]},
        )
        assert put.status == 200, await put.text()
        assert (await put.json())["config_version"] == expected

    got = await stg.client.get(
        f"/v1/agents/{agent_id}/config", headers={"Authorization": f"Bearer {cred}"}
    )
    body = await got.json()
    assert body["config_version"] == 3
    assert body["cameras"][0]["camera_id"] == "c3"


async def test_admin_set_config_rejects_invalid_camera(stg: Rig) -> None:
    agent_id, _ = await _enrolled(stg)
    resp = await stg.client.post(
        f"/admin/agents/{agent_id}/config",
        headers=stg.admin_headers,
        json={"cameras": [{"camera_id": "c1"}]},  # missing rtsp_url
    )
    assert resp.status == 400


async def test_admin_set_config_requires_admin_token(stg: Rig) -> None:
    resp = await stg.client.post("/admin/agents/x/config", json={"cameras": []})
    assert resp.status == 401
