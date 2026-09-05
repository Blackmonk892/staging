"""``POST /v1/agents/{id}/rotate-credential`` -- supersede + reissue, expiry tolerated."""

from __future__ import annotations

import pytest
from conftest import Rig, enroll, mint_code

from staging_cloud import domain
from staging_cloud.contracts.enrollment import CredentialRotationResponse

pytestmark = pytest.mark.asyncio


async def _enrolled(stg: Rig) -> tuple[str, str]:
    body = await enroll(stg, await mint_code(stg))
    return str(body["agent_id"]), str(body["credential"])


async def test_rotate_supersedes_old_credential(stg: Rig) -> None:
    agent_id, old = await _enrolled(stg)
    resp = await stg.client.post(
        f"/v1/agents/{agent_id}/rotate-credential",
        headers={"Authorization": f"Bearer {old}"},
    )
    assert resp.status == 200
    body = await resp.json()
    CredentialRotationResponse.model_validate(body)
    new = str(body["credential"])
    assert new != old

    old_row = await stg.repo.resolve_credential(domain.hash_token(old))
    new_row = await stg.repo.resolve_credential(domain.hash_token(new))
    assert old_row is not None and not old_row.active
    assert new_row is not None and new_row.active


async def test_rotate_tolerates_expired_credential(stg: Rig) -> None:
    agent_id, cred = await _enrolled(stg)
    # Backdate the active credential row directly in the repo.
    await stg.repo.deactivate_agent_credentials(agent_id, reason="test")
    await stg.repo.insert_credential(
        domain.Credential(
            agent_id=agent_id,
            credential_sha256=domain.hash_token(cred),
            issued_at=domain.utcnow(),
            expires_at=domain.expiry_after(-5),
            active=True,
        )
    )
    resp = await stg.client.post(
        f"/v1/agents/{agent_id}/rotate-credential",
        headers={"Authorization": f"Bearer {cred}"},
    )
    assert resp.status == 200


async def test_rotate_rejects_wrong_token(stg: Rig) -> None:
    agent_id, _ = await _enrolled(stg)
    resp = await stg.client.post(
        f"/v1/agents/{agent_id}/rotate-credential",
        headers={"Authorization": "Bearer cred-bogus"},
    )
    assert resp.status == 401


async def test_rotate_rejects_revoked_agent(stg: Rig) -> None:
    agent_id, cred = await _enrolled(stg)
    await stg.repo.set_agent_revoked(agent_id, True)
    resp = await stg.client.post(
        f"/v1/agents/{agent_id}/rotate-credential",
        headers={"Authorization": f"Bearer {cred}"},
    )
    assert resp.status == 401
