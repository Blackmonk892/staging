"""Central operator UI for the staging cloud (``GET /admin`` + its JSON helpers).

This is a thin *operator layer* on top of the already-tested control-plane APIs
(``POST /admin/pairing-codes``, ``POST /v1/agents/enroll``, heartbeat, config,
video). It adds **no** new backend behaviour and changes **no** existing API
contract -- every route here either renders HTML or is a small JSON endpoint that
calls the same :mod:`staging_cloud.domain` + :class:`~staging_cloud.repo_base.Repo`
methods the documented admin API already uses.

Pieces:

* :func:`render_login` / :func:`render_admin` -- pure HTML renderers. The admin
  page carries a little vanilla JS that polls :func:`handle_ui_state` every few
  seconds and drives the "generate a code, then watch for the agent" flow.
* :func:`handle_admin` -- ``GET /admin``: the single landing page. Unauthenticated
  visitors get a token form instead of a bare 401.
* :func:`handle_login` / :func:`handle_logout` -- exchange a valid
  ``STAGING_ADMIN_TOKEN`` for an HttpOnly **session cookie** and back. The cookie
  is ``hmac_sha256(admin_token, "staging-admin-session-v1")`` -- it proves the
  holder knew the token once, is verified in constant time, never contains the
  token itself and is never rendered into the page.
* :func:`handle_ui_state` -- ``GET /admin/ui/state``: JSON snapshot for polling
  (agents + derived connected/offline status, open codes, recent enrollments).
* :func:`handle_ui_mint` -- ``POST /admin/ui/pairing-codes``: mint a code with the
  exact same logic as ``POST /admin/pairing-codes``.

Auth for every route here accepts *either* the existing admin-token mechanisms
(``?token=``, ``X-Admin-Token``, bearer) *or* the session cookie. The documented
``/admin/*`` and ``/v1/*`` routes in :mod:`staging_cloud.api` are untouched.
"""

from __future__ import annotations

import hmac
import html
import json
from datetime import datetime
from hashlib import sha256
from typing import Any

from aiohttp import web

from staging_cloud import domain
from staging_cloud.dashboard import collect_snapshot
from staging_cloud.domain import PairingCode
from staging_cloud.repo_base import Repo
from staging_cloud.settings import StagingSettings

# Own app keys (aiohttp AppKeys compare by identity, so we cannot reuse api.py's).
# ``build_app`` stashes ``repo`` / ``settings`` under these too.
UI_REPO_KEY: web.AppKey[Repo] = web.AppKey("ui_repo", Repo)
UI_SETTINGS_KEY: web.AppKey[StagingSettings] = web.AppKey("ui_settings", StagingSettings)

SESSION_COOKIE = "stg_admin"
_SESSION_SALT = b"staging-admin-session-v1"

# An agent whose most recent heartbeat is within this many seconds is "Connected".
CONNECTED_WITHIN_SECONDS = 90


# -- session cookie --------------------------------------------------------


def session_value(admin_token: str) -> str:
    """Opaque session token derived from the admin token (never the token itself)."""
    return hmac.new(admin_token.encode("utf-8"), _SESSION_SALT, sha256).hexdigest()


def _cookie_ok(request: web.Request, settings: StagingSettings) -> bool:
    """True when the request carries a valid admin session cookie."""
    presented = request.cookies.get(SESSION_COOKIE)
    if not presented:
        return False
    return hmac.compare_digest(presented, session_value(settings.staging_admin_token))


def _authed(request: web.Request) -> bool:
    """Accept the existing admin-token mechanisms OR the session cookie."""
    settings = request.app[UI_SETTINGS_KEY]
    token = request.query.get("token") or request.headers.get("X-Admin-Token") or _bearer(request)
    if domain.admin_token_ok(token, settings.staging_admin_token):
        return True
    return _cookie_ok(request, settings)


def _bearer(request: web.Request) -> str | None:
    """Token from an ``Authorization: Bearer <token>`` header, or ``None``."""
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    return header[len(prefix) :] if header.startswith(prefix) else None


