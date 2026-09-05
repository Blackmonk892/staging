"""Central operator UI (``/admin`` + ``/admin/ui/*``) -- auth, and the real
pair → enroll → heartbeat → "appears connected" flow end to end.

These drive the actual HTTP handlers over a loopback socket (the shared ``stg``
rig) and assert on real backend state, not on the presence of HTML buttons.
"""

from __future__ import annotations

import pytest
from conftest import Rig, fingerprint_payload

from staging_cloud.ui import SESSION_COOKIE, session_value

pytestmark = pytest.mark.asyncio

ADMIN_TOKEN = "test-admin-token"


def _cookie_header() -> dict[str, str]:
    """A valid admin session cookie header (as a signed-in browser would send)."""
    return {"Cookie": f"{SESSION_COOKIE}={session_value(ADMIN_TOKEN)}"}


# -- auth ---------------------------------------------------------------


async def test_admin_page_prompts_for_token_when_signed_out(stg: Rig) -> None:
    resp = await stg.client.get("/admin", allow_redirects=False)
    assert resp.status == 401
    body = await resp.text()
    assert 'action="/admin/login"' in body
    assert "Pair a New Agent" not in body  # the real page is not exposed


async def test_query_token_sets_session_and_redirects(stg: Rig) -> None:
    resp = await stg.client.get("/admin", params={"token": ADMIN_TOKEN}, allow_redirects=False)
    assert resp.status == 303
    assert resp.headers["Location"] == "/admin"
    assert resp.cookies[SESSION_COOKIE].value == session_value(ADMIN_TOKEN)


async def test_login_form_good_and_bad_token(stg: Rig) -> None:
    ok = await stg.client.post("/admin/login", data={"token": ADMIN_TOKEN}, allow_redirects=False)
    assert ok.status == 303
    assert ok.cookies[SESSION_COOKIE].value == session_value(ADMIN_TOKEN)

    bad = await stg.client.post("/admin/login", data={"token": "nope"}, allow_redirects=False)
    assert bad.status == 401
    assert SESSION_COOKIE not in bad.cookies


async def test_session_cookie_unlocks_admin_page_and_state(stg: Rig) -> None:
    page = await stg.client.get("/admin", headers=_cookie_header())
    assert page.status == 200
    assert "Pair a New Agent" in await page.text()
    # The token itself must never be rendered into the page.
    assert ADMIN_TOKEN not in await page.text()

    state = await stg.client.get("/admin/ui/state", headers=_cookie_header())
    assert state.status == 200
    assert "agents" in await state.json()


async def test_ui_endpoints_reject_missing_auth(stg: Rig) -> None:
    assert (await stg.client.get("/admin/ui/state")).status == 401
    assert (await stg.client.post("/admin/ui/pairing-codes", json={})).status == 401


async def test_ui_state_also_accepts_the_existing_admin_token(stg: Rig) -> None:
    resp = await stg.client.get("/admin/ui/state", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert resp.status == 200


async def test_logout_clears_session(stg: Rig) -> None:
    resp = await stg.client.post("/admin/logout", allow_redirects=False)
    assert resp.status == 303
    assert resp.cookies[SESSION_COOKIE].value == ""


# -- pairing code minting (layered on the existing backend) --------------


async def test_ui_mint_returns_no_secrets(stg: Rig) -> None:
    resp = await stg.client.post(
        "/admin/ui/pairing-codes",
        headers=_cookie_header(),
        json={"site_id": "s1", "tenant_id": "t1"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["code"].startswith("pc-")
    assert "credential" not in body and "token" not in body
    # It is a real open code the existing admin API also sees.
    listing = await (await stg.client.get("/admin/pairing-codes", headers=stg.admin_headers)).json()
    assert body["code"] in [c["code"] for c in listing["codes"]]


async def test_ui_mint_requires_site_and_tenant(stg: Rig) -> None:
    resp = await stg.client.post(
        "/admin/ui/pairing-codes", headers=_cookie_header(), json={"site_id": "s1"}
    )
    assert resp.status == 400


# -- the whole flow: /admin -> code -> real enroll -> heartbeat -> connected --


async def test_full_pair_enroll_heartbeat_flow(stg: Rig) -> None:
    # 1. Operator (signed in) generates a pairing code through the UI.
    mint = await stg.client.post(
        "/admin/ui/pairing-codes",
        headers=_cookie_header(),
        json={"site_id": "test-site", "tenant_id": "test-tenant"},
    )
    assert mint.status == 200
    code = (await mint.json())["code"]

    # 2. Before enrollment the UI shows the code as open and no matching agent.
    state = await (await stg.client.get("/admin/ui/state", headers=_cookie_header())).json()
    assert code in [c["code"] for c in state["pairing_codes"]]
    assert state["enrollments"] == []

    # 3. The customer's agent enrolls for real with that exact code.
    enroll = await stg.client.post(
        "/v1/agents/enroll",
        json={"pairing_code": code, "fingerprint": fingerprint_payload()},
    )
    assert enroll.status == 200
    enrolled = await enroll.json()
    agent_id = enrolled["agent_id"]
    credential = enrolled["credential"]

    # 4. The UI state now binds that code to the new agent, which shows up
    #    'offline' until it heartbeats.
    state = await (await stg.client.get("/admin/ui/state", headers=_cookie_header())).json()
    assert code not in [c["code"] for c in state["pairing_codes"]]
    ev = next(e for e in state["enrollments"] if e["pairing_code"] == code)
    assert ev["agent_id"] == agent_id
    agent = next(a for a in state["agents"] if a["agent_id"] == agent_id)
    assert agent["site_id"] == "test-site"
    assert agent["status"] == "never"

    # 5. The agent heartbeats -> the UI shows it Connected with a fresh last-seen.
    hb = await stg.client.post(
        f"/v1/agents/{agent_id}/heartbeat",
        headers={"Authorization": f"Bearer {credential}"},
        json={"agent_id": agent_id, "agent_version": "9.9.9", "cameras": [{}]},
    )
    assert hb.status == 200

    state = await (await stg.client.get("/admin/ui/state", headers=_cookie_header())).json()
    agent = next(a for a in state["agents"] if a["agent_id"] == agent_id)
    assert agent["status"] == "connected"
    assert agent["agent_version"] == "9.9.9"
    assert agent["camera_count"] == 1
    assert agent["last_seen_age_seconds"] is not None
    assert agent["last_seen_age_seconds"] < 30


# -- existing surfaces still work --------------------------------------


async def test_legacy_dashboard_and_video_still_served(stg: Rig) -> None:
    root = await stg.client.get("/", headers=stg.admin_headers)
    assert root.status == 200
    assert "nzerox staging cloud" in await root.text()

    video = await stg.client.get("/video", headers=stg.admin_headers)
    assert video.status == 200
    assert "live video receiver" in await video.text()
