"""Optional MongoRepo coverage -- runs only when ``mongomock-motor`` is installed.

No real mongod: ``mongomock_motor`` provides an in-memory async Mongo. This
exercises ``ensure_indexes()``, an enroll→rotate→heartbeat round-trip through
the repo methods, and the unique-index rejection on ``credential_sha256``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

mongomock_motor = pytest.importorskip("mongomock_motor")

from staging_cloud import domain  # noqa: E402
from staging_cloud.domain import Agent, Credential  # noqa: E402


@pytest.fixture
def mongo_repo(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A :class:`MongoRepo` whose client is the in-memory ``AsyncMongoMockClient``."""
    from staging_cloud import repo as repo_mod

    monkeypatch.setattr(repo_mod, "AsyncIOMotorClient", mongomock_motor.AsyncMongoMockClient)
    return repo_mod.MongoRepo("mongodb://memory", "nzerox_staging_test")


async def test_ensure_indexes_is_idempotent(mongo_repo) -> None:  # type: ignore[no-untyped-def]
    await mongo_repo.ensure_indexes()
    await mongo_repo.ensure_indexes()
    assert await mongo_repo.ping() is True


async def test_enroll_rotate_heartbeat_round_trip(mongo_repo) -> None:  # type: ignore[no-untyped-def]
    await mongo_repo.ensure_indexes()
    now = domain.utcnow()
    agent = Agent(
        id="agent-1",
        tenant_id="t",
        site_id="s",
        machine_id="m",
        enrolled_at=now,
    )
    await mongo_repo.insert_agent(agent)
    tok = domain.mint_token()
    await mongo_repo.insert_credential(
        Credential(
            agent_id="agent-1",
            credential_sha256=domain.hash_token(tok),
            issued_at=now,
            expires_at=domain.expiry_after(3600, now=now),
            active=True,
        )
    )
    resolved = await mongo_repo.resolve_credential(domain.hash_token(tok))
    assert resolved is not None and resolved.active

    await mongo_repo.deactivate_agent_credentials("agent-1", reason="rotated")
    tok2 = domain.mint_token()
    await mongo_repo.insert_credential(
        Credential(
            agent_id="agent-1",
            credential_sha256=domain.hash_token(tok2),
            issued_at=now,
            expires_at=domain.expiry_after(3600, now=now),
            active=True,
        )
    )
    assert (
        await mongo_repo.get_active_credential("agent-1")
    ).credential_sha256 == domain.hash_token(tok2)

    await mongo_repo.append_heartbeat("agent-1", {"agent_version": "1.0.0"}, received_at=now)
    await mongo_repo.upsert_agent_status("agent-1", {"agent_version": "1.0.0"})
    assert (await mongo_repo.get_agent_status("agent-1"))["agent_version"] == "1.0.0"


async def test_unique_credential_sha256_index_rejects_duplicate(mongo_repo) -> None:  # type: ignore[no-untyped-def]
    from pymongo.errors import DuplicateKeyError

    await mongo_repo.ensure_indexes()
    now = domain.utcnow()
    row = Credential(
        agent_id="agent-x",
        credential_sha256=domain.hash_token("dup"),
        issued_at=now,
        expires_at=domain.expiry_after(3600, now=now),
        active=True,
    )
    await mongo_repo.insert_credential(row)
    with pytest.raises(DuplicateKeyError):
        await mongo_repo.insert_credential(row)


async def test_claim_pairing_code_is_single_use(mongo_repo) -> None:  # type: ignore[no-untyped-def]
    from staging_cloud.domain import PairingCode

    await mongo_repo.ensure_indexes()
    now = domain.utcnow()
    await mongo_repo.insert_pairing_code(
        PairingCode(
            code="pc-TESTTEST00",
            tenant_id="t",
            site_id="s",
            expires_at=domain.expiry_after(3600, now=now),
            max_uses=1,
            use_count=0,
            used=False,
        )
    )
    first = await mongo_repo.claim_pairing_code("pc-TESTTEST00", now=now, agent_id="a1")
    assert first is not None
    second = await mongo_repo.claim_pairing_code("pc-TESTTEST00", now=now, agent_id="a2")
    assert second is None