def _set_session(response: web.StreamResponse, settings: StagingSettings) -> None:
    """Attach the HttpOnly admin session cookie to ``response``."""
    response.set_cookie(
        SESSION_COOKIE,
        session_value(settings.staging_admin_token),
        httponly=True,
        samesite="Lax",
        secure=True,
        max_age=12 * 3600,
        path="/",
    )


def _require_json_auth(request: web.Request) -> None:
    """Raise ``HTTPUnauthorized`` (JSON body) unless the request is authenticated."""
    if not _authed(request):
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "admin session or token required"}),
            content_type="application/json",
        )


# -- snapshot shaping for the polling UI ---------------------------------


def _age_seconds(iso_ts: str | None, *, now: datetime) -> float | None:
    """Whole seconds since ``iso_ts`` (an ISO-8601 instant), or ``None``."""
    if not iso_ts:
        return None
    try:
        seen = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    return max(0.0, (now - seen).total_seconds())


def _agent_status(age: float | None) -> str:
    """``connected`` / ``offline`` / ``never`` from a last-seen age in seconds."""
    if age is None:
        return "never"
    return "connected" if age <= CONNECTED_WITHIN_SECONDS else "offline"


async def build_ui_state(repo: Repo, settings: StagingSettings) -> dict[str, Any]:
    """The JSON body :func:`handle_ui_state` returns: agents + status + codes + enrollments.

    Built entirely from :func:`staging_cloud.dashboard.collect_snapshot` (read-only)
    plus a derived connected/offline label -- no new persistence surface.
    """
    snapshot = await collect_snapshot(
        repo, env_name=settings.env_name, manifest_channel=settings.update_channel
    )
    now = domain.utcnow()

    agents: list[dict[str, Any]] = []
    for a in snapshot.agents:
        age = _age_seconds(a.get("last_seen_at"), now=now)
        agents.append(
            {
                "agent_id": a["agent_id"],
                "tenant_id": a["tenant_id"],
                "site_id": a["site_id"],
                "machine_id": a.get("machine_id"),
                "status": "revoked" if a.get("revoked") else _agent_status(age),
                "last_seen_at": a.get("last_seen_at"),
                "last_seen_age_seconds": age,
                "agent_version": a.get("agent_version"),
                "camera_count": a.get("camera_count"),
                "config_version": a.get("config_version"),
                "credential_active": a.get("credential_active", False),
            }
        )

    # Enroll events (newest first) -- lets the "waiting for agent" panel bind a
    # freshly minted code to the agent that used it.
    enrollments = [
        {
            "pairing_code": row.get("pairing_code"),
            "agent_id": row.get("agent_id"),
            "site_id": row.get("site_id"),
            "tenant_id": row.get("tenant_id"),
            "at": row.get("at"),
        }
        for row in snapshot.audit
        if row.get("kind") == "enroll"
    ]

    return {
        "env": snapshot.env_name,
        "server_time": domain.iso(now),
        "connected_within_seconds": CONNECTED_WITHIN_SECONDS,
        "agents": agents,
        "pairing_codes": snapshot.pairing_codes,
        "enrollments": enrollments,
    }


# -- JSON endpoints -----------------------------------------------------


async def handle_ui_state(request: web.Request) -> web.Response:
    """``GET /admin/ui/state`` -- polling snapshot for the admin page (cookie or token)."""
    _require_json_auth(request)
    repo = request.app[UI_REPO_KEY]
    settings = request.app[UI_SETTINGS_KEY]
    return web.json_response(await build_ui_state(repo, settings))


