"""Agent<->cloud wire contract for one MPEG-TS video segment.

Vendored verbatim from ``nzerox.contracts.video_segment`` (NZeroC repo).
``VideoSegment`` is the metadata that travels (base64-JSON) in the
``X-Segment-Meta`` header of every ``POST /v1/ingest/video/{site}/{camera}``.
It is a frozen dataclass (not Pydantic) because upstream it is produced once per
segment on a hot path and never parsed from untrusted input; the staging cloud
reconstructs it from the header purely to read ``codec`` / ``fps`` /
``resolution`` / ``duration_ms``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Output codec after the agent's processing. "hevc" is the container/RTP spelling
# of H.265; the agent's ``StreamingConfig.codec`` uses the ffmpeg-ish "h265".
SegmentCodec = Literal["hevc", "h264"]


@dataclass(frozen=True, kw_only=True)
class VideoSegment:
    """Metadata for one independently-decodable MPEG-TS segment."""

    camera_id: str
    site_id: str
    seq: int  # monotonic per camera; survives restarts via a persisted counter
    agent_boot_id: str = ""
    codec: SegmentCodec
    resolution: str  # "1280x720" (transcode) or the native substream resolution
    fps: float  # 5.0 for transcode, native for relay
    pts_base_ms: int  # wall-clock epoch ms of the first frame in this segment
    duration_ms: int  # measured segment duration
    config_version: str  # which encode config produced this segment
    gop_frames: int  # frames contained in this segment
    byte_length: int  # payload size in bytes
    keyframe: bool = True
    gap_flag: bool = False


__all__ = ["SegmentCodec", "VideoSegment"]
