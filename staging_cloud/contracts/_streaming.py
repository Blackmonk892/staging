"""Streaming-config types that ``CameraSyncEntry`` references.

Vendored from the NZeroC repo so a cloud-authored camera config with a
per-camera ``stream_target`` / ``stream_profile`` validates and serialises here
exactly as it does against the agent:

* ``StreamProfile``  <- ``nzerox.config.schema.StreamProfile``
* ``TransportMode``  <- ``nzerox.agent.streaming.contracts.TransportMode``
* ``StreamTarget``   <- ``nzerox.agent.streaming.contracts.StreamTarget``
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StreamProfile(str, Enum):
    """Which ONVIF/RTSP stream profile a camera entry should be ingested from."""

    SUBSTREAM = "SUBSTREAM"
    MAINSTREAM = "MAINSTREAM"


# Every transport mode the agent's router understands. Declared once so a value
# valid on the wire is by construction valid for the agent-side config.
TransportMode = Literal[
    "https_only",
    "webrtc_only",
    "dual",
    "webrtc_primary_https_fallback",
    "https_primary_webrtc_fallback",
]


class StreamTarget(BaseModel):
    """Per-camera streaming override the cloud attaches to a ``CameraSyncEntry``.

    Every field is optional: an unset field falls back to the site-wide
    streaming config on the agent.
    """

    model_config = ConfigDict(extra="forbid")

    codec: Literal["h265", "h264"] | None = Field(default=None, description="Override output codec")
    fps: float | None = Field(default=None, gt=0, description="Override output frame rate")
    resolution: str | None = Field(default=None, description="Override output resolution")
    max_kbps: int | None = Field(default=None, gt=0, description="Override the VBV bitrate cap")
    transport_mode: TransportMode | None = Field(
        default=None, description="Override the site transport mode for this camera"
    )
    safety_floor_level: int | None = Field(
        default=None, ge=0, le=3, description="Override the adaptive-ladder floor for this camera"
    )
    priority: int | None = Field(
        default=None, description="Relative shed priority; lower is throttled/dropped first"
    )
    safety_tagged: bool = Field(
        default=False, description="Safety-critical camera: throttles last, never below its floor"
    )
