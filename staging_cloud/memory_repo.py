"""Dict-backed :class:`~staging_cloud.repo_base.Repo`: no database, no I/O.

Selected when ``MONGODB_URI=memory://``. It backs the entire pytest suite and a
local ``python -m staging_cloud`` run, so the HTTP contract can be exercised
end-to-end with nothing installed but the runtime deps.

It is the exact analogue of ``devcloud.cloud_state.DevCloud`` for this package:
same rules (delegated to :mod:`staging_cloud.domain`), just held in plain dicts
instead of Mongo collections. State lives only for the process lifetime.

Concurrency note: asyncio in one process is single-threaded between ``await``
points, and none of these methods ``await`` mid-mutation, so the
"atomic" claim/bump operations are atomic here for free -- no lock needed. The
Mongo implementation has to do real work for the same guarantee.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from staging_cloud.domain import (
    Agent,
    Credential,
    FleetConfig,
    PairingCode,
    utcnow,
)


class MemoryRepo:
    """In-process implementation of every :class:`Repo` method over dicts/lists."""

    def __init__(self) -> None:
        """Start empty. No connection, no files -- state is process-local."""
        self._agents: dict[str, Agent] = {}
        # agent_id -> list of Credential rows (active + superseded), newest last.
        self._credentials: dict[str, list[Credential]] = {}
        self._pairing_codes: dict[str, PairingCode] = {}
        self._fleet_config: dict[str, FleetConfig] = {}
        self._commands: dict[str, list[dict[str, Any]]] = {}
        self._agent_status: dict[str, dict[str, Any]] = {}
        self._heartbeats: list[dict[str, Any]] = []
        self._manifests: dict[str, dict[str, Any]] = {}
        self._audit: list[dict[str, Any]] = []

    # -- Lifecycle / health -------------------------------------------------

    async def ping(self) -> bool:
        """Always reachable -- the store is this object."""
        return True

    async def ensure_indexes(self) -> None:
        """No-op: there are no indexes to build for a dict backend."""
        return None

    async def close(self) -> None:
        """No-op: nothing to release."""
        return None

    # -- agents -----------------------------------------------------------

    async def insert_agent(self, agent: Agent) -> None:
        self._agents[agent.id] = agent

    async def get_agent(self, agent_id: str) -> Agent | None:
        return self._agents.get(agent_id)

    async def set_agent_revoked(self, agent_id: str, revoked: bool) -> bool:
        current = self._agents.get(agent_id)
        if current is None:
            return False
        # `Agent` is frozen; replace the whole value rather than mutate.
        self._agents[agent_id] = Agent(
            id=current.id,
            tenant_id=current.tenant_id,
            site_id=current.site_id,
            machine_id=current.machine_id,
            enrolled_at=current.enrolled_at,
            fingerprint=current.fingerprint,
            revoked=revoked,
        )
        return True

    async def list_agents(self) -> list[Agent]:
        return sorted(self._agents.values(), key=lambda a: a.enrolled_at)

    # -- credentials ------------------------------------------------------

    async def insert_credential(self, credential: Credential) -> None:
        self._credentials.setdefault(credential.agent_id, []).append(credential)

    async def get_active_credential(self, agent_id: str) -> Credential | None:
        for row in reversed(self._credentials.get(agent_id, [])):
            if row.active:
                return row
        return None

    async def resolve_credential(self, credential_sha256: str) -> Credential | None:
        # Prefer an active row: an inactive row with the same hash only exists in
        # contrived cases (a token re-issued after being deactivated); the Mongo
        # backend's unique index makes that impossible there.
        fallback: Credential | None = None
        for rows in self._credentials.values():
            for row in rows:
                if row.credential_sha256 == credential_sha256:
                    if row.active:
                        return row
                    fallback = fallback or row
        return fallback

    async def deactivate_agent_credentials(self, agent_id: str, *, reason: str) -> None:
        rows = self._credentials.get(agent_id, [])
        for idx, row in enumerate(rows):
            if row.active:
                rows[idx] = Credential(
                    agent_id=row.agent_id,
                    credential_sha256=row.credential_sha256,
                    issued_at=row.issued_at,
                    expires_at=row.expires_at,
                    active=False,
                    reason=reason,
                )

    # -- pairing codes --------------------------------------------------

    async def insert_pairing_code(self, code: PairingCode) -> None:
        self._pairing_codes[code.code] = code

    async def claim_pairing_code(
        self, code: str, *, now: datetime, agent_id: str
    ) -> PairingCode | None:
        doc = self._pairing_codes.get(code)
        if doc is None or doc.used or doc.expires_at <= now:
            return None
        new_use_count = doc.use_count + 1
        updated = PairingCode(
            code=doc.code,
            tenant_id=doc.tenant_id,
            site_id=doc.site_id,
            expires_at=doc.expires_at,
            max_uses=doc.max_uses,
            use_count=new_use_count,
            used=new_use_count >= doc.max_uses,
            note=doc.note,
        )
        self._pairing_codes[code] = updated
        return updated

    async def list_open_pairing_codes(self, *, now: datetime) -> list[PairingCode]:
        return [c for c in self._pairing_codes.values() if not c.used and c.expires_at > now]

    # -- fleet config ---------------------------------------------------

    async def get_fleet_config(self, agent_id: str) -> FleetConfig | None:
        return self._fleet_config.get(agent_id)

    async def bump_fleet_config(self, agent_id: str, cameras: list[dict[str, Any]]) -> FleetConfig:
        current = self._fleet_config.get(agent_id)
        next_version = (current.config_version if current is not None else 0) + 1
        updated = FleetConfig(agent_id=agent_id, config_version=next_version, cameras=list(cameras))
        self._fleet_config[agent_id] = updated
        return updated

    # -- commands -----------------------------------------------------

    async def queue_command(self, agent_id: str, *, kind: str, payload: dict[str, Any]) -> None:
        self._commands.setdefault(agent_id, []).append({"kind": kind, "payload": payload})

    async def take_pending_commands(self, agent_id: str) -> list[dict[str, Any]]:
        pending = self._commands.pop(agent_id, [])
        return list(pending)

    # -- agent status + heartbeat history --------------------------------

    async def upsert_agent_status(self, agent_id: str, status: dict[str, Any]) -> None:
        self._agent_status[agent_id] = {"agent_id": agent_id, **status}

    async def get_agent_status(self, agent_id: str) -> dict[str, Any] | None:
        return self._agent_status.get(agent_id)

    async def append_heartbeat(
        self, agent_id: str, body: dict[str, Any], *, received_at: datetime
    ) -> None:
        self._heartbeats.append({"agent_id": agent_id, "received_at": received_at, "body": body})

    # -- update manifest ----------------------------------------------

    async def get_update_manifest(self, channel: str) -> dict[str, Any] | None:
        return self._manifests.get(channel)

    async def set_update_manifest(self, channel: str, manifest: dict[str, Any]) -> None:
        self._manifests[channel] = dict(manifest)

    # -- audit trail ------------------------------------------------

    async def append_audit(self, event: dict[str, Any]) -> None:
        self._audit.append({"at": utcnow(), **event})

    async def recent_audit(self, limit: int) -> list[dict[str, Any]]:
        return list(reversed(self._audit))[:limit]
