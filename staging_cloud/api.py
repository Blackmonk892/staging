"""aiohttp handlers + ``build_app(repo, settings)`` for the staging control plane.

This module is the thin HTTP shell -- the exact analogue of ``devcloud/server.py``.
Every handler: pulls the bearer / admin token off the request, parses the body
(strictly where a contract exists, leniently for heartbeat), delegates the rule
to :mod:`staging_cloud.domain`, calls one or two :class:`~staging_cloud.repo_base.Repo`
methods, and returns ``web.json_response``. No business logic lives here that is
not a one-liner around ``domain`` + ``repo``.

Route groups:

* **Contract parity with DevCloud** (real): enroll, rotate-credential, heartbeat,
  config, update-manifest.
* **Live video test receiver** (real): ``POST /v1/ingest/video/{site}/{camera}``
  stores the most recent MPEG-TS segments per camera in a bounded in-memory ring
  (:mod:`staging_cloud.video_store`) -- authenticated with the *same* data-plane
  bearer as heartbeat/config. The admin-gated ``/video`` surface plays them back
  as live HLS and shows per-camera counters.
* **Registered but 501** (outside the target flow, kept so a mis-pointed agent
  gets a clear logged error, not a 404): WHIP, context, alerts.
* **Staging-only**: ``/healthz`` (platform health check), ``/`` dashboard, and
  the admin-token-gated ``/admin/*`` surface (pairing codes, revoke, config,
  manifest, commands, machine-readable state).

Auth: data-plane routes resolve the ``Authorization: Bearer`` token to a stored
sha256 and run :func:`domain.authorize_data_plane`. ``/admin/*`` and ``/`` take
the admin token from ``?token=``, ``X-Admin-Token`` or a bearer header and
compare it in constant time. There are no sessions.
"""

from __future__ import annotations

import html
import json
import logging
import uuid
from typing import Any
from urllib.parse import quote

from aiohttp import web
from pydantic import ValidationError

from staging_cloud import domain
from staging_cloud.contracts.camera_sync import CameraSyncEntry
from staging_cloud.contracts.enrollment import EnrollmentRequest
from staging_cloud.contracts.heartbeat import PendingCommand
from staging_cloud.contracts.updates import UpdateManifest
from staging_cloud.dashboard import collect_snapshot, render_dashboard
from staging_cloud.domain import Agent, Credential, PairingCode
from staging_cloud.repo_base import Repo
from staging_cloud.settings import StagingSettings
from staging_cloud.ui import (
    UI_REPO_KEY,
    UI_SETTINGS_KEY,
    admin_session_ok,
    register_ui_routes,
)
from staging_cloud.video_store import (
    StreamStatus,
    VideoSegmentStore,
    decode_segment_meta,
)

_logger = logging.getLogger("staging_cloud")

_REPO_KEY: web.AppKey[Repo] = web.AppKey("repo", Repo)
_SETTINGS_KEY: web.AppKey[StagingSettings] = web.AppKey("settings", StagingSettings)
_VIDEO_KEY: web.AppKey[VideoSegmentStore] = web.AppKey("video", VideoSegmentStore)

# hls.js for browser MPEG-TS/HLS playback (Chrome/Firefox have no native HLS).
# Pinned; Safari falls back to native HLS and needs no script.
_HLSJS_URL = "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.13/hls.min.js"


# -- request helpers --------------------------------------------------------


def _bearer(request: web.Request) -> str | None:
    """Token from an ``Authorization: Bearer <token>`` header, or ``None``."""
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    return header[len(prefix) :] if header.startswith(prefix) else None


