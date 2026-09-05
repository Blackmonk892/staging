"""Enrollment contracts: the one-time agent <-> cloud pairing handshake.

Vendored verbatim from ``nzerox.agent.contracts.enrollment`` (NZeroC repo). The
staging cloud validates ``EnrollmentRequest`` on ``POST /v1/agents/enroll`` and
hand-builds the response dict (``EnrollmentResponse.credential`` is a
``SecretStr`` and ``model_dump()`` would mask it). ``EnrollmentResponse`` /
``CredentialRotationResponse`` are kept for the test suite's response-shape
assertions.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from staging_cloud.contracts._base import UtcDatetime


class HardwareFingerprint(BaseModel):
    """Stable-ish description of the physical host requesting enrollment."""

    # `extra="forbid"`: an unexpected key here means the agent and cloud have
    # drifted on what a fingerprint contains -- fail loudly at parse time.
    model_config = ConfigDict(extra="forbid")

    machine_id: str = Field(description="OS-level stable machine identifier (e.g. /etc/machine-id)")
    primary_mac: str = Field(description="MAC address of the primary network interface")
    os_name: str = Field(description="Operating system name, e.g. 'Windows' or 'Linux'")
    os_version: str = Field(description="Operating system version string")
    cpu_count: int = Field(gt=0, description="Number of logical CPUs visible to the agent")
    total_memory_gb: float = Field(gt=0, description="Total physical RAM in gigabytes")
    declared_max_cameras: int = Field(
        gt=0, description="Upper bound on cameras this host is willing to run"
    )


class EnrollmentRequest(BaseModel):
    """Agent -> cloud: 'here is my pairing code and what I am running on'."""

    model_config = ConfigDict(extra="forbid")

    pairing_code: str = Field(
        min_length=1, description="Short-lived operator-issued code that authorises this enrollment"
    )
    fingerprint: HardwareFingerprint = Field(description="Description of the requesting host")


class EnrollmentResponse(BaseModel):
    """Cloud -> agent: the durable identity plus a short-lived credential."""

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID = Field(description="Durable identifier assigned to this agent by the cloud")
    credential: SecretStr = Field(
        description="Bearer credential for all subsequent authenticated calls"
    )
    credential_expires_at: UtcDatetime = Field(
        description="UTC instant after which `credential` is rejected and must be rotated"
    )
    tenant_id: str = Field(description="Identifier of the owning tenant/organisation")
    site_id: str = Field(description="Identifier of the physical site this agent is bound to")


class CredentialRotationResponse(BaseModel):
    """Cloud -> agent: a fresh credential replacing one that is near expiry.

    Deliberately the identity-only subset of ``EnrollmentResponse`` -- ``agent_id``
    never changes across a rotation, and ``tenant_id`` / ``site_id`` bindings are
    set once at enrollment and are not re-sent here.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: UUID = Field(
        description="Durable agent identifier; echoed back unchanged for the caller to cross-check"
    )
    credential: SecretStr = Field(description="The replacement bearer credential")
    credential_expires_at: UtcDatetime = Field(
        description="UTC instant after which the replacement credential must itself be rotated"
    )