async def handle_ui_mint(request: web.Request) -> web.Response:
    """``POST /admin/ui/pairing-codes`` -- mint one pairing code (same logic as the admin API).

    Body: ``{"site_id": str, "tenant_id": str, "ttl_seconds"?: int, "note"?: str}``.
    Returns ``{"code", "expires_at", "site_id", "tenant_id"}``. Never returns any
    credential or secret.
    """
    _require_json_auth(request)
    repo = request.app[UI_REPO_KEY]
    settings = request.app[UI_SETTINGS_KEY]
    try:
        raw = await request.json()
    except (json.JSONDecodeError, ValueError):
        raw = {}
    body = raw if isinstance(raw, dict) else {}

    site_id = str(body.get("site_id", "")).strip()
    tenant_id = str(body.get("tenant_id", "")).strip()
    if not site_id or not tenant_id:
        return web.json_response({"error": "site_id and tenant_id are required"}, status=400)
    ttl_seconds = int(body.get("ttl_seconds") or settings.pairing_code_ttl_seconds)
    note = body.get("note")

    now = domain.utcnow()
    expires_at = domain.expiry_after(ttl_seconds, now=now)
    code = domain.mint_pairing_code()
    await repo.insert_pairing_code(
        PairingCode(
            code=code,
            tenant_id=tenant_id,
            site_id=site_id,
            expires_at=expires_at,
            max_uses=1,
            use_count=0,
            used=False,
            note=str(note) if note is not None else None,
        )
    )
    await repo.append_audit(
        {
            "kind": "pairing_code_minted",
            "site_id": site_id,
            "tenant_id": tenant_id,
            "count": 1,
            "via": "admin-ui",
        }
    )
    return web.json_response(
        {
            "code": code,
            "expires_at": domain.iso(expires_at),
            "ttl_seconds": ttl_seconds,
            "site_id": site_id,
            "tenant_id": tenant_id,
        }
    )


# -- HTML routes ------------------------------------------------------


async def handle_admin(request: web.Request) -> web.StreamResponse:
    """``GET /admin`` -- the central operator page (token form if not signed in).

    ``GET /admin?token=<STAGING_ADMIN_TOKEN>`` signs in and 303-redirects to a
    clean ``/admin`` so the token never lingers in the address bar or history.
    """
    settings = request.app[UI_SETTINGS_KEY]
    query_token = request.query.get("token")
    if query_token is not None:
        if domain.admin_token_ok(query_token, settings.staging_admin_token):
            resp = web.HTTPSeeOther(location="/admin")
            _set_session(resp, settings)
            raise resp
        return web.Response(
            text=render_login(error="That token was not accepted."),
            content_type="text/html",
            status=401,
        )

    if not _authed(request):
        return web.Response(text=render_login(), content_type="text/html", status=401)
    return web.Response(text=render_admin(settings), content_type="text/html")


async def handle_login(request: web.Request) -> web.StreamResponse:
    """``POST /admin/login`` -- form field ``token`` ⇒ set session cookie ⇒ /admin."""
    settings = request.app[UI_SETTINGS_KEY]
    form = await request.post()
    token = str(form.get("token", ""))
    if not domain.admin_token_ok(token, settings.staging_admin_token):
        return web.Response(
            text=render_login(error="That token was not accepted."),
            content_type="text/html",
            status=401,
        )
    resp = web.HTTPSeeOther(location="/admin")
    _set_session(resp, settings)
    raise resp


async def handle_logout(_request: web.Request) -> web.StreamResponse:
    """``POST /admin/logout`` -- clear the session cookie and return to the form."""
    resp = web.HTTPSeeOther(location="/admin")
    resp.del_cookie(SESSION_COOKIE, path="/")
    raise resp