async def _read_json(request: web.Request) -> dict[str, Any]:
    """Best-effort JSON object body; an empty / non-object / invalid body ⇒ ``{}``."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _admin_token(request: web.Request) -> str | None:
    """Admin token from ``?token=``, ``X-Admin-Token``, or a bearer header."""
    return request.query.get("token") or request.headers.get("X-Admin-Token") or _bearer(request)


def _require_admin(request: web.Request) -> None:
    """Raise ``HTTPUnauthorized`` unless a valid admin token is present."""
    settings = request.app[_SETTINGS_KEY]
    if not domain.admin_token_ok(_admin_token(request), settings.staging_admin_token):
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "admin token required"}),
            content_type="application/json",
        )


async def _resolve_data_plane(
    request: web.Request, agent_id: str
) -> tuple[Agent | None, Credential | None]:
    """Load the path agent + resolve the presented bearer to a stored credential."""
    repo = request.app[_REPO_KEY]
    agent = await repo.get_agent(agent_id)
    token = _bearer(request)
    credential = await repo.resolve_credential(domain.hash_token(token)) if token else None
    return agent, credential


async def _deny_data_plane(request: web.Request, agent_id: str, reason: str) -> web.Response:
    """Audit a data-plane auth failure and return the shared 401 body."""
    repo = request.app[_REPO_KEY]
    await repo.append_audit(
        {"kind": "auth_failed", "agent_id": agent_id, "path": request.path, "reason": reason}
    )
    return web.json_response(domain.unauthorized(), status=401)


# -- contract-parity handlers -------------------------------------------------


async def _handle_enroll(request: web.Request) -> web.Response:
    """``POST /v1/agents/enroll`` -- pairing code + fingerprint ⇒ identity + credential."""
    repo = request.app[_REPO_KEY]
    settings = request.app[_SETTINGS_KEY]
    raw = await _read_json(request)
    try:
        parsed = EnrollmentRequest.model_validate(raw)
    except ValidationError as exc:
        return web.json_response(
            {"error": "invalid enrollment request", "detail": str(exc)}, status=400
        )

    now = domain.utcnow()
    agent_id = str(uuid.uuid4())
    claimed = await repo.claim_pairing_code(parsed.pairing_code, now=now, agent_id=agent_id)
    if claimed is None:
        await repo.append_audit(
            {"kind": "pairing_code_rejected", "agent_id": agent_id, "code": parsed.pairing_code}
        )
        return web.json_response({"error": "invalid pairing code"}, status=403)

    token = domain.mint_token()
    expires_at = domain.expiry_after(settings.credential_ttl_seconds, now=now)
    agent = Agent(
        id=agent_id,
        tenant_id=claimed.tenant_id,
        site_id=claimed.site_id,
        machine_id=parsed.fingerprint.machine_id,
        enrolled_at=now,
        fingerprint=parsed.fingerprint.model_dump(mode="json"),
    )
    await repo.insert_agent(agent)
    await repo.insert_credential(
        Credential(
            agent_id=agent_id,
            credential_sha256=domain.hash_token(token),
            issued_at=now,
            expires_at=expires_at,
            active=True,
            reason="enroll",
        )
    )
    await repo.append_audit(
        {
            "kind": "enroll",
            "agent_id": agent_id,
            "tenant_id": claimed.tenant_id,
            "site_id": claimed.site_id,
            "pairing_code": claimed.code,
        }
    )
    return web.json_response(
        domain.enrollment_response(
            agent_id=agent_id,
            credential=token,
            credential_expires_at=expires_at,
            tenant_id=claimed.tenant_id,
            site_id=claimed.site_id,
        )
    )


async def _handle_rotate(request: web.Request) -> web.Response:
    """``POST /v1/agents/{id}/rotate-credential`` -- swap a near-expiry token.

    Requires the *current* active credential but tolerates it being expired --
    that is the entire purpose of rotation.
    """
    repo = request.app[_REPO_KEY]
    settings = request.app[_SETTINGS_KEY]
    agent_id = request.match_info["agent_id"]
    agent, credential = await _resolve_data_plane(request, agent_id)
    if not domain.authorize_rotation(agent, credential):
        return await _deny_data_plane(request, agent_id, "rotation not authorized")

    now = domain.utcnow()
    token = domain.mint_token()
    expires_at = domain.expiry_after(settings.credential_ttl_seconds, now=now)
    await repo.deactivate_agent_credentials(agent_id, reason="rotated")
    await repo.insert_credential(
        Credential(
            agent_id=agent_id,
            credential_sha256=domain.hash_token(token),
            issued_at=now,
            expires_at=expires_at,
            active=True,
            reason="rotate",
        )
    )
    await repo.append_audit({"kind": "rotate", "agent_id": agent_id})
    return web.json_response(
        domain.rotation_response(
            agent_id=agent_id, credential=token, credential_expires_at=expires_at
        )
    )


async def _handle_heartbeat(request: web.Request) -> web.Response:
    """``POST /v1/agents/{id}/heartbeat`` -- store telemetry, return queued commands.

    The body is parsed leniently: a real agent's ``HeartbeatRequest`` is stored
    verbatim in the history collection, and a best-effort summary (version,
    applied config version, camera count) is upserted for the dashboard.
    """
    repo = request.app[_REPO_KEY]
    agent_id = request.match_info["agent_id"]
    agent, credential = await _resolve_data_plane(request, agent_id)
    if not domain.authorize_data_plane(agent, credential):
        return await _deny_data_plane(request, agent_id, "heartbeat auth failed")

    now = domain.utcnow()
    body = await _read_json(request)
    await repo.append_heartbeat(agent_id, body, received_at=now)
    await repo.upsert_agent_status(
        agent_id,
        {
            "last_seen_at": domain.iso(now),
            "agent_version": body.get("agent_version"),
            "config_version_applied": body.get("config_version_applied"),
            "camera_count": len(body.get("cameras", []) or []),
            "last_heartbeat": body,
        },
    )
    commands = await repo.take_pending_commands(agent_id)
    return web.json_response(domain.heartbeat_response(commands=commands, now=now))


async def _handle_config(request: web.Request) -> web.Response:
    """``GET /v1/agents/{id}/config`` -- the cloud-authored camera fleet document."""
    repo = request.app[_REPO_KEY]
    agent_id = request.match_info["agent_id"]
    agent, credential = await _resolve_data_plane(request, agent_id)
    if not domain.authorize_data_plane(agent, credential):
        return await _deny_data_plane(request, agent_id, "config auth failed")
    fleet = await repo.get_fleet_config(agent_id)
    return web.json_response(domain.config_response(fleet))


async def _handle_update_manifest(request: web.Request) -> web.Response:
    """``GET /v1/agents/{id}/update-manifest`` -- the current signed manifest, if any."""
    repo = request.app[_REPO_KEY]
    settings = request.app[_SETTINGS_KEY]
    agent_id = request.match_info["agent_id"]
    agent, credential = await _resolve_data_plane(request, agent_id)
    if not domain.authorize_data_plane(agent, credential):
        return await _deny_data_plane(request, agent_id, "update-manifest auth failed")
    manifest = await repo.get_update_manifest(settings.update_channel)
    if manifest is None:
        return web.json_response({"error": "no update manifest published"}, status=404)
    return web.json_response(manifest)


# -- registered-but-unimplemented ------------------------------------------


async def _handle_not_implemented(request: web.Request) -> web.Response:
    """Any WHIP / context / alert route: log the hit, return a clean 501.

    Registered rather than left to 404 so an agent pointed at staging for one of
    these gets an unambiguous "not implemented in staging" instead of a
    misleading "route not found".
    """
    _logger.info("501 for out-of-scope route %s %s", request.method, request.path)
    return web.json_response({"error": "not implemented in staging"}, status=501)


# -- live video test receiver ---------------------------------------------


def _parse_seq(request: web.Request) -> int:
    """``X-Segment-Seq`` as an int, or ``-1`` when absent / unparseable (mirrors DevCloud)."""
    raw = request.headers.get("X-Segment-Seq")
    try:
        return int(raw) if raw is not None else -1
    except ValueError:
        return -1


async def _authorize_ingest(request: web.Request) -> Credential | None:
    """Resolve the bearer to an active data-plane credential, or ``None``.

    Uses exactly the heartbeat/config mechanism: hash the bearer, look it up,
    load its agent, run :func:`domain.authorize_data_plane`. The ingest path
    carries ``{site_id}`` (not ``{agent_id}``), so the agent is taken from the
    resolved credential rather than the URL.
    """
    repo = request.app[_REPO_KEY]
    token = _bearer(request)
    if not token:
        return None
    credential = await repo.resolve_credential(domain.hash_token(token))
    if credential is None:
        return None
    agent = await repo.get_agent(credential.agent_id)
    if not domain.authorize_data_plane(agent, credential):
        return None
    return credential


async def _handle_ingest_video(request: web.Request) -> web.Response:
    """``POST /v1/ingest/video/{site_id}/{camera_id}`` -- receive one live MPEG-TS segment.

    Auth: the shared data-plane bearer. Body: the raw MPEG-TS segment. Headers:
    ``X-Segment-Seq`` (int) and ``X-Segment-Meta`` (base64 ``VideoSegment``
    JSON). The segment is appended to a bounded in-memory ring
    (:class:`~staging_cloud.video_store.VideoSegmentStore`); nothing is written
    to Mongo or disk. Returns ``202`` with the camera's updated counters -- the
    agent's transport treats any 2xx as delivered.
    """
    credential = await _authorize_ingest(request)
    if credential is None:
        return await _deny_data_plane(request, "unknown", "video ingest auth failed")

    site_id = request.match_info["site_id"]
    camera_id = request.match_info["camera_id"]
    seq = _parse_seq(request)
    meta = decode_segment_meta(request.headers.get("X-Segment-Meta"))
    payload = await request.read()

    store = request.app[_VIDEO_KEY]
    status = store.add(site_id, camera_id, seq=seq, payload=payload, meta=meta)
    return web.json_response(
        {
            "status": "stored",
            "seq": seq,
            "bytes": len(payload),
            "retained_segments": status.retained_count,
            "received_segments": status.segment_count,
        },
        status=202,
    )


# -- admin-gated video playback + status --------------------------------


def _view_token_qs(request: web.Request) -> str:
    """``?token=…`` (url-encoded) to thread onto playlist / player sub-URLs, or ``""``.

    The admin token has to ride the query string so the browser's HLS fetches
    (which carry no custom headers) stay authenticated against ``_require_admin``.
    """
    token = request.query.get("token", "")
    return f"?token={quote(token, safe='')}" if token else ""


async def _handle_video_status(request: web.Request) -> web.Response:
    """``GET /video/status`` -- JSON counters for every camera currently streaming."""
    _require_admin(request)
    store = request.app[_VIDEO_KEY]
    return web.json_response({"streams": [s.as_json() for s in store.all_status()]})


def _video_index_row(status: StreamStatus, qs: str) -> str:
    """One ``<tr>`` of the ``/video`` status table for one camera stream."""
    last_seen = status.last_received_at.isoformat() if status.last_received_at else "never"
    player = f"/video/{quote(status.site_id)}/{quote(status.camera_id)}{qs}"
    fps = ("%g" % status.fps) if status.fps else "-"
    return (
        "<tr>"
        f'<td><a href="{player}">'
        f"{html.escape(status.site_id)} / {html.escape(status.camera_id)}</a></td>"
        f"<td>{status.last_seq}</td>"
        f"<td>{status.retained_count} / {status.segment_count}</td>"
        f"<td>{html.escape(last_seen)}</td>"
        f"<td>{status.bytes_received:,}</td>"
        f"<td>{html.escape(status.codec or '-')}</td>"
        f"<td>{fps}</td>"
        f"<td>{html.escape(status.resolution or '-')}</td>"
        "</tr>"
    )


async def _handle_video_index(request: web.Request) -> web.Response:
    """``GET /video`` -- HTML status page listing every streaming camera (auto-refresh)."""
    _require_admin(request)
    store = request.app[_VIDEO_KEY]
    qs = _view_token_qs(request)
    rows = "".join(_video_index_row(s, qs) for s in store.all_status()) or (
        '<tr><td colspan="8" class="muted">no video received yet</td></tr>'
    )
    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="3">
<title>staging cloud video</title>{_VIDEO_STYLE}</head><body>
<h1>staging cloud &mdash; live video receiver</h1>
<div class="muted">in-memory, bounded to the {store.max_segments_per_camera} most recent
  segments per camera &middot; auto-refresh 3s</div>
<table>
<tr><th>site / camera</th><th>last seq</th><th>segs (kept/total)</th><th>last received</th>
    <th>bytes</th><th>codec</th><th>fps</th><th>resolution</th></tr>
{rows}
</table>
</body></html>"""
    return web.Response(text=body, content_type="text/html")


