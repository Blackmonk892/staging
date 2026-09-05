"""The out-of-scope routes are registered and answer 501, not 404.

``POST /v1/ingest/video/...`` used to be in this list; it is now a real receiver
(``test_staging_video_ingest.py``), so here it only proves it is no longer a 501
and is gated by the data-plane bearer.
"""

from __future__ import annotations

import pytest
from conftest import Rig

pytestmark = pytest.mark.asyncio


async def test_segment_ingest_is_no_longer_501_and_needs_auth(stg: Rig) -> None:
    resp = await stg.client.post("/v1/ingest/video/site-1/cam-1", data=b"ts-bytes")
    assert resp.status == 401  # implemented now; rejects a missing bearer


async def test_whip_is_501(stg: Rig) -> None:
    resp = await stg.client.post("/v1/whip/site-1/cam-1", data="v=0")
    assert resp.status == 501


async def test_context_is_501(stg: Rig) -> None:
    resp = await stg.client.get("/v1/context/cam-1")
    assert resp.status == 501


async def test_alerts_is_501(stg: Rig) -> None:
    resp = await stg.client.post("/v1/alerts", json={"alert_id": "x"})
    assert resp.status == 501


async def test_unknown_route_is_still_404(stg: Rig) -> None:
    resp = await stg.client.get("/v1/nonsense")
    assert resp.status == 404
