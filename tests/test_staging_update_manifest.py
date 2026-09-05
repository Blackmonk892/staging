"""``GET /v1/agents/{id}/update-manifest`` + ``POST /admin/manifest`` -- absent ⇒ 404."""

from __future__ import annotations

import pytest
from conftest import Rig, enroll, mint_code

pytestmark = pytest.mark.asyncio

_MANIFEST = {
    "version": "1.4.0",
    "download_url": "https://updates.example.com/nzerox-1.4.0.bin",
    "sha256": "0" * 64,
    "ed25519_signature": "not-a-real-signature",
    "minimum_version_required": "1.0.0",
    "released_at": "2026-01-01T00:00:00+00:00",
    "is_mandatory": False,
}


async def _enrolled(stg: Rig) -> tuple[str, str]:
    body = await enroll(stg, await mint_code(stg))
    return str(body["agent_id"]), str(body["credential"])


async def test_manifest_absent_is_404(stg: Rig) -> None:
    agent_id, cred = await _enrolled(stg)
    resp = await stg.client.get(
        f"/v1/agents/{agent_id}/update-manifest",
        headers={"Authorization": f"Bearer {cred}"},
    )
    assert resp.status == 404


async def test_manifest_publish_then_fetch(stg: Rig) -> None:
    agent_id, cred = await _enrolled(stg)
    pub = await stg.client.post("/admin/manifest", headers=stg.admin_headers, json=_MANIFEST)
    assert pub.status == 200, await pub.text()

    got = await stg.client.get(
        f"/v1/agents/{agent_id}/update-manifest",
        headers={"Authorization": f"Bearer {cred}"},
    )
    assert got.status == 200
    body = await got.json()
    assert body["version"] == "1.4.0"
    assert body["sha256"] == "0" * 64


async def test_manifest_publish_rejects_bad_semver(stg: Rig) -> None:
    bad = {**_MANIFEST, "version": "not-a-version"}
    resp = await stg.client.post("/admin/manifest", headers=stg.admin_headers, json=bad)
    assert resp.status == 400


async def test_manifest_fetch_requires_auth(stg: Rig) -> None:
    resp = await stg.client.get("/v1/agents/whoever/update-manifest")
    assert resp.status == 401
