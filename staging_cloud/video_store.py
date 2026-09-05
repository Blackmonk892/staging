"""In-memory, bounded receiver for live MPEG-TS video segments.

The agent's ``https_only`` transport POSTs one keyframe-aligned MPEG-TS segment
every ~2 s to ``POST /v1/ingest/video/{site_id}/{camera_id}``. This module is the
staging cloud's *test* sink for that stream: it keeps only the most recent
``max_segments_per_camera`` segments per camera in process memory, plus running
counters, and nothing else.

Why in-memory and not a collection / a file:

* **MongoDB Atlas is a bad fit for raw video bytes** -- 16 MB doc cap, no
  streaming reads, and a staging test would churn the free-tier cluster. So the
  ``Repo`` layer (Mongo *and* memory backends) is left completely untouched.
* **Render Free has an ephemeral filesystem** and spins the container down after
  ~15 min idle, so a local ``/tmp`` file buys nothing over a dict and adds I/O.
* The task explicitly wants *no permanent storage* and a *bounded* footprint.

Consequences, by design: everything here is lost on a deploy, a crash or an
idle spin-down, and it is not shared between instances (Render Free runs one).
That is fine -- this only has to prove that live segments are arriving and are
playable in a browser right now.

Concurrency: the staging cloud is a single-threaded asyncio process and none of
these methods ``await`` mid-mutation, so the ring buffers need no lock (same
reasoning as :mod:`staging_cloud.memory_repo`).
"""

from __future__ import annotations

import base64
import json
import math
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from staging_cloud.contracts.video_segment import VideoSegment
from staging_cloud.domain import utcnow

# Default retention: ~20 segments ≈ 40 s at the agent's 2 s cadence -- enough for
# a live HLS window without unbounded growth. Overridable via
# ``StagingSettings.video_max_segments_per_camera``.
DEFAULT_MAX_SEGMENTS_PER_CAMERA = 20

# Hard cap on how many distinct (site, camera) streams are tracked at once; the
# least-recently-fed stream is dropped past this. A staging test drives one or a
# few cameras -- this is just insurance against an accidental fan-out.
DEFAULT_MAX_STREAMS = 32

# Fallback segment duration (seconds) when ``X-Segment-Meta`` carried none, used
# for the HLS ``#EXTINF`` line.
_FALLBACK_SEGMENT_SECONDS = 2.0


@dataclass(frozen=True)
class SegmentMeta:
    """The subset of ``X-Segment-Meta`` (a base64 ``VideoSegment`` JSON) we surface.

    ``raw`` keeps the full decoded dict for the status page; the named fields are
    the ones the task's status view asks for (codec / fps / resolution) plus the
    duration used to build the HLS playlist.
    """

    codec: str = ""
    fps: float | None = None
    resolution: str = ""
    duration_ms: int | None = None
    pts_base_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        """Segment duration in seconds for ``#EXTINF``; a sane fallback if unknown."""
        if self.duration_ms and self.duration_ms > 0:
            return self.duration_ms / 1000.0
        return _FALLBACK_SEGMENT_SECONDS


