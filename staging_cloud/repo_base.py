"""The persistence Protocol every staging-cloud backend implements.

:class:`Repo` is a typing-only ``Protocol`` (no behaviour). Two concrete classes
satisfy it: :class:`staging_cloud.memory_repo.MemoryRepo` (dict-backed, used by
the pytest suite and ``MONGODB_URI=memory://``) and
:class:`staging_cloud.repo.MongoRepo` (``motor`` over MongoDB Atlas / a local
``mongo:7``).

Why a Protocol and not an ABC: ``api.py`` only ever needs the async method
surface, and a ``Protocol`` lets the two implementations stay completely
independent (one has zero third-party deps) while ``mypy --strict`` still
verifies both against a single contract.

Every method is ``async``. Methods return the frozen value objects from
:mod:`staging_cloud.domain` (never raw backend documents) so the API layer is
backend-agnostic. All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from staging_cloud.domain import Agent, Credential, FleetConfig, PairingCode


@runtime_checkable
class Repo(Protocol):
    """Async persistence surface backing the staging control plane."""

    # -- Lifecycle / health -------------------------------------------------

    async def ping(self) -> bool:
        """Return True iff the backend is reachable. Backs ``GET /healthz``."""
        ...

    async def ensure_indexes(self) -> None:
        """Create every collection index (idempotent). Called once on startup."""
        ...

    async def close(self) -> None:
        """Release any backend resources (connection pool). Safe to call twice."""
        ...

    # -- agents -----------------------------------------------------------

    async def insert_agent(self, agent: Agent) -> None:
        """Persist a newly enrolled agent registry row."""
        ...

    async def get_agent(self, agent_id: str) -> Agent | None:
        """Fetch one agent by id, or ``None`` if unknown."""
        ...

    async def set_agent_revoked(self, agent_id: str, revoked: bool) -> bool:
        """Set the ``revoked`` flag; return True if an agent row matched."""
        ...

    async def list_agents(self) -> list[Agent]:
        """Every agent row, enrolment order. Dashboard + ``/admin/state``."""
        ...

    # -- credentials ------------------------------------------------------

    async def insert_credential(self, credential: Credential) -> None:
        """Insert a credential row (active on enroll / rotate, superseded rows kept)."""
        ...

    async def get_active_credential(self, agent_id: str) -> Credential | None:
        """The single active credential for an agent, or ``None``."""
        ...

    async def resolve_credential(self, credential_sha256: str) -> Credential | None:
        """Look a credential up by its stored sha256 hex (path-less data-plane auth)."""
        ...

    async def deactivate_agent_credentials(self, agent_id: str, *, reason: str) -> None:
        """Mark every active credential for an agent inactive (rotation / revoke)."""
        ...

    # -- pairing codes --------------------------------------------------

    async def insert_pairing_code(self, code: PairingCode) -> None:
        """Persist a freshly minted pairing code."""
        ...

    async def claim_pairing_code(
        self, code: str, *, now: datetime, agent_id: str
    ) -> PairingCode | None:
        """Atomically claim one use of ``code``.

        Returns the resulting :class:`PairingCode` on success (the caller only
        reads its ``tenant_id`` / ``site_id`` / ``code``), or ``None`` when the
        code is unknown, expired or already fully used. Implementations MUST make
        the find-and-increment atomic so two concurrent enrols cannot both claim
        the last use; the row is marked ``used`` once ``use_count`` reaches
        ``max_uses``.
        """
        ...

    async def list_open_pairing_codes(self, *, now: datetime) -> list[PairingCode]:
        """Unused, unexpired pairing codes -- for ``GET /admin/pairing-codes``."""
        ...

    # -- fleet config ---------------------------------------------------

    async def get_fleet_config(self, agent_id: str) -> FleetConfig | None:
        """The agent's camera config doc, or ``None`` if none has been authored."""
        ...

    async def bump_fleet_config(self, agent_id: str, cameras: list[dict[str, Any]]) -> FleetConfig:
        """Replace ``cameras`` and monotonically increment ``config_version`` (from 0)."""
        ...

    # -- commands -----------------------------------------------------

    async def queue_command(self, agent_id: str, *, kind: str, payload: dict[str, Any]) -> None:
        """Enqueue one out-of-band command for delivery on the next heartbeat."""
        ...

    async def take_pending_commands(self, agent_id: str) -> list[dict[str, Any]]:
        """Return undelivered commands for an agent and mark them delivered."""
        ...

    # -- agent status + heartbeat history --------------------------------

    async def upsert_agent_status(self, agent_id: str, status: dict[str, Any]) -> None:
        """Upsert the last-seen summary row for an agent (one per agent)."""
        ...

    async def get_agent_status(self, agent_id: str) -> dict[str, Any] | None:
        """The last-seen summary row for an agent, if any."""
        ...

    async def append_heartbeat(
        self, agent_id: str, body: dict[str, Any], *, received_at: datetime
    ) -> None:
        """Append one heartbeat body to the bounded history collection."""
        ...

    # -- update manifest ----------------------------------------------

    async def get_update_manifest(self, channel: str) -> dict[str, Any] | None:
        """The published manifest for a channel, or ``None`` (⇒ the route 404s)."""
        ...

    async def set_update_manifest(self, channel: str, manifest: dict[str, Any]) -> None:
        """Publish (replace) the manifest for a channel."""
        ...

    # -- audit trail ------------------------------------------------

    async def append_audit(self, event: dict[str, Any]) -> None:
        """Append one append-only audit event (enroll / rotate / revoke / ...)."""
        ...

    async def recent_audit(self, limit: int) -> list[dict[str, Any]]:
        """The most recent ``limit`` audit events, newest first."""
        ...
