"""The live-video test receiver: ingest, bounded retention, HLS playback, status.

Covers ``POST /v1/ingest/video/{site}/{camera}`` (data-plane auth, header
parsing, bounded in-memory storage) and the admin-gated ``/video`` surface
(status JSON, status page, HLS playlist + segments, player page).
"""

from __future__ import annotations

import base64
import json

import pytest
from aiohttp import ClientResponse
from conftest import Rig, enroll, mint_code

pytestmark = pytest.mark.asyncio

_TS_PACKET = b"\x47" + b"\x00" * 187  # one 188-byte MPEG-TS packet


def _meta_header(
    seq: int, *, codec: str = "hevc", fps: float = 5.0, resolution: str = "1280x720"
) -> str:
    """A base64-encoded ``VideoSegment`` JSON, exactly as the agent sends it."""
    payload = {
        "camera_id": "cam-v",
        "site_id": "site-v",
        "seq": seq,
        "agent_boot_id": "boot-1",
        "codec": codec,
        "resolution": resolution,
        "fps": fps,
        "pts_base_ms": 1_000 + seq * 2_000,
        "duration_ms": 2_000,
        "config_version": "0",
        "gop_frames": 10,
        "byte_length": 2 * len(_TS_PACKET),
        "keyframe": True,
        "gap_flag": False,
    }
    return base64.b64encode(json.dumps(payload, sort_keys=True).encode()).decode()


async def _cred(stg: Rig) -> str:
    body = await enroll(stg, await mint_code(stg))
    return str(body["credential"])


async def _post_segment(
    stg: Rig,
    cred: str | None,
    *,
    site: str = "site-v",
    cam: str = "cam-v",
    seq: int = 0,
    body: bytes | None = None,
    meta: str | None = "",
    with_seq_header: bool = True,
) -> ClientResponse:
    headers: dict[str, str] = {"Content-Type": "video/mp2t"}
    if cred is not None:
        headers["Authorization"] = f"Bearer {cred}"
    if with_seq_header:
        headers["X-Segment-Seq"] = str(seq)
    if meta == "":
        headers["X-Segment-Meta"] = _meta_header(seq)
    elif meta is not None:
        headers["X-Segment-Meta"] = meta
    return await stg.client.post(
        f"/v1/ingest/video/{site}/{cam}",
        data=body if body is not None else _TS_PACKET * 2,
        headers=headers,
    )


# -- ingest ------------------------------------------------------------------


async def test_ingest_requires_bearer(stg: Rig) -> None:
    resp = await _post_segment(stg, None)
    assert resp.status == 401


async def test_ingest_stores_a_segment(stg: Rig) -> None:
    cred = await _cred(stg)
    resp = await _post_segment(stg, cred, seq=0)
    assert resp.status == 202
    body = await resp.json()
    assert body["status"] == "stored"
    assert body["seq"] == 0
    assert body["bytes"] == 2 * len(_TS_PACKET)
    assert body["retained_segments"] == 1
    assert body["received_segments"] == 1


async def test_ingest_rejects_revoked_agent(stg: Rig) -> None:
    body = await enroll(stg, await mint_code(stg))
    agent_id, cred = str(body["agent_id"]), str(body["credential"])
    await stg.repo.set_agent_revoked(agent_id, True)
    resp = await _post_segment(stg, cred)
    assert resp.status == 401


async def test_ingest_tolerates_missing_or_bad_headers(stg: Rig) -> None:
    cred = await _cred(stg)
    resp = await _post_segment(stg, cred, meta="not-base64!!", with_seq_header=False)
    assert resp.status == 202
    assert (await resp.json())["seq"] == -1

    status = await (await stg.client.get("/video/status", headers=stg.admin_headers)).json()
    entry = status["streams"][0]
    assert entry["codec"] == ""  # unparseable meta -> empty, not an error


# -- retention bound -------------------------------------------------------


