"""Update contracts: the signed manifest describing an agent build to install.

Vendored verbatim from ``nzerox.agent.contracts.updates`` (NZeroC repo). The
staging cloud validates ``POST /admin/manifest`` bodies against ``UpdateManifest``
and serves the stored dump from ``GET /v1/agents/{id}/update-manifest``. This is
the manifest shape and its *syntactic* guarantees only -- no signature checking.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from staging_cloud.contracts._base import SemVerStr, Sha256Str, UtcDatetime


class UpdateManifest(BaseModel):
    """A single downloadable agent release and everything needed to trust it."""

    # `extra="forbid"`: a manifest with an unrecognised key might be a newer
    # schema the running updater cannot safely reason about -- refuse it.
    model_config = ConfigDict(extra="forbid")

    version: SemVerStr = Field(description="SemVer version this manifest describes")
    download_url: str = Field(description="URL the update artifact is fetched from")
    sha256: Sha256Str = Field(description="Lower-case hex SHA-256 of the artifact bytes")
    ed25519_signature: str = Field(
        min_length=1, description="Detached Ed25519 signature over the canonical manifest"
    )
    minimum_version_required: SemVerStr = Field(
        description="Oldest running version allowed to jump directly to `version`"
    )
    released_at: UtcDatetime = Field(description="UTC instant this release was published")
    is_mandatory: bool = Field(
        description="If true, the agent must apply this update rather than defer it"
    )