async def _handle_video_player(request: web.Request) -> web.Response:
    """``GET /video/{site_id}/{camera_id}`` -- an HTML page that plays the live HLS."""
    _require_admin(request)
    site_id = request.match_info["site_id"]
    camera_id = request.match_info["camera_id"]
    title = f"{html.escape(site_id)} / {html.escape(camera_id)}"
    body = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{title} &middot; staging video</title>{_VIDEO_STYLE}</head><body>
<h1>{title}</h1>
<video id="v" controls autoplay muted playsinline></video>
<div id="stat" class="muted">waiting for segments&hellip;</div>
<p class="muted"><a href="/video{_view_token_qs(request)}">&larr; all cameras</a></p>
<script src="{_HLSJS_URL}" onerror="document.getElementById('stat').textContent=
  'could not load hls.js; only Safari can play this stream natively'"></script>
<script>{_VIDEO_PLAYER_JS}</script>
</body></html>"""
    return web.Response(text=body, content_type="text/html")


async def _handle_video_playlist(request: web.Request) -> web.Response:
    """``GET /video/{site_id}/{camera_id}/index.m3u8`` -- a live (sliding) HLS media playlist."""
    _require_admin(request)
    store = request.app[_VIDEO_KEY]
    site_id = request.match_info["site_id"]
    camera_id = request.match_info["camera_id"]
    segments = store.recent_segments(site_id, camera_id)
    if not segments:
        # No ENDLIST, nothing to serve yet -- the player retries.
        return web.Response(status=404, text="no segments received yet")

    qs = _view_token_qs(request)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{store.target_duration(site_id, camera_id)}",
        f"#EXT-X-MEDIA-SEQUENCE:{segments[0].seq}",
    ]
    for seg in segments:
        lines.append(f"#EXTINF:{seg.meta.duration_seconds:.3f},")
        lines.append(f"seg/{seg.seq}.ts{qs}")
    return web.Response(
        text="\n".join(lines) + "\n",
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


async def _handle_video_segment(request: web.Request) -> web.Response:
    """``GET /video/{site_id}/{camera_id}/seg/{seq}.ts`` -- one retained MPEG-TS segment."""
    _require_admin(request)
    store = request.app[_VIDEO_KEY]
    seg = store.get_segment(
        request.match_info["site_id"],
        request.match_info["camera_id"],
        int(request.match_info["seq"]),
    )
    if seg is None:
        return web.Response(status=404, text="segment not retained")
    return web.Response(
        body=seg.payload, content_type="video/mp2t", headers={"Cache-Control": "no-store"}
    )


_VIDEO_STYLE = (
    "<style>"
    "body{font:13px/1.5 ui-monospace,Menlo,Consolas,monospace;margin:0;padding:1.5rem;"
    "background:#0f1115;color:#d7dbe0}"
    "h1{font-size:1.1rem;margin:0 0 .5rem}.muted{color:#7f8794}"
    "a{color:#8ab4f8}video{width:100%;max-width:960px;background:#000;border-radius:6px}"
    "table{border-collapse:collapse;width:100%;margin-top:1rem}"
    "th,td{text-align:left;padding:.3rem .55rem;border-bottom:1px solid #262b33}"
    "th{color:#9aa3af}#stat{margin-top:.5rem}"
    "</style>"
)

# Loads the live playlist, retrying every 2s until segments arrive, and polls the
# status endpoint so the on-page counters move while the camera runs.
_VIDEO_PLAYER_JS = """
(function () {
  var qs = location.search || "";
  var base = location.pathname.replace(/\\/+$/, "");
  var playlist = base + "/index.m3u8" + qs;
  var video = document.getElementById("v");
  var stat = document.getElementById("stat");

  function start() {
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = playlist;                 // Safari: native HLS
      video.play().catch(function () {});
      return;
    }
    if (!window.Hls || !window.Hls.isSupported()) {
      stat.textContent = "this browser cannot play HLS";
      return;
    }
    var hls = new Hls({ liveSyncDurationCount: 2, lowLatencyMode: true });
    hls.loadSource(playlist);
    hls.attachMedia(video);
    hls.on(window.Hls.Events.ERROR, function (_e, data) {
      if (!data.fatal) return;
      hls.destroy();
      setTimeout(start, 2000);              // no segments yet / transient -> retry
    });
  }

  function poll() {
    fetch(base.replace(/\\/[^/]+\\/[^/]+$/, "") + "/status" + qs)
      .then(function (r) { return r.ok ? r.json() : { streams: [] }; })
      .then(function (d) {
        var parts = base.split("/");
        var cam = decodeURIComponent(parts.pop());
        var site = decodeURIComponent(parts.pop());
        var s = (d.streams || []).find(function (x) {
          return x.site_id === site && x.camera_id === cam;
        });
        var dot = " \\u00b7 ";
        stat.textContent = s
          ? ("seq " + s.last_seq + dot + s.retained_count + "/" + s.segment_count + " segs" +
             dot + s.bytes_received.toLocaleString() + " B" +
             dot + (s.codec || "?") + " " + (s.resolution || "?") + " @" + (s.fps || "?") + "fps" +
             dot + (s.last_received_at || ""))
          : "waiting for segments\\u2026";
      })
      .catch(function () {});
  }

  start();
  poll();
  setInterval(poll, 3000);
})();
"""


# -- staging-only: health + dashboard ------------------------------------


async def _handle_healthz(request: web.Request) -> web.Response:
    """``GET /healthz`` -- Cloud Run health check; 200 only when the backend pings."""
    repo = request.app[_REPO_KEY]
    settings = request.app[_SETTINGS_KEY]
    try:
        db_ok = await repo.ping()
    except Exception as exc:  # noqa: BLE001 - any backend error ⇒ unhealthy
        _logger.warning("healthz ping failed: %r", exc)
        db_ok = False
    body = {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "down",
        "env": settings.env_name,
    }
    return web.json_response(body, status=200 if db_ok else 503)


async def _handle_dashboard(request: web.Request) -> web.Response:
    """``GET /`` -- the JS-free HTML operations dashboard for a signed-in admin.

    An unauthenticated browser hitting the bare root is redirected to the clean
    operator UI at ``/admin`` (which renders its own sign-in form) instead of a
    bare JSON 401, so ``/`` is a usable entry point. The dashboard body itself
    stays gated on the admin token or an equivalent admin session cookie -- no
    data is served without auth.
    """
    settings = request.app[_SETTINGS_KEY]
    token_ok = domain.admin_token_ok(_admin_token(request), settings.staging_admin_token)
    if not (token_ok or admin_session_ok(request)):
        raise web.HTTPFound("/admin")
    repo = request.app[_REPO_KEY]
    snapshot = await collect_snapshot(
        repo, env_name=settings.env_name, manifest_channel=settings.update_channel
    )
    return web.Response(text=render_dashboard(snapshot), content_type="text/html")


# -- staging-only: /admin/* --------------------------------------------


async def _handle_admin_state(request: web.Request) -> web.Response:
    """``GET /admin/state`` -- machine-readable registry snapshot."""
    _require_admin(request)
    repo = request.app[_REPO_KEY]
    settings = request.app[_SETTINGS_KEY]
    snapshot = await collect_snapshot(
        repo, env_name=settings.env_name, manifest_channel=settings.update_channel
    )
    return web.json_response(snapshot.as_json())


async def _handle_admin_mint_codes(request: web.Request) -> web.Response:
    """``POST /admin/pairing-codes`` -- mint one or more single-use pairing codes."""
    _require_admin(request)
    repo = request.app[_REPO_KEY]
    settings = request.app[_SETTINGS_KEY]
    body = await _read_json(request)
    site_id = str(body.get("site_id", "")).strip()
    tenant_id = str(body.get("tenant_id", "")).strip()
    if not site_id or not tenant_id:
        return web.json_response({"error": "site_id and tenant_id are required"}, status=400)
    ttl_seconds = int(body.get("ttl_seconds") or settings.pairing_code_ttl_seconds)
    count = max(1, int(body.get("count") or 1))
    max_uses = max(1, int(body.get("max_uses") or 1))
    note = body.get("note")
    now = domain.utcnow()
    expires_at = domain.expiry_after(ttl_seconds, now=now)

    minted: list[str] = []
    for _ in range(count):
        code = domain.mint_pairing_code()
        await repo.insert_pairing_code(
            PairingCode(
                code=code,
                tenant_id=tenant_id,
                site_id=site_id,
                expires_at=expires_at,
                max_uses=max_uses,
                use_count=0,
                used=False,
                note=str(note) if note is not None else None,
            )
        )
        minted.append(code)
    await repo.append_audit(
        {"kind": "pairing_code_minted", "site_id": site_id, "tenant_id": tenant_id, "count": count}
    )
    return web.json_response(
        {"codes": minted, "expires_at": domain.iso(expires_at), "max_uses": max_uses}
    )


async def _handle_admin_list_codes(request: web.Request) -> web.Response:
    """``GET /admin/pairing-codes`` -- unused, unexpired codes."""
    _require_admin(request)
    repo = request.app[_REPO_KEY]
    codes = await repo.list_open_pairing_codes(now=domain.utcnow())
    return web.json_response(
        {
            "codes": [
                {
                    "code": c.code,
                    "tenant_id": c.tenant_id,
                    "site_id": c.site_id,
                    "expires_at": domain.iso(c.expires_at),
                    "max_uses": c.max_uses,
                    "use_count": c.use_count,
                    "note": c.note,
                }
                for c in codes
            ]
        }
    )


async def _handle_admin_revoke(request: web.Request) -> web.Response:
    """``POST /admin/agents/{id}/revoke`` -- parity with ``DevCloud.revoke``."""
    _require_admin(request)
    repo = request.app[_REPO_KEY]
    agent_id = request.match_info["agent_id"]
    matched = await repo.set_agent_revoked(agent_id, True)
    if not matched:
        return web.json_response({"error": "unknown agent"}, status=404)
    await repo.deactivate_agent_credentials(agent_id, reason="revoked")
    await repo.append_audit({"kind": "revoke", "agent_id": agent_id})
    return web.json_response({"status": "revoked", "agent_id": agent_id})


async def _handle_admin_set_config(request: web.Request) -> web.Response:
    """``POST /admin/agents/{id}/config`` -- validate cameras, bump ``config_version``."""
    _require_admin(request)
    repo = request.app[_REPO_KEY]
    agent_id = request.match_info["agent_id"]
    body = await _read_json(request)
    raw_cameras = body.get("cameras", [])
    if not isinstance(raw_cameras, list):
        return web.json_response({"error": "'cameras' must be a list"}, status=400)
    try:
        cameras = [
            CameraSyncEntry.model_validate(entry).model_dump(mode="json") for entry in raw_cameras
        ]
    except ValidationError as exc:
        return web.json_response({"error": "invalid camera entry", "detail": str(exc)}, status=400)
    fleet = await repo.bump_fleet_config(agent_id, cameras)
    await repo.append_audit(
        {"kind": "config_updated", "agent_id": agent_id, "config_version": fleet.config_version}
    )
    return web.json_response(
        {"agent_id": agent_id, "config_version": fleet.config_version, "cameras": fleet.cameras}
    )


async def _handle_admin_manifest(request: web.Request) -> web.Response:
    """``POST /admin/manifest`` -- validate body as ``UpdateManifest`` and publish it."""
    _require_admin(request)
    repo = request.app[_REPO_KEY]
    settings = request.app[_SETTINGS_KEY]
    body = await _read_json(request)
    try:
        manifest = UpdateManifest.model_validate(body)
    except ValidationError as exc:
        return web.json_response(
            {"error": "invalid update manifest", "detail": str(exc)}, status=400
        )
    dump = manifest.model_dump(mode="json")
    await repo.set_update_manifest(settings.update_channel, dump)
    await repo.append_audit(
        {
            "kind": "manifest_published",
            "channel": settings.update_channel,
            "version": manifest.version,
        }
    )
    return web.json_response({"status": "published", "channel": settings.update_channel, **dump})


async def _handle_admin_queue_command(request: web.Request) -> web.Response:
    """``POST /admin/commands/{id}`` -- validate body as ``PendingCommand`` and queue it."""
    _require_admin(request)
    repo = request.app[_REPO_KEY]
    agent_id = request.match_info["agent_id"]
    body = await _read_json(request)
    try:
        command = PendingCommand.model_validate(body)
    except ValidationError as exc:
        return web.json_response({"error": "invalid command", "detail": str(exc)}, status=400)
    await repo.queue_command(agent_id, kind=command.kind, payload=command.payload)
    await repo.append_audit(
        {"kind": "command_queued", "agent_id": agent_id, "command_kind": command.kind}
    )
    return web.json_response({"status": "queued", "agent_id": agent_id, "kind": command.kind})


# -- app assembly ---------------------------------------------------------


def build_app(repo: Repo, settings: StagingSettings) -> web.Application:
    """Wire every route onto a fresh :class:`aiohttp.web.Application`.

    Args:
        repo: The persistence backend (``MemoryRepo`` or ``MongoRepo``).
        settings: The resolved :class:`StagingSettings`.

    Returns:
        An app with ``repo`` / ``settings`` stashed under typed keys and all
        routes registered. It has bound no socket -- that is ``server.run``'s
        job (or the test server's).
    """
    app = web.Application(client_max_size=32 * 1024 * 1024)
    app[_REPO_KEY] = repo
    app[_SETTINGS_KEY] = settings
    # The central operator UI (staging_cloud.ui) reads repo/settings under its own
    # AppKeys (aiohttp AppKeys compare by identity); point them at the same objects.
    app[UI_REPO_KEY] = repo
    app[UI_SETTINGS_KEY] = settings
    app[_VIDEO_KEY] = VideoSegmentStore(
        max_segments_per_camera=settings.video_max_segments_per_camera
    )

    # -- contract parity --
    app.router.add_post("/v1/agents/enroll", _handle_enroll)
    app.router.add_post("/v1/agents/{agent_id}/rotate-credential", _handle_rotate)
    app.router.add_post("/v1/agents/{agent_id}/heartbeat", _handle_heartbeat)
    app.router.add_get("/v1/agents/{agent_id}/config", _handle_config)
    app.router.add_get("/v1/agents/{agent_id}/update-manifest", _handle_update_manifest)

    # -- live video test receiver (real; shared data-plane auth) --
    app.router.add_post("/v1/ingest/video/{site_id}/{camera_id}", _handle_ingest_video)
    app.router.add_get("/video", _handle_video_index)
    app.router.add_get("/video/status", _handle_video_status)
    app.router.add_get("/video/{site_id}/{camera_id}", _handle_video_player)
    app.router.add_get("/video/{site_id}/{camera_id}/index.m3u8", _handle_video_playlist)
    app.router.add_get(r"/video/{site_id}/{camera_id}/seg/{seq:\d+}.ts", _handle_video_segment)

    # -- registered but 501 --
    app.router.add_post("/v1/whip/{site_id}/{camera_id}", _handle_not_implemented)
    app.router.add_get("/v1/context/{camera_id}", _handle_not_implemented)
    app.router.add_post("/v1/alerts", _handle_not_implemented)

    # -- staging-only --
    app.router.add_get("/healthz", _handle_healthz)
    app.router.add_get("/", _handle_dashboard)
    # Central operator UI: /admin landing page + /admin/login|logout + /admin/ui/*
    # JSON helpers. Layered on the APIs below; changes none of them.
    register_ui_routes(app)
    app.router.add_get("/admin/state", _handle_admin_state)
    app.router.add_post("/admin/pairing-codes", _handle_admin_mint_codes)
    app.router.add_get("/admin/pairing-codes", _handle_admin_list_codes)
    app.router.add_post("/admin/agents/{agent_id}/revoke", _handle_admin_revoke)
    app.router.add_post("/admin/agents/{agent_id}/config", _handle_admin_set_config)
    app.router.add_post("/admin/manifest", _handle_admin_manifest)
    app.router.add_post("/admin/commands/{agent_id}", _handle_admin_queue_command)

    return app
