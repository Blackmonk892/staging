"""``python -m staging_cloud.admin`` -- operator CLI that talks to the backend directly.

Everything here can also be done over the ``/admin/*`` HTTP surface, but the CLI
works *before* the service is public (e.g. minting the very first pairing code
straight after pointing it at a fresh Atlas cluster) and is handy in deploy
scripts. It builds :class:`~staging_cloud.settings.StagingSettings` from the same
environment the server uses, selects the same repo, and calls
:class:`~staging_cloud.repo_base.Repo` methods.

Subcommands::

    mint     --site S --tenant T [--ttl SECONDS] [--count N] [--max-uses N] [--note TEXT]
    list                                   open (unused, unexpired) pairing codes
    revoke   --agent AGENT_ID              revoke an agent + kill its credentials
    set-config --agent AGENT_ID --file cameras.json   replace the fleet config
    publish-manifest --file manifest.json  publish the update manifest
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from staging_cloud import domain
from staging_cloud.domain import PairingCode
from staging_cloud.repo_base import Repo
from staging_cloud.server import load_settings, pick_repo
from staging_cloud.settings import StagingSettings


def _load_json_file(path: Path) -> Any:
    """Read and parse a JSON file, or exit non-zero with a clear message."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"staging_cloud.admin: cannot read {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


async def _mint(repo: Repo, settings: StagingSettings, args: argparse.Namespace) -> int:
    """Mint ``--count`` pairing codes for a site and print them one per line."""
    now = domain.utcnow()
    ttl = int(args.ttl or settings.pairing_code_ttl_seconds)
    expires_at = domain.expiry_after(ttl, now=now)
    for _ in range(max(1, int(args.count))):
        code = domain.mint_pairing_code()
        await repo.insert_pairing_code(
            PairingCode(
                code=code,
                tenant_id=args.tenant,
                site_id=args.site,
                expires_at=expires_at,
                max_uses=max(1, int(args.max_uses)),
                use_count=0,
                used=False,
                note=args.note,
            )
        )
        print(code)
    await repo.append_audit(
        {"kind": "pairing_code_minted", "site_id": args.site, "tenant_id": args.tenant}
    )
    return 0


async def _list(repo: Repo, _settings: StagingSettings, _args: argparse.Namespace) -> int:
    """Print every open pairing code with its site and expiry."""
    codes = await repo.list_open_pairing_codes(now=domain.utcnow())
    if not codes:
        print("(no open pairing codes)")
        return 0
    for c in codes:
        uses = f"uses {c.use_count}/{c.max_uses}"
        print(f"{c.code}\t{c.tenant_id}/{c.site_id}\t{uses}\t{domain.iso(c.expires_at)}")
    return 0


async def _revoke(repo: Repo, _settings: StagingSettings, args: argparse.Namespace) -> int:
    """Revoke one agent and deactivate its credentials."""
    matched = await repo.set_agent_revoked(args.agent, True)
    if not matched:
        print(f"unknown agent {args.agent}", file=sys.stderr)
        return 1
    await repo.deactivate_agent_credentials(args.agent, reason="revoked")
    await repo.append_audit({"kind": "revoke", "agent_id": args.agent})
    print(f"revoked {args.agent}")
    return 0


async def _set_config(repo: Repo, _settings: StagingSettings, args: argparse.Namespace) -> int:
    """Replace an agent's camera fleet config from a JSON file (a list of entries)."""
    cameras = _load_json_file(Path(args.file))
    if not isinstance(cameras, list):
        print("set-config: the JSON file must contain a list of camera entries", file=sys.stderr)
        return 2
    fleet = await repo.bump_fleet_config(args.agent, cameras)
    await repo.append_audit(
        {"kind": "config_updated", "agent_id": args.agent, "config_version": fleet.config_version}
    )
    print(f"config for {args.agent} is now version {fleet.config_version}")
    return 0


async def _publish_manifest(repo: Repo, settings: StagingSettings, args: argparse.Namespace) -> int:
    """Publish the update manifest for the configured channel from a JSON file."""
    manifest = _load_json_file(Path(args.file))
    if not isinstance(manifest, dict):
        print("publish-manifest: the JSON file must contain an object", file=sys.stderr)
        return 2
    await repo.set_update_manifest(settings.update_channel, manifest)
    await repo.append_audit({"kind": "manifest_published", "channel": settings.update_channel})
    print(f"published manifest on channel {settings.update_channel}")
    return 0


_COMMANDS = {
    "mint": _mint,
    "list": _list,
    "revoke": _revoke,
    "set-config": _set_config,
    "publish-manifest": _publish_manifest,
}


def _build_parser() -> argparse.ArgumentParser:
    """Assemble the argparse tree for the five subcommands."""
    parser = argparse.ArgumentParser(
        prog="python -m staging_cloud.admin",
        description="Operator CLI for the staging cloud (talks to the backend directly).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_mint = sub.add_parser("mint", help="mint one or more single-use pairing codes")
    p_mint.add_argument("--site", required=True)
    p_mint.add_argument("--tenant", required=True)
    p_mint.add_argument("--ttl", type=int, default=None, help="lifetime in seconds")
    p_mint.add_argument("--count", type=int, default=1)
    p_mint.add_argument("--max-uses", type=int, default=1, dest="max_uses")
    p_mint.add_argument("--note", default=None)

    sub.add_parser("list", help="list open (unused, unexpired) pairing codes")

    p_revoke = sub.add_parser("revoke", help="revoke an agent and its credentials")
    p_revoke.add_argument("--agent", required=True)

    p_cfg = sub.add_parser("set-config", help="replace an agent's camera fleet config")
    p_cfg.add_argument("--agent", required=True)
    p_cfg.add_argument("--file", required=True, help="JSON file: a list of CameraSyncEntry dicts")

    p_man = sub.add_parser("publish-manifest", help="publish the update manifest")
    p_man.add_argument("--file", required=True, help="JSON file: an UpdateManifest object")

    return parser


async def _run(args: argparse.Namespace) -> int:
    """Build settings + repo, dispatch to the chosen subcommand, then close the repo."""
    settings = load_settings()
    repo = pick_repo(settings)
    try:
        await repo.ensure_indexes()
        handler = _COMMANDS[args.command]
        return await handler(repo, settings, args)
    finally:
        await repo.close()


def main() -> int:
    """CLI entry point. Returns a process exit code."""
    args = _build_parser().parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