async def test_only_recent_n_segments_are_retained(stg: Rig) -> None:
    cred = await _cred(stg)
    cap = stg.settings.video_max_segments_per_camera
    for seq in range(cap + 5):
        assert (await _post_segment(stg, cred, seq=seq)).status == 202

    status = await (await stg.client.get("/video/status", headers=stg.admin_headers)).json()
    entry = status["streams"][0]
    assert entry["retained_count"] == cap
    assert entry["segment_count"] == cap + 5
    assert entry["last_seq"] == cap + 4

    # The oldest segment has aged out; the newest is still fetchable.
    gone = await stg.client.get("/video/site-v/cam-v/seg/0.ts", headers=stg.admin_headers)
    assert gone.status == 404
    kept = await stg.client.get(f"/video/site-v/cam-v/seg/{cap + 4}.ts", headers=stg.admin_headers)
    assert kept.status == 200


# -- status --------------------------------------------------------------


async def test_status_reports_counters_and_meta(stg: Rig) -> None:
    cred = await _cred(stg)
    await _post_segment(stg, cred, seq=0, body=b"\x47" + b"a" * 187)
    await _post_segment(stg, cred, seq=1, body=b"\x47" + b"b" * 375)

    resp = await stg.client.get("/video/status", headers=stg.admin_headers)
    assert resp.status == 200
    entry = (await resp.json())["streams"][0]
    assert entry["site_id"] == "site-v"
    assert entry["camera_id"] == "cam-v"
    assert entry["last_seq"] == 1
    assert entry["segment_count"] == 2
    assert entry["retained_count"] == 2
    assert entry["bytes_received"] == 188 + 376
    assert entry["codec"] == "hevc"
    assert entry["fps"] == 5.0
    assert entry["resolution"] == "1280x720"
    assert entry["last_received_at"] is not None


async def test_status_page_and_routes_require_admin_token(stg: Rig) -> None:
    for path in (
        "/video",
        "/video/status",
        "/video/site-v/cam-v",
        "/video/site-v/cam-v/index.m3u8",
        "/video/site-v/cam-v/seg/0.ts",
    ):
        assert (await stg.client.get(path)).status == 401


async def test_status_page_lists_the_camera(stg: Rig) -> None:
    cred = await _cred(stg)
    await _post_segment(stg, cred, seq=0)
    resp = await stg.client.get("/video", headers=stg.admin_headers)
    assert resp.status == 200
    assert resp.content_type == "text/html"
    text = await resp.text()
    assert "cam-v" in text and "site-v" in text


# -- playback ----------------------------------------------------------------


async def test_playlist_is_a_live_hls_media_playlist(stg: Rig) -> None:
    cred = await _cred(stg)
    for seq in range(3):
        await _post_segment(stg, cred, seq=seq)

    resp = await stg.client.get(
        "/video/site-v/cam-v/index.m3u8", params={"token": "test-admin-token"}
    )
    assert resp.status == 200
    assert "mpegurl" in resp.content_type
    text = await resp.text()
    assert text.startswith("#EXTM3U")
    assert "#EXT-X-MEDIA-SEQUENCE:0" in text
    assert "#EXT-X-ENDLIST" not in text  # live, keeps growing
    assert "seg/0.ts?token=test-admin-token" in text
    assert "seg/2.ts?token=test-admin-token" in text
    assert text.count("#EXTINF:") == 3


async def test_playlist_404s_until_a_segment_arrives(stg: Rig) -> None:
    resp = await stg.client.get(
        "/video/site-v/cam-v/index.m3u8", params={"token": "test-admin-token"}
    )
    assert resp.status == 404


async def test_segment_bytes_round_trip(stg: Rig) -> None:
    cred = await _cred(stg)
    body = b"\x47" + b"live-mpeg-ts-bytes" * 20
    await _post_segment(stg, cred, seq=7, body=body)

    resp = await stg.client.get(
        "/video/site-v/cam-v/seg/7.ts", params={"token": "test-admin-token"}
    )
    assert resp.status == 200
    assert resp.content_type == "video/mp2t"
    assert await resp.read() == body


async def test_player_page_references_hls_and_the_playlist(stg: Rig) -> None:
    resp = await stg.client.get("/video/site-v/cam-v", params={"token": "test-admin-token"})
    assert resp.status == 200
    assert resp.content_type == "text/html"
    text = await resp.text()
    assert "hls" in text.lower()
    assert "index.m3u8" in text
    assert "<video" in text