def _as_float(value: Any) -> float | None:
    """Best-effort ``float`` coercion; ``None`` on anything non-numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    """Best-effort ``int`` coercion; ``None`` on anything non-numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def decode_segment_meta(header_value: str | None) -> SegmentMeta:
    """Decode the ``X-Segment-Meta`` header into a :class:`SegmentMeta`.

    The header is ``base64(JSON of the agent's VideoSegment dataclass)``. When
    the decoded object matches the shared
    :class:`staging_cloud.contracts.video_segment.VideoSegment` contract exactly, that
    is used to pull the fields (so the contract stays the single source of
    truth); otherwise the fields are read leniently straight off the dict. A
    missing / malformed / non-base64 header yields an empty :class:`SegmentMeta`
    rather than an error -- a test receiver must never reject a live segment over
    metadata.
    """
    if not header_value:
        return SegmentMeta()
    try:
        decoded = json.loads(base64.b64decode(header_value).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return SegmentMeta()
    if not isinstance(decoded, dict):
        return SegmentMeta()

    try:
        seg = VideoSegment(**decoded)
    except TypeError:
        return SegmentMeta(
            codec=str(decoded.get("codec", "")),
            fps=_as_float(decoded.get("fps")),
            resolution=str(decoded.get("resolution", "")),
            duration_ms=_as_int(decoded.get("duration_ms")),
            pts_base_ms=_as_int(decoded.get("pts_base_ms")),
            raw=decoded,
        )
    return SegmentMeta(
        codec=seg.codec,
        fps=seg.fps,
        resolution=seg.resolution,
        duration_ms=seg.duration_ms,
        pts_base_ms=seg.pts_base_ms,
        raw=decoded,
    )


@dataclass(frozen=True)
class ReceivedSegment:
    """One stored MPEG-TS segment plus what the receiver knows about it."""

    seq: int
    payload: bytes
    received_at: datetime
    meta: SegmentMeta

    @property
    def byte_length(self) -> int:
        """Size of the stored MPEG-TS payload in bytes."""
        return len(self.payload)


@dataclass
class StreamStatus:
    """A camera stream's live counters -- the body of the status endpoint."""

    site_id: str
    camera_id: str
    last_seq: int
    segment_count: int  # total segments ever received for this camera
    retained_count: int  # segments currently held in the ring buffer
    last_received_at: datetime | None
    bytes_received: int  # total bytes ever received for this camera
    codec: str
    fps: float | None
    resolution: str

    def as_json(self) -> dict[str, Any]:
        """JSON-serialisable form (datetime → ISO)."""
        return {
            "site_id": self.site_id,
            "camera_id": self.camera_id,
            "last_seq": self.last_seq,
            "segment_count": self.segment_count,
            "retained_count": self.retained_count,
            "last_received_at": (
                self.last_received_at.isoformat() if self.last_received_at else None
            ),
            "bytes_received": self.bytes_received,
            "codec": self.codec,
            "fps": self.fps,
            "resolution": self.resolution,
        }


@dataclass
class _StreamState:
    """Per-camera ring buffer + counters that survive buffer eviction."""

    site_id: str
    camera_id: str
    segments: deque[ReceivedSegment]
    total_segments: int = 0
    total_bytes: int = 0
    last_seq: int = -1
    last_received_at: datetime | None = None
    codec: str = ""
    fps: float | None = None
    resolution: str = ""

    def status(self) -> StreamStatus:
        """Snapshot this stream's current counters."""
        return StreamStatus(
            site_id=self.site_id,
            camera_id=self.camera_id,
            last_seq=self.last_seq,
            segment_count=self.total_segments,
            retained_count=len(self.segments),
            last_received_at=self.last_received_at,
            bytes_received=self.total_bytes,
            codec=self.codec,
            fps=self.fps,
            resolution=self.resolution,
        )


class VideoSegmentStore:
    """Process-memory sink: most recent N MPEG-TS segments per camera, plus counters."""

    def __init__(
        self,
        *,
        max_segments_per_camera: int = DEFAULT_MAX_SEGMENTS_PER_CAMERA,
        max_streams: int = DEFAULT_MAX_STREAMS,
    ) -> None:
        """Configure the bounds; allocates nothing until the first segment.

        Args:
            max_segments_per_camera: Ring-buffer depth per ``(site, camera)``.
            max_streams: Cap on tracked streams; the least-recently-fed one is
                dropped past this.
        """
        self._max_segments = max(1, max_segments_per_camera)
        self._max_streams = max(1, max_streams)
        # Ordered so the first key is the least-recently-fed stream (LRU evict).
        self._streams: OrderedDict[tuple[str, str], _StreamState] = OrderedDict()

    @property
    def max_segments_per_camera(self) -> int:
        """Ring-buffer depth per camera (for display)."""
        return self._max_segments

    # -- ingest --------------------------------------------------------------

    def add(
        self, site_id: str, camera_id: str, *, seq: int, payload: bytes, meta: SegmentMeta
    ) -> StreamStatus:
        """Store one received segment and return the camera's updated status.

        Side effects: appends to (and may evict from) the per-camera ring buffer;
        may evict the least-recently-fed *stream* when ``max_streams`` is
        exceeded; advances the camera's running counters.
        """
        key = (site_id, camera_id)
        state = self._streams.get(key)
        if state is None:
            state = _StreamState(
                site_id=site_id,
                camera_id=camera_id,
                segments=deque(maxlen=self._max_segments),
            )
            self._streams[key] = state
            self._evict_streams_if_needed()

        received_at = utcnow()
        state.segments.append(
            ReceivedSegment(seq=seq, payload=payload, received_at=received_at, meta=meta)
        )
        state.total_segments += 1
        state.total_bytes += len(payload)
        state.last_seq = seq
        state.last_received_at = received_at
        if meta.codec:
            state.codec = meta.codec
        if meta.fps is not None:
            state.fps = meta.fps
        if meta.resolution:
            state.resolution = meta.resolution

        # Mark this stream most-recently-used for the LRU eviction order.
        self._streams.move_to_end(key)
        return state.status()

    def _evict_streams_if_needed(self) -> None:
        """Drop the least-recently-fed stream(s) once ``max_streams`` is exceeded."""
        while len(self._streams) > self._max_streams:
            self._streams.popitem(last=False)

    # -- reads -------------------------------------------------------------

    def recent_segments(self, site_id: str, camera_id: str) -> list[ReceivedSegment]:
        """Retained segments for a camera, oldest-first (empty if the camera is unknown)."""
        state = self._streams.get((site_id, camera_id))
        return list(state.segments) if state is not None else []

    def get_segment(self, site_id: str, camera_id: str, seq: int) -> ReceivedSegment | None:
        """One retained segment by sequence number, or ``None`` if it has aged out."""
        state = self._streams.get((site_id, camera_id))
        if state is None:
            return None
        for segment in state.segments:
            if segment.seq == seq:
                return segment
        return None

    def status(self, site_id: str, camera_id: str) -> StreamStatus | None:
        """One camera's status, or ``None`` if nothing has been received for it."""
        state = self._streams.get((site_id, camera_id))
        return state.status() if state is not None else None

    def all_status(self) -> list[StreamStatus]:
        """Every tracked camera's status, most-recently-active first."""
        return [state.status() for _key, state in reversed(self._streams.items())]

    def target_duration(self, site_id: str, camera_id: str) -> int:
        """HLS ``#EXT-X-TARGETDURATION`` (ceil of the longest retained segment)."""
        segments = self.recent_segments(site_id, camera_id)
        if not segments:
            return int(math.ceil(_FALLBACK_SEGMENT_SECONDS))
        longest = max(seg.meta.duration_seconds for seg in segments)
        return max(1, int(math.ceil(longest)))
