"""Pure control-plane logic: no I/O, no aiohttp, no database.

Everything in this module is a plain function or an immutable value object. It is
the single place the staging cloud's rules live -- token and pairing-code
minting, sha256 hashing of bearer credentials, credential-expiry math, the
data-plane authorization predicate, and the hand-built response dictionaries
that go on the wire.

Why "hand-built response dicts": the shared
:class:`staging_cloud.contracts.enrollment.EnrollmentResponse` wraps ``credential``
in ``SecretStr``, and ``model_dump()`` renders that as ``**********`` -- so the
one place a real credential must appear on the wire (enroll / rotate) cannot go
through the model. Those handlers build the dict here instead; the non-secret
subset is still cross-checked against the contract in the tests.

Why this is pure: ``api.py`` (aiohttp) and both repos (``memory_repo`` / ``repo``)
import *this*, never the other way round. Keeping the rules I/O-free means the
Mongo-backed and dict-backed deployments provably apply identical logic, and the
truth tables can be unit-tested with no server at all.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# -- Value objects --------------------------------------------------------------
#
# Repos map their stored documents onto these frozen dataclasses before handing
# them to the API layer, so `domain` predicates take typed values (never raw
# Mongo documents) and the two backends cannot expose different shapes.


@dataclass(frozen=True)
class Agent:
    """One row of the ``agents`` registry collection."""

    id: str
    tenant_id: str
    site_id: str
    machine_id: str
    enrolled_at: datetime
    fingerprint: dict[str, Any] = field(default_factory=dict)
    revoked: bool = False


@dataclass(frozen=True)
class Credential:
    """One row of the ``credentials`` collection -- a bearer token, hashed only.

    The plaintext token is returned to the agent exactly once (at the enroll /
    rotate call that mints it) and never stored; ``credential_sha256`` is all the
    server keeps, so a database dump cannot be replayed against the API.
    """

    agent_id: str
    credential_sha256: str
    issued_at: datetime
    expires_at: datetime
    active: bool
    reason: str | None = None

    @property
    def expired(self) -> bool:
        """True when this credential's expiry instant is in the past (UTC now)."""
        return self.expires_at <= utcnow()


@dataclass(frozen=True)
class PairingCode:
    """One row of the ``pairing_codes`` collection: a real per-site claim ticket."""

    code: str
    tenant_id: str
    site_id: str
    expires_at: datetime
    max_uses: int
    use_count: int
    used: bool
    note: str | None = None


@dataclass(frozen=True)
class FleetConfig:
    """One row of the ``fleet_config`` collection: the cloud-authored camera doc."""

    agent_id: str
    config_version: int
    cameras: list[dict[str, Any]]


# -- Time ---------------------------------------------------------------------


def utcnow() -> datetime:
    """Current instant as a timezone-aware UTC ``datetime`` (the only clock used here)."""
    return datetime.now(timezone.utc)


def iso(instant: datetime) -> str:
    """ISO-8601 rendering of an aware datetime -- the wire format for every timestamp."""
    return instant.isoformat()


def expiry_after(seconds: float, *, now: datetime | None = None) -> datetime:
    """Return ``now + seconds`` as a UTC datetime. ``now`` defaults to :func:`utcnow`."""
    base = now if now is not None else utcnow()
    return base + timedelta(seconds=seconds)


# -- Secrets ----------------------------------------------------------------

# Crockford base32 (no I/L/O/U -- avoids visual ambiguity in a code an operator
# reads aloud or types from a screen). Pairing codes are `pc-` + 10 of these.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def mint_token() -> str:
    """Mint a fresh opaque bearer credential.

    Output: a ``cred-<43 url-safe chars>`` string with 256 bits of entropy.
    Side effects: none (``secrets`` draws from the OS CSPRNG). The caller hashes
    this with :func:`hash_token` before storing it and returns the plaintext to
    the agent exactly once.
    """
    return f"cred-{secrets.token_urlsafe(32)}"


def mint_pairing_code() -> str:
    """Mint a fresh single-use pairing code: ``pc-`` + 10 Crockford base32 chars."""
    body = "".join(secrets.choice(_CROCKFORD) for _ in range(10))
    return f"pc-{body}"


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a bearer token -- the only form the server persists."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(candidate: str | None, stored_sha256: str) -> bool:
    """Constant-time comparison of a presented token against a stored sha256 hex."""
    if not candidate:
        return False
    return hmac.compare_digest(hash_token(candidate), stored_sha256)


def admin_token_ok(presented: str | None, expected: str) -> bool:
    """Constant-time check of a presented admin token against the configured one."""
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


# -- Authorization --------------------------------------------------------------


def authorize_data_plane(
    agent: Agent | None,
    credential: Credential | None,
    *,
    now: datetime | None = None,
) -> bool:
    """The data-plane auth predicate (heartbeat / config / update-manifest).

    Returns True iff: the agent exists and is not revoked; the presented
    credential resolved to an active row bound to *that* agent; and it has not
    expired. Rotation deliberately does NOT use this -- it tolerates an expired
    credential, which is the whole point of rotating.
    """
    moment = now if now is not None else utcnow()
    if agent is None or agent.revoked:
        return False
    if credential is None or not credential.active:
        return False
    if credential.agent_id != agent.id:
        return False
    return credential.expires_at > moment


def authorize_rotation(agent: Agent | None, credential: Credential | None) -> bool:
    """Rotation auth: current credential required, expiry tolerated, agent not revoked."""
    if agent is None or agent.revoked:
        return False
    if credential is None or not credential.active:
        return False
    return credential.agent_id == agent.id


# -- Hand-built response bodies -----------------------------------------------
#
# The credential-bearing responses cannot round-trip through the pydantic model
# (SecretStr masks the token on dump), so they are assembled here. Field names /
# shapes match `EnrollmentResponse` / `CredentialRotationResponse` /
# `HeartbeatResponse` / `CameraSyncResponse` exactly.


def enrollment_response(
    *,
    agent_id: str,
    credential: str,
    credential_expires_at: datetime,
    tenant_id: str,
    site_id: str,
) -> dict[str, Any]:
    """Body for ``POST /v1/agents/enroll`` -- includes the plaintext credential once."""
    return {
        "agent_id": agent_id,
        "credential": credential,
        "credential_expires_at": iso(credential_expires_at),
        "tenant_id": tenant_id,
        "site_id": site_id,
    }


def rotation_response(
    *, agent_id: str, credential: str, credential_expires_at: datetime
) -> dict[str, Any]:
    """Body for ``POST /v1/agents/{id}/rotate-credential`` -- identity-only subset."""
    return {
        "agent_id": agent_id,
        "credential": credential,
        "credential_expires_at": iso(credential_expires_at),
    }


def heartbeat_response(
    *, commands: list[dict[str, Any]], now: datetime | None = None
) -> dict[str, Any]:
    """Body for ``POST /v1/agents/{id}/heartbeat`` -- server clock + queued commands."""
    return {"server_time": iso(now if now is not None else utcnow()), "commands": list(commands)}


def config_response(fleet: FleetConfig | None, *, now: datetime | None = None) -> dict[str, Any]:
    """Body for ``GET /v1/agents/{id}/config``; a missing doc reads as version 0."""
    version = fleet.config_version if fleet is not None else 0
    cameras = list(fleet.cameras) if fleet is not None else []
    return {
        "config_version": version,
        "cameras": cameras,
        "server_time": iso(now if now is not None else utcnow()),
    }


def unauthorized() -> dict[str, Any]:
    """The single 401 body shape used everywhere on the data plane."""
    return {"error": "unauthorized"}
