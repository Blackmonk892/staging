"""Camera-sync contracts: the cloud-authored fleet configuration document.

Vendored verbatim from ``nzerox.agent.contracts.camera_sync`` (NZeroC repo). The
staging cloud validates each entry of a ``POST /admin/agents/{id}/config`` body
against ``CameraSyncEntry`` (``extra="forbid"``, so the accepted field set must
match the agent's exactly) and serves the stored list from
``GET /v1/agents/{id}/config``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from staging_cloud.contracts._base import UtcDatetime
from staging_cloud.contracts._streaming import StreamProfile, StreamTarget


class CameraSyncEntry(BaseModel):
    """One camera's worth of cloud-authored configuration."""

    # `extra="forbid"`: the cloud adding a field the agent does not understand
    # is a deployment-ordering bug we want surfaced, not swallowed.
    model_config = ConfigDict(extra="forbid")

    camera_id: str = Field(description="Fleet-unique identifier for this camera")
    rtsp_url: str = Field(description="Full RTSP URL for the mainstream")
    substream_url: str | None = Field(
        default=None, description="Full RTSP URL for a lower-resolution substream, if any"
    )
    credentials_ref: str | None = Field(
        default=None,
        description="Opaque reference the agent resolves to credentials; never a literal secret",
    )
    fps: float = Field(default=5.0, gt=0, description="Target frame dispatch rate")
    resolution: tuple[int, int] = Field(
        default=(640, 480), description="Expected (width, height) of the stream"
    )
    stream_profile: StreamProfile = Field(
        default=StreamProfile.SUBSTREAM, description="Which ONVIF/RTSP profile this entry targets"
    )
    priority: int = Field(
        default=100,
        description="Relative admission priority; lower is dropped first under resource pressure",
    )
    stream_target: StreamTarget | None = Field(
        default=None,
        description=(
            "Optional per-camera video-streaming override (codec/fps/resolution/bitrate/"
            "transport). When absent, the site-wide streaming config applies."
        ),
    )


class CameraSyncResponse(BaseModel):
    """The full fleet document for one agent at one point in time."""

    model_config = ConfigDict(extra="forbid")

    config_version: int = Field(
        ge=0, description="Monotonic version of this configuration document"
    )
    cameras: list[CameraSyncEntry] = Field(
        default_factory=list, description="Every camera this agent should be running"
    )
    server_time: UtcDatetime = Field(
        description="UTC instant the cloud produced this document (for clock-skew detection)"
    )
