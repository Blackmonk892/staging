"""MongoDB-backed :class:`~staging_cloud.repo_base.Repo` via the async ``motor`` driver.

This is the production backend. It maps the frozen value objects from
:mod:`staging_cloud.domain` onto nine collections (see ``DEPLOY.md`` / the plan's
persistence table) and keeps every rule in ``domain`` -- this file is pure
storage plumbing.

Design choices worth stating:

* **Tokens are stored only as sha256 hex.** ``credentials.credential_sha256`` has
  a unique index; the plaintext bearer never touches the database. A dump cannot
  be replayed against the API.
* **The pairing-code claim is a single ``find_one_and_update``.** Two agents
  racing on the last use of a code cannot both win -- Mongo serialises the
  atomic increment, and the row flips to ``used`` when ``use_count`` hits
  ``max_uses``.
* **``config_version`` uses ``$inc`` with ``upsert=True``.** ``$inc`` on a
  missing field starts at 1, matching ``DevCloud.set_config``; the agent read
  path is a plain ``find_one`` that treats a missing document as version 0.
* **TTL indexes** expire heartbeat history, delivered commands and superseded
  credentials so the free-tier cluster does not fill up.

The client is created with ``tz_aware=True`` so datetimes round-trip as
timezone-aware UTC, matching what ``domain`` expects everywhere.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from staging_cloud.domain import (
    Agent,
    Credential,
    FleetConfig,
    PairingCode,
    utcnow,
)

# Superseded credentials and delivered commands are swept this long after they
# stop being useful; heartbeat rows use the configurable history TTL.
_CLEANUP_GRACE = timedelta(days=1)


class MongoRepo:
    """Async MongoDB implementation of every :class:`Repo` method."""

    def __init__(self, uri: str, db_name: str, *, heartbeat_history_days: int = 7) -> None:
        """Open a connection pool to ``uri`` and bind to database ``db_name``.

        No network call happens here -- ``motor`` connects lazily on first use.
        ``heartbeat_history_days`` sets the TTL on the ``heartbeats`` collection.
        """
        self._client: AsyncIOMotorClient = AsyncIOMotorClient(uri, tz_aware=True)
        self._db = self._client[db_name]
        self._heartbeat_history_days = heartbeat_history_days

    # -- Lifecycle / health -------------------------------------------------

    async def ping(self) -> bool:
        """Round-trip the cheap ``ping`` admin command; any failure ⇒ False."""
        await self._client.admin.command("ping")
        return True

    async def ensure_indexes(self) -> None:
        """Create every collection index. Idempotent -- safe to call on each boot."""
        await self._db.agents.create_index([("tenant_id", ASCENDING), ("site_id", ASCENDING)])
        await self._db.agents.create_index("machine_id")

        await self._db.credentials.create_index("credential_sha256", unique=True)
        await self._db.credentials.create_index([("agent_id", ASCENDING), ("active", ASCENDING)])
        await self._db.credentials.create_index("cleanup_at", expireAfterSeconds=0)

        await self._db.pairing_codes.create_index("expires_at", expireAfterSeconds=0)
        await self._db.pairing_codes.create_index([("site_id", ASCENDING), ("used", ASCENDING)])

        await self._db.commands.create_index(
            [("agent_id", ASCENDING), ("delivered", ASCENDING), ("queued_at", ASCENDING)]
        )
        await self._db.commands.create_index("cleanup_at", expireAfterSeconds=0)

        await self._db.heartbeats.create_index(
            [("agent_id", ASCENDING), ("received_at", DESCENDING)]
        )
        await self._db.heartbeats.create_index(
            "received_at", expireAfterSeconds=self._heartbeat_history_days * 86_400
        )

        await self._db.audit_events.create_index([("at", DESCENDING)])
        await self._db.audit_events.create_index([("agent_id", ASCENDING), ("at", DESCENDING)])

    async def close(self) -> None:
        """Close the connection pool. Safe to call more than once."""
        self._client.close()

    # -- agents -----------------------------------------------------------

    async def insert_agent(self, agent: Agent) -> None:
        await self._db.agents.insert_one(
            {
                "_id": agent.id,
                "tenant_id": agent.tenant_id,
                "site_id": agent.site_id,
                "machine_id": agent.machine_id,
                "enrolled_at": agent.enrolled_at,
                "fingerprint": agent.fingerprint,
                "revoked": agent.revoked,
            }
        )

    async def get_agent(self, agent_id: str) -> Agent | None:
        doc = await self._db.agents.find_one({"_id": agent_id})
        return _agent_from_doc(doc) if doc is not None else None

    async def set_agent_revoked(self, agent_id: str, revoked: bool) -> bool:
        result = await self._db.agents.update_one({"_id": agent_id}, {"$set": {"revoked": revoked}})
        return bool(result.matched_count)

    async def list_agents(self) -> list[Agent]:
        cursor = self._db.agents.find().sort("enrolled_at", ASCENDING)
        return [_agent_from_doc(doc) async for doc in cursor]

    # -- credentials ------------------------------------------------------

    async def insert_credential(self, credential: Credential) -> None:
        await self._db.credentials.insert_one(
            {
                "agent_id": credential.agent_id,
                "credential_sha256": credential.credential_sha256,
                "issued_at": credential.issued_at,
                "expires_at": credential.expires_at,
                "active": credential.active,
                "reason": credential.reason,
            }
        )

    async def get_active_credential(self, agent_id: str) -> Credential | None:
        doc = await self._db.credentials.find_one({"agent_id": agent_id, "active": True})
        return _credential_from_doc(doc) if doc is not None else None

    async def resolve_credential(self, credential_sha256: str) -> Credential | None:
        doc = await self._db.credentials.find_one({"credential_sha256": credential_sha256})
        return _credential_from_doc(doc) if doc is not None else None

    async def deactivate_agent_credentials(self, agent_id: str, *, reason: str) -> None:
        await self._db.credentials.update_many(
            {"agent_id": agent_id, "active": True},
            {
                "$set": {
                    "active": False,
                    "reason": reason,
                    "cleanup_at": utcnow() + _CLEANUP_GRACE,
                }
            },
        )

    # -- pairing codes --------------------------------------------------

    async def insert_pairing_code(self, code: PairingCode) -> None:
        await self._db.pairing_codes.insert_one(
            {
                "_id": code.code,
                "tenant_id": code.tenant_id,
                "site_id": code.site_id,
                "expires_at": code.expires_at,
                "max_uses": code.max_uses,
                "use_count": code.use_count,
                "used": code.used,
                "note": code.note,
            }
        )

    async def claim_pairing_code(
        self, code: str, *, now: datetime, agent_id: str
    ) -> PairingCode | None:
        doc = await self._db.pairing_codes.find_one_and_update(
            {"_id": code, "used": False, "expires_at": {"$gt": now}},
            {
                "$inc": {"use_count": 1},
                "$set": {"used_at": now, "used_by_agent_id": agent_id},
            },
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            return None
        # Flip `used` once the (post-increment) count reaches the cap, so a
        # subsequent claim of a single-use code fails the `used: False` filter.
        if doc["use_count"] >= doc["max_uses"]:
            await self._db.pairing_codes.update_one({"_id": code}, {"$set": {"used": True}})
            doc["used"] = True
        return _pairing_code_from_doc(doc)

    async def list_open_pairing_codes(self, *, now: datetime) -> list[PairingCode]:
        cursor = self._db.pairing_codes.find({"used": False, "expires_at": {"$gt": now}})
        return [_pairing_code_from_doc(doc) async for doc in cursor]

    # -- fleet config ---------------------------------------------------

    async def get_fleet_config(self, agent_id: str) -> FleetConfig | None:
        doc = await self._db.fleet_config.find_one({"_id": agent_id})
        if doc is None:
            return None
        return FleetConfig(
            agent_id=agent_id,
            config_version=int(doc.get("config_version", 0)),
            cameras=list(doc.get("cameras", [])),
        )

    async def bump_fleet_config(self, agent_id: str, cameras: list[dict[str, Any]]) -> FleetConfig:
        doc = await self._db.fleet_config.find_one_and_update(
            {"_id": agent_id},
            {"$inc": {"config_version": 1}, "$set": {"cameras": list(cameras)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return FleetConfig(
            agent_id=agent_id,
            config_version=int(doc["config_version"]),
            cameras=list(doc.get("cameras", [])),
        )

    # -- commands -----------------------------------------------------

    async def queue_command(self, agent_id: str, *, kind: str, payload: dict[str, Any]) -> None:
        await self._db.commands.insert_one(
            {
                "agent_id": agent_id,
                "kind": kind,
                "payload": payload,
                "delivered": False,
                "queued_at": utcnow(),
            }
        )

    async def take_pending_commands(self, agent_id: str) -> list[dict[str, Any]]:
        cursor = self._db.commands.find({"agent_id": agent_id, "delivered": False}).sort(
            "queued_at", ASCENDING
        )
        rows = [doc async for doc in cursor]
        if not rows:
            return []
        await self._db.commands.update_many(
            {"_id": {"$in": [doc["_id"] for doc in rows]}},
            {"$set": {"delivered": True, "cleanup_at": utcnow() + _CLEANUP_GRACE}},
        )
        return [{"kind": doc["kind"], "payload": doc.get("payload", {})} for doc in rows]

    # -- agent status + heartbeat history --------------------------------

    async def upsert_agent_status(self, agent_id: str, status: dict[str, Any]) -> None:
        await self._db.agent_status.update_one({"_id": agent_id}, {"$set": status}, upsert=True)

    async def get_agent_status(self, agent_id: str) -> dict[str, Any] | None:
        doc = await self._db.agent_status.find_one({"_id": agent_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return dict(doc)

    async def append_heartbeat(
        self, agent_id: str, body: dict[str, Any], *, received_at: datetime
    ) -> None:
        await self._db.heartbeats.insert_one(
            {"agent_id": agent_id, "received_at": received_at, "body": body}
        )

    # -- update manifest ----------------------------------------------

    async def get_update_manifest(self, channel: str) -> dict[str, Any] | None:
        doc = await self._db.update_manifest.find_one({"_id": channel})
        if doc is None:
            return None
        doc.pop("_id", None)
        return dict(doc)

    async def set_update_manifest(self, channel: str, manifest: dict[str, Any]) -> None:
        await self._db.update_manifest.replace_one(
            {"_id": channel}, {"_id": channel, **manifest}, upsert=True
        )

    # -- audit trail ------------------------------------------------

    async def append_audit(self, event: dict[str, Any]) -> None:
        await self._db.audit_events.insert_one({"at": utcnow(), **event})

    async def recent_audit(self, limit: int) -> list[dict[str, Any]]:
        cursor = self._db.audit_events.find().sort("at", DESCENDING).limit(limit)
        out: list[dict[str, Any]] = []
        async for doc in cursor:
            doc.pop("_id", None)
            out.append(dict(doc))
        return out


# -- document -> value-object mappers -------------------------------------


def _agent_from_doc(doc: dict[str, Any]) -> Agent:
    """Map an ``agents`` document onto a :class:`~staging_cloud.domain.Agent`."""
    return Agent(
        id=doc["_id"],
        tenant_id=doc["tenant_id"],
        site_id=doc["site_id"],
        machine_id=doc.get("machine_id", ""),
        enrolled_at=doc["enrolled_at"],
        fingerprint=dict(doc.get("fingerprint", {})),
        revoked=bool(doc.get("revoked", False)),
    )


def _credential_from_doc(doc: dict[str, Any]) -> Credential:
    """Map a ``credentials`` document onto a :class:`~staging_cloud.domain.Credential`."""
    return Credential(
        agent_id=doc["agent_id"],
        credential_sha256=doc["credential_sha256"],
        issued_at=doc["issued_at"],
        expires_at=doc["expires_at"],
        active=bool(doc.get("active", False)),
        reason=doc.get("reason"),
    )


def _pairing_code_from_doc(doc: dict[str, Any]) -> PairingCode:
    """Map a ``pairing_codes`` document onto a :class:`~staging_cloud.domain.PairingCode`."""
    return PairingCode(
        code=doc["_id"],
        tenant_id=doc["tenant_id"],
        site_id=doc["site_id"],
        expires_at=doc["expires_at"],
        max_uses=int(doc.get("max_uses", 1)),
        use_count=int(doc.get("use_count", 0)),
        used=bool(doc.get("used", False)),
        note=doc.get("note"),
    )
