"""Deployable staging cloud: a real HTTPS control plane backed by a database.

A stand-in for the NZeroC cloud control plane that the NZeroC Windows agent can
enrol against, heartbeat to, pull config/updates from, and stream live
~2-second MPEG-TS video segments to -- speaking the *exact* HTTP contract the
agent already uses, but deployable as its own service (MongoDB Atlas + Render).

This is a **standalone repository**: it has no dependency on the NZeroC source
tree. The handful of agent<->cloud wire models the request handlers validate
against are vendored under :mod:`staging_cloud.contracts` (byte-compatible copies
of the upstream Pydantic / dataclass definitions).

Layout:

- ``domain.py``      -- pure logic: token/code minting, sha256 hashing, expiry
  math, auth predicates, hand-built (contract-checked) response dicts. No I/O.
- ``repo_base.py``   -- the :class:`Repo` typing Protocol every backend satisfies.
- ``memory_repo.py`` -- dict-backed :class:`MemoryRepo` (selected by
  ``MONGODB_URI=memory://``); the pytest suite and local dev run on it.
- ``repo.py``        -- :class:`MongoRepo` over ``motor``; ``ensure_indexes()``.
- ``video_store.py`` -- bounded in-memory ring for the live-video test receiver.
- ``api.py``         -- aiohttp handlers + ``build_app(repo, settings)``.
- ``server.py``      -- ``run()``: load settings, pick a repo, ensure indexes,
  serve. ``python -m staging_cloud``.
- ``dashboard.py``   -- ``collect_snapshot`` + ``render_dashboard`` (no JS).
- ``admin.py``       -- ``python -m staging_cloud.admin`` operator CLI.
- ``settings.py``    -- :class:`StagingSettings` (env vars + defaults).
- ``contracts/``     -- vendored agent<->cloud wire models (no NZeroC import).
"""

from __future__ import annotations

STAGING_API_VERSION = "v1"

__all__ = ["STAGING_API_VERSION"]
