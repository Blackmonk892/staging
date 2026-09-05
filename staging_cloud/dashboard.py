"""Server-rendered operations view for the staging cloud (``GET /`` + ``/admin/state``).

Two pieces:

* :func:`collect_snapshot` -- an ``async`` read-only gather of everything the
  operator view shows (agents, their last-seen status, open pairing codes, the
  published manifest, a tail of the audit trail) into one plain :class:`Snapshot`.
* :func:`render_dashboard` -- a pure function turning a :class:`Snapshot` into a
  self-contained HTML page: inline CSS, a ``<meta http-equiv="refresh">`` and no
  JavaScript at all. It is a debugging surface, deliberately plain.

``Snapshot.as_json`` is the same data as the machine-readable ``/admin/state``
body, so the HTML view and the JSON view cannot drift.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from staging_cloud import domain

if TYPE_CHECKING:
    from staging_cloud.repo_base import Repo

_REFRESH_SECONDS = 5
_AUDIT_TAIL = 40

_STYLE = """
* { box-sizing: border-box; }
body { font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
       margin: 0; padding: 1.5rem; background: #0f1115; color: #d7dbe0; }
h1 { font-size: 1.1rem; margin: 0 0 .25rem; }
h2 { font-size: .95rem; margin: 1.5rem 0 .5rem; color: #8ab4f8; }
.muted { color: #7f8794; }
table { border-collapse: collapse; width: 100%; margin-bottom: .5rem; }
th, td { text-align: left; padding: .3rem .55rem; border-bottom: 1px solid #262b33;
         vertical-align: top; }
th { color: #9aa3af; font-weight: 600; }
code, pre { background: #171a21; border: 1px solid #262b33; border-radius: 4px; }
code { padding: .05rem .3rem; }
pre { padding: .5rem .65rem; overflow-x: auto; margin: .25rem 0; white-space: pre-wrap; }
.pill { padding: .05rem .4rem; border-radius: 999px; font-size: .8rem; }
.ok { background: #14351f; color: #6ee7a0; }
.bad { background: #3a1620; color: #ff9db1; }
""".strip()


@dataclass(frozen=True)
class Snapshot:
    """A single read-consistent view of the control plane for the operator surfaces."""

    env_name: str
    agents: list[dict[str, Any]]
    pairing_codes: list[dict[str, Any]]
    manifest: dict[str, Any] | None
    audit: list[dict[str, Any]]

    def as_json(self) -> dict[str, Any]:
        """The exact body ``GET /admin/state`` returns (JSON-serialisable)."""
        return {
            "env": self.env_name,
            "agents": self.agents,
            "pairing_codes": self.pairing_codes,
            "update_manifest": self.manifest,
            "audit": self.audit,
        }


async def collect_snapshot(
    repo: Repo, *, env_name: str = "staging", manifest_channel: str = "default"
) -> Snapshot:
    """Gather agents + statuses + open codes + manifest + audit tail into a :class:`Snapshot`.

    Read-only: issues only ``get_*`` / ``list_*`` calls. Side effects: none.
    """
    now = domain.utcnow()
    agents_out: list[dict[str, Any]] = []
    for agent in await repo.list_agents():
        status = await repo.get_agent_status(agent.id)
        credential = await repo.get_active_credential(agent.id)
        fleet = await repo.get_fleet_config(agent.id)
        agents_out.append(
            {
                "agent_id": agent.id,
                "tenant_id": agent.tenant_id,
                "site_id": agent.site_id,
                "machine_id": agent.machine_id,
                "enrolled_at": domain.iso(agent.enrolled_at),
                "revoked": agent.revoked,
                "credential_active": credential is not None,
                "credential_expired": bool(credential and credential.expired),
                "config_version": fleet.config_version if fleet is not None else 0,
                "last_seen_at": (status or {}).get("last_seen_at"),
                "agent_version": (status or {}).get("agent_version"),
                "camera_count": (status or {}).get("camera_count"),
            }
        )

    codes = [
        {
            "code": c.code,
            "tenant_id": c.tenant_id,
            "site_id": c.site_id,
            "expires_at": domain.iso(c.expires_at),
            "max_uses": c.max_uses,
            "use_count": c.use_count,
            "note": c.note,
        }
        for c in await repo.list_open_pairing_codes(now=now)
    ]
    manifest = await repo.get_update_manifest(manifest_channel)
    audit = await repo.recent_audit(_AUDIT_TAIL)
    return Snapshot(
        env_name=env_name,
        agents=agents_out,
        pairing_codes=codes,
        manifest=manifest,
        audit=[_jsonify_audit(row) for row in audit],
    )


def _jsonify_audit(row: dict[str, Any]) -> dict[str, Any]:
    """Render an audit row's ``at`` datetime to ISO so it is JSON-serialisable."""
    out = dict(row)
    at = out.get("at")
    if isinstance(at, datetime):
        out["at"] = at.isoformat()
    return out


def _esc(value: object) -> str:
    """HTML-escape any value's ``str()`` form."""
    return html.escape(str(value))


def _pill(text: str, *, good: bool) -> str:
    """A coloured status pill span."""
    return f'<span class="pill {"ok" if good else "bad"}">{_esc(text)}</span>'


def _agent_row(agent: dict[str, Any]) -> str:
    """One ``<tr>`` for the agents table."""
    if agent["revoked"]:
        state = _pill("REVOKED", good=False)
    elif agent["credential_expired"]:
        state = _pill("CRED EXPIRED", good=False)
    elif not agent["credential_active"]:
        state = _pill("NO CRED", good=False)
    else:
        state = _pill("OK", good=True)
    cam_count = agent.get("camera_count")
    return (
        "<tr>"
        f"<td>{_esc(agent['agent_id'])}</td>"
        f"<td>{state}</td>"
        f"<td>{_esc(agent['tenant_id'])} / {_esc(agent['site_id'])}</td>"
        f"<td>{_esc(agent.get('agent_version') or '-')}</td>"
        f"<td>{_esc(agent['config_version'])}</td>"
        f"<td>{_esc(cam_count if cam_count is not None else '-')}</td>"
        f"<td>{_esc(agent.get('last_seen_at') or 'never')}</td>"
        "</tr>"
    )


def render_dashboard(snapshot: Snapshot) -> str:
    """Turn a :class:`Snapshot` into a complete, self-contained HTML page."""
    if snapshot.agents:
        agent_rows = "".join(_agent_row(a) for a in snapshot.agents)
    else:
        agent_rows = '<tr><td colspan="7" class="muted">no agents enrolled yet</td></tr>'

    if snapshot.pairing_codes:
        code_rows = "".join(
            "<tr>"
            f"<td>{_esc(c['code'])}</td>"
            f"<td>{_esc(c['tenant_id'])} / {_esc(c['site_id'])}</td>"
            f"<td>{_esc(c['use_count'])}/{_esc(c['max_uses'])}</td>"
            f"<td>{_esc(c['expires_at'])}</td>"
            "</tr>"
            for c in snapshot.pairing_codes
        )
    else:
        code_rows = '<tr><td colspan="4" class="muted">no open pairing codes</td></tr>'

    manifest = snapshot.manifest
    manifest_block = (
        f"<pre>{_esc(json.dumps(manifest, indent=2))}</pre>"
        if manifest is not None
        else '<span class="muted">not published (route 404s)</span>'
    )

    if snapshot.audit:
        audit_rows = "".join(
            f"<tr><td>{_esc(r.get('at', ''))}</td><td>{_esc(r.get('kind', ''))}</td>"
            f"<td>{_esc(r.get('agent_id', ''))}</td></tr>"
            for r in snapshot.audit
        )
    else:
        audit_rows = '<tr><td colspan="3" class="muted">no events yet</td></tr>'

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{_REFRESH_SECONDS}">
<title>nzerox staging cloud</title><style>{_STYLE}</style></head><body>
<h1>nzerox staging cloud</h1>
<div class="muted">env {_esc(snapshot.env_name)} &middot; auto-refresh {_REFRESH_SECONDS}s &middot;
  {len(snapshot.agents)} agent(s) &middot; {len(snapshot.pairing_codes)} open code(s)</div>

<h2>Agents</h2>
<table>
<tr><th>agent_id</th><th>state</th><th>tenant / site</th><th>version</th>
    <th>cfg&nbsp;v</th><th>cams</th><th>last seen</th></tr>
{agent_rows}
</table>

<h2>Open pairing codes</h2>
<table>
<tr><th>code</th><th>tenant / site</th><th>uses</th><th>expires</th></tr>
{code_rows}
</table>

<h2>Update manifest</h2>
{manifest_block}

<h2>Audit trail (newest first)</h2>
<table><tr><th>at</th><th>kind</th><th>agent_id</th></tr>{audit_rows}</table>
</body></html>"""