# -- HTML renderers ------------------------------------------------------

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       margin: 0; background: #0f1115; color: #e7eaee; }
a { color: #8ab4f8; }
header { padding: 1rem 1.5rem; border-bottom: 1px solid #262b33;
         display: flex; align-items: baseline; gap: .9rem; flex-wrap: wrap; }
header .brand { font-weight: 700; font-size: 1.15rem; letter-spacing: .02em; }
header .sub { color: #9aa3af; }
.badge { margin-left: auto; background: #3a2d10; color: #f7c948; border: 1px solid #6b551d;
         padding: .1rem .55rem; border-radius: 999px; font-size: .78rem; font-weight: 700;
         letter-spacing: .08em; }
nav { padding: .6rem 1.5rem; border-bottom: 1px solid #262b33; display: flex; gap: 1.1rem;
      flex-wrap: wrap; background: #12151b; }
nav a { text-decoration: none; color: #c7cdd6; font-size: .92rem; }
nav a:hover { color: #fff; }
main { padding: 1.5rem; max-width: 960px; }
section { margin-bottom: 2.2rem; }
h2 { font-size: 1.02rem; margin: 0 0 .7rem; color: #8ab4f8; }
p.lead { color: #b6bcc6; margin: .2rem 0 1rem; }
button { font: inherit; font-weight: 600; cursor: pointer; border-radius: 7px;
         border: 1px solid #3b4657; background: #1f6feb; color: #fff; padding: .55rem 1rem; }
button.secondary { background: #1b1f27; color: #d7dbe0; }
button:disabled { opacity: .55; cursor: default; }
input[type=text] { font: inherit; padding: .5rem .6rem; border-radius: 7px;
                   border: 1px solid #3b4657; background: #12151b; color: #e7eaee; }
label { display: inline-flex; flex-direction: column; gap: .25rem; font-size: .82rem;
        color: #9aa3af; }
.card { background: #12151b; border: 1px solid #262b33; border-radius: 10px; padding: 1.1rem; }
.row { display: flex; gap: .8rem; flex-wrap: wrap; align-items: flex-end; }
.code { font: 700 2rem/1.1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        letter-spacing: .06em; color: #6ee7a0; margin: .6rem 0 .2rem; user-select: all; }
.muted { color: #7f8794; }
.steps { counter-reset: step; list-style: none; padding: 0; margin: 0; }
.steps li { counter-increment: step; margin: .3rem 0; padding-left: 2rem; position: relative; }
.steps li::before { content: counter(step); position: absolute; left: 0; top: 0;
  width: 1.4rem; height: 1.4rem; border-radius: 999px; background: #1f6feb; color: #fff;
  font-size: .8rem; font-weight: 700; display: grid; place-items: center; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #262b33;
         font-size: .9rem; vertical-align: top; }
th { color: #9aa3af; font-weight: 600; }
.pill { padding: .07rem .5rem; border-radius: 999px; font-size: .78rem; font-weight: 600; }
.pill.connected { background: #14351f; color: #6ee7a0; }
.pill.offline { background: #3a1620; color: #ff9db1; }
.pill.never, .pill.revoked { background: #2a2f38; color: #b6bcc6; }
.wait { display: flex; align-items: center; gap: .5rem; color: #f7c948; margin-top: .8rem; }
.dot { width: .7rem; height: .7rem; border-radius: 999px; background: #f7c948;
       animation: pulse 1.2s ease-in-out infinite; }
.ok .dot { background: #6ee7a0; animation: none; }
.ok { color: #6ee7a0; }
@keyframes pulse { 0%,100% { opacity: .35; } 50% { opacity: 1; } }
.login { max-width: 380px; margin: 5rem auto; }
""".strip()


def render_login(*, error: str | None = None) -> str:
    """The token form shown to an unauthenticated ``/admin`` visitor."""
    err = f'<p style="color:#ff9db1">{_esc(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NZeroC Staging Cloud &mdash; sign in</title><style>{_STYLE}</style></head><body>
<div class="login card">
  <div class="brand" style="font-weight:700;font-size:1.15rem">NZeroC</div>
  <div class="sub muted">Staging Cloud &middot; <span class="badge">STAGING</span></div>
  <p class="lead">Enter the staging admin token to continue.</p>
  {err}
  <form method="post" action="/admin/login" class="row">
    <label>Admin token
      <input type="text" name="token" autocomplete="off" autofocus
             style="min-width:15rem" spellcheck="false"></label>
    <button type="submit">Sign in</button>
  </form>
</div>
</body></html>"""


def render_admin(settings: StagingSettings) -> str:
    """The central operator page. All live data arrives via ``/admin/ui/state`` polling."""
    env = _esc(settings.env_name)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NZeroC Staging Cloud</title><style>{_STYLE}</style></head><body>
<header>
  <span class="brand">NZeroC</span>
  <span class="sub">Staging Cloud</span>
  <span class="badge">STAGING</span>
</header>
<nav>
  <a href="#pair">Pair Agent</a>
  <a href="#agents">Agents</a>
  <a href="/video">Cameras / Video</a>
  <a href="/">Raw dashboard</a>
  <form method="post" action="/admin/logout" style="margin-left:auto">
    <button class="secondary" type="submit" style="padding:.3rem .7rem">Sign out</button>
  </form>
</nav>
<main>
  <section id="pair">
    <h2>Pair a New Agent</h2>
    <p class="lead">Generate a code here, hand it to the person installing the NZeroC
      Agent on their laptop, and watch this page for the agent to connect.</p>
    <div class="card">
      <div class="row">
        <label>Site ID
          <input type="text" id="site" value="test-site" spellcheck="false"></label>
        <label>Tenant ID
          <input type="text" id="tenant" value="test-tenant" spellcheck="false"></label>
        <button id="gen" type="button">Generate Pairing Code</button>
      </div>
      <div id="codebox" hidden>
        <div class="code" id="code">------</div>
        <div class="muted">Give this code to the user installing the NZeroC Agent on
          their laptop. Code expires in <span id="expiry">--:--</span>.</div>
        <div id="waiting" class="wait"><span class="dot"></span>
          <span id="waiting-text">Waiting for enrollment&hellip;</span></div>
        <div id="enrolled" hidden></div>
        <div style="margin-top:.8rem">
          <button class="secondary" type="button" id="another">Generate another code</button>
        </div>
      </div>
    </div>
    <ol class="steps" style="margin-top:1rem">
      <li>Generate a pairing code here.</li>
      <li>Give the code to the person installing the NZeroC Agent.</li>
      <li>They enter the code in the NZeroC Agent UI.</li>
      <li>The agent enrolls automatically.</li>
      <li>This page shows the agent when it connects.</li>
    </ol>
  </section>

  <section id="agents">
    <h2>Agents</h2>
    <p class="lead">Live from the control plane &middot; refreshes every 5s &middot;
      <span class="muted">env {env}</span></p>
    <table>
      <thead><tr><th>Agent</th><th>Site</th><th>Status</th><th>Last seen</th>
        <th>Version</th><th>Cams</th><th>Cfg</th></tr></thead>
      <tbody id="agents-body">
        <tr><td colspan="7" class="muted">loading&hellip;</td></tr>
      </tbody>
    </table>
  </section>
</main>
<script>{_ADMIN_JS}</script>
</body></html>"""


_ADMIN_JS = r"""
(function () {
  var POLL_MS = 5000;
  var genBtn = document.getElementById("gen");
  var anotherBtn = document.getElementById("another");
  var codebox = document.getElementById("codebox");
  var codeEl = document.getElementById("code");
  var expiryEl = document.getElementById("expiry");
  var waiting = document.getElementById("waiting");
  var waitingText = document.getElementById("waiting-text");
  var enrolled = document.getElementById("enrolled");
  var agentsBody = document.getElementById("agents-body");

  var active = null;      // { code, expiresAt }
  var expiryTimer = null;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function rel(secs) {
    if (secs == null) return "never";
    secs = Math.round(secs);
    if (secs < 60) return secs + " sec";
    if (secs < 3600) return Math.round(secs / 60) + " min";
    return Math.round(secs / 3600) + " hr";
  }

  function startExpiryCountdown() {
    if (expiryTimer) clearInterval(expiryTimer);
    function tick() {
      if (!active) return;
      var left = Math.max(0, Math.floor((active.expiresAt - Date.now()) / 1000));
      var m = Math.floor(left / 60), s = left % 60;
      expiryEl.textContent = m + ":" + (s < 10 ? "0" : "") + s;
      if (left <= 0) { clearInterval(expiryTimer); expiryTimer = null; }
    }
    tick();
    expiryTimer = setInterval(tick, 1000);
  }

  function mint() {
    genBtn.disabled = true;
    fetch("/admin/ui/pairing-codes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        site_id: document.getElementById("site").value.trim() || "test-site",
        tenant_id: document.getElementById("tenant").value.trim() || "test-tenant"
      })
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        genBtn.disabled = false;
        if (!res.ok) { alert(res.j.error || "could not mint code"); return; }
        active = { code: res.j.code, expiresAt: Date.parse(res.j.expires_at) };
        codeEl.textContent = res.j.code;
        codebox.hidden = false;
        waiting.hidden = false;
        waiting.classList.remove("ok");
        waitingText.textContent = "Waiting for enrollment…";
        enrolled.hidden = true;
        startExpiryCountdown();
        refresh();
      })
      .catch(function () { genBtn.disabled = false; alert("network error"); });
  }

  function renderAgents(state) {
    var rows = state.agents.map(function (a) {
      var cls = a.status === "connected" ? "connected"
              : a.status === "offline" ? "offline"
              : a.status === "revoked" ? "revoked" : "never";
      return "<tr>" +
        "<td title='" + esc(a.agent_id) + "'>" + esc(a.agent_id.slice(0, 8)) + "</td>" +
        "<td>" + esc(a.site_id) + "</td>" +
        "<td><span class='pill " + cls + "'>" + esc(a.status) + "</span></td>" +
        "<td>" + (a.last_seen_age_seconds == null ? "never"
                  : rel(a.last_seen_age_seconds) + " ago") + "</td>" +
        "<td>" + esc(a.agent_version || "-") + "</td>" +
        "<td>" + esc(a.camera_count == null ? "-" : a.camera_count) + "</td>" +
        "<td>" + esc(a.config_version == null ? "-" : a.config_version) + "</td>" +
      "</tr>";
    });
    agentsBody.innerHTML = rows.join("") ||
      "<tr><td colspan='7' class='muted'>no agents enrolled yet</td></tr>";
  }

  function checkEnrollment(state) {
    if (!active) return;
    var stillOpen = state.pairing_codes.some(function (c) { return c.code === active.code; });
    var ev = state.enrollments.find(function (e) { return e.pairing_code === active.code; });
    if (!ev) {
      if (!stillOpen && expiryTimer == null && active.expiresAt < Date.now()) {
        waitingText.textContent = "Code expired before it was used.";
      }
      return;
    }
    var agent = state.agents.find(function (a) { return a.agent_id === ev.agent_id; });
    waiting.classList.add("ok");
    waitingText.textContent = "Agent enrolled";
    var seen = agent && agent.last_seen_age_seconds != null
      ? rel(agent.last_seen_age_seconds) + " ago" : "no heartbeat yet";
    var status = agent ? agent.status : "pending";
    enrolled.hidden = false;
    enrolled.innerHTML =
      "<div class='ok' style='margin-top:.4rem'>✓ Agent enrolled</div>" +
      "<table style='margin-top:.5rem'><tbody>" +
      "<tr><th>Agent</th><td>" + esc(ev.agent_id) + "</td></tr>" +
      "<tr><th>Site</th><td>" + esc(ev.site_id || (agent && agent.site_id) || "-") + "</td></tr>" +
      "<tr><th>Status</th><td><span class='pill " + esc(status) + "'>" + esc(status) +
        "</span></td></tr>" +
      "<tr><th>Last heartbeat</th><td>" + esc(seen) + "</td></tr>" +
      "</tbody></table>";
  }

  function refresh() {
    fetch("/admin/ui/state", { headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (r.status === 401) { location.href = "/admin"; return null; }
        return r.ok ? r.json() : null;
      })
      .then(function (state) {
        if (!state) return;
        renderAgents(state);
        checkEnrollment(state);
      })
      .catch(function () {});
  }

  if (genBtn) genBtn.addEventListener("click", mint);
  if (anotherBtn) anotherBtn.addEventListener("click", mint);
  refresh();
  setInterval(refresh, POLL_MS);
})();
"""


def _esc(value: object) -> str:
    """HTML-escape any value's ``str()`` form."""
    return html.escape(str(value))


def register_ui_routes(app: web.Application) -> None:
    """Attach the central-UI routes. Called from :func:`staging_cloud.api.build_app`."""
    app.router.add_get("/admin", handle_admin)
    app.router.add_post("/admin/login", handle_login)
    app.router.add_post("/admin/logout", handle_logout)
    app.router.add_get("/admin/ui/state", handle_ui_state)
    app.router.add_post("/admin/ui/pairing-codes", handle_ui_mint)
