"""``/admin/*`` + ``/`` -- token gate, minted-code round-trip, dashboard HTML."""

from __future__ import annotations

import pytest
from conftest import Rig, enroll, fingerprint_payload, mint_code

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/admin/state"),
        ("POST", "/admin/pairing-codes"),
        ("GET", "/admin/pairing-codes"),
        ("POST", "/admin/agents/a1/revoke"),
        ("POST", "/admin/agents/a1/config"),
        ("POST", "/admin/manifest"),
        ("POST", "/admin/commands/a1"),
    ],
)
async def test_admin_routes_reject_missing_token(stg: Rig, method: str, path: str) -> None:
    resp = await stg.client.request(method, path)
    assert resp.status == 401


async def test_root_redirects_anonymous_to_operator_ui(stg: Rig) -> None:
    resp = await stg.client.get("/", allow_redirects=False)
    assert resp.status in (302, 303)
    assert resp.headers["Location"] == "/admin"


async def test_root_still_token_gated_for_dashboard_body(stg: Rig) -> None:
    # A wrong token is not a signed-in admin -> still bounced to the UI, never
    # the raw dashboard.
    resp = await stg.client.get("/", headers={"X-Admin-Token": "wrong"}, allow_redirects=False)
    assert resp.status in (302, 303)
    assert resp.headers["Location"] == "/admin"


async def test_admin_rejects_wrong_token(stg: Rig) -> None:
    resp = await stg.client.get("/admin/state", headers={"X-Admin-Token": "wrong"})
    assert resp.status == 401


async def test_admin_token_accepted_via_query_and_header_and_bearer(stg: Rig) -> None:
    for headers, params in (
        ({"X-Admin-Token": "test-admin-token"}, None),
        ({"Authorization": "Bearer test-admin-token"}, None),
        (None, {"token": "test-admin-token"}),
    ):
        resp = await stg.client.get("/admin/state", headers=headers, params=params)
        assert resp.status == 200, (headers, params)


async def test_minted_code_round_trips_into_enroll(stg: Rig) -> None:
    code = await mint_code(stg, site_id="site-x", tenant_id="tenant-x")
    body = await enroll(stg, code)
    assert body["site_id"] == "site-x"

    state = await (await stg.client.get("/admin/state", headers=stg.admin_headers)).json()
    assert any(a["agent_id"] == body["agent_id"] for a in state["agents"])


async def test_list_pairing_codes_hides_used_ones(stg: Rig) -> None:
    code = await mint_code(stg)
    await stg.client.post(
        "/v1/agents/enroll",
        json={"pairing_code": code, "fingerprint": fingerprint_payload()},
    )
    listing = await (await stg.client.get("/admin/pairing-codes", headers=stg.admin_headers)).json()
    assert code not in [c["code"] for c in listing["codes"]]


async def test_revoke_blocks_further_data_plane(stg: Rig) -> None:
    body = await enroll(stg, await mint_code(stg))
    agent_id, cred = str(body["agent_id"]), str(body["credential"])

    revoke = await stg.client.post(f"/admin/agents/{agent_id}/revoke", headers=stg.admin_headers)
    assert revoke.status == 200

    hb = await stg.client.post(
        f"/v1/agents/{agent_id}/heartbeat",
        headers={"Authorization": f"Bearer {cred}"},
        json={"agent_id": agent_id},
    )
    assert hb.status == 401


async def test_queue_command_shows_on_next_heartbeat(stg: Rig) -> None:
    body = await enroll(stg, await mint_code(stg))
    agent_id, cred = str(body["agent_id"]), str(body["credential"])

    q = await stg.client.post(
        f"/admin/commands/{agent_id}",
        headers=stg.admin_headers,
        json={"kind": "rotate_credential", "payload": {}},
    )
    assert q.status == 200

    hb = await stg.client.post(
        f"/v1/agents/{agent_id}/heartbeat",
        headers={"Authorization": f"Bearer {cred}"},
        json={"agent_id": agent_id},
    )
    assert [c["kind"] for c in (await hb.json())["commands"]] == ["rotate_credential"]


async def test_dashboard_renders_html_with_agent(stg: Rig) -> None:
    body = await enroll(stg, await mint_code(stg, site_id="site-dash"))
    resp = await stg.client.get("/", headers=stg.admin_headers)
    assert resp.status == 200
    assert resp.content_type == "text/html"
    text = await resp.text()
    assert "nzerox staging cloud" in text
    assert str(body["agent_id"]) in text
    assert "<script" not in text.lower()  # no JS
