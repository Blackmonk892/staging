"""``POST /v1/agents/enroll`` -- pairing-code claim, credential mint, single-use."""

from __future__ import annotations

import pytest
from conftest import Rig, enroll, fingerprint_payload, mint_code

from staging_cloud import domain
from staging_cloud.contracts.enrollment import EnrollmentResponse

pytestmark = pytest.mark.asyncio


async def test_enroll_happy_path(stg: Rig) -> None:
    code = await mint_code(stg, site_id="site-a", tenant_id="tenant-a")
    body = await enroll(stg, code)

    assert body["tenant_id"] == "tenant-a"
    assert body["site_id"] == "site-a"
    assert str(body["credential"]).startswith("cred-")

    # The non-secret subset validates against the real wire contract.
    EnrollmentResponse.model_validate(body)

    # The credential is persisted only as a sha256 hash.
    stored = await stg.repo.resolve_credential(domain.hash_token(str(body["credential"])))
    assert stored is not None and stored.active and stored.agent_id == body["agent_id"]


async def test_enroll_rejects_bad_pairing_code(stg: Rig) -> None:
    resp = await stg.client.post(
        "/v1/agents/enroll",
        json={"pairing_code": "pc-NOPENOPE00", "fingerprint": fingerprint_payload()},
    )
    assert resp.status == 403
    assert (await resp.json())["error"] == "invalid pairing code"


async def test_enroll_rejects_malformed_body(stg: Rig) -> None:
    resp = await stg.client.post("/v1/agents/enroll", json={"pairing_code": "x"})
    assert resp.status == 400


async def test_pairing_code_is_single_use(stg: Rig) -> None:
    code = await mint_code(stg)
    first = await stg.client.post(
        "/v1/agents/enroll",
        json={"pairing_code": code, "fingerprint": fingerprint_payload()},
    )
    assert first.status == 200
    second = await stg.client.post(
        "/v1/agents/enroll",
        json={"pairing_code": code, "fingerprint": fingerprint_payload()},
    )
    assert second.status == 403


async def test_pairing_code_max_uses_allows_n_enrolls(stg: Rig) -> None:
    code = await mint_code(stg, max_uses=2)
    for _ in range(2):
        ok = await stg.client.post(
            "/v1/agents/enroll",
            json={"pairing_code": code, "fingerprint": fingerprint_payload()},
        )
        assert ok.status == 200
    exhausted = await stg.client.post(
        "/v1/agents/enroll",
        json={"pairing_code": code, "fingerprint": fingerprint_payload()},
    )
    assert exhausted.status == 403
