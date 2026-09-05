"""Process entry point: load settings, pick a backend, ensure indexes, serve.

``run()`` is what ``python -m staging_cloud`` calls. It:

1. builds :class:`~staging_cloud.settings.StagingSettings` from the environment
   (a missing required var aborts here with a clear message and a non-zero exit);
2. selects the repo -- :class:`~staging_cloud.memory_repo.MemoryRepo` when
   ``MONGODB_URI=memory://``, otherwise :class:`~staging_cloud.repo.MongoRepo`;
3. calls ``ensure_indexes()`` once (a no-op on the memory backend);
4. serves ``build_app(repo, settings)`` on ``HOST:PORT`` with plain HTTP.

TLS is intentionally not handled here: on Render the platform terminates HTTPS
at the service edge and forwards plain HTTP to ``$PORT``, and the agent's
``require_secure_base_url`` accepts the resulting ``https://<service>.onrender.com``
URL unchanged. For a bare-VM deployment, put a TLS-terminating reverse proxy in
front.
"""

from __future__ import annotations

import logging
import sys

from aiohttp import web
from pydantic import ValidationError

from staging_cloud.api import build_app
from staging_cloud.repo_base import Repo
from staging_cloud.settings import StagingSettings

_logger = logging.getLogger("staging_cloud")


def load_settings() -> StagingSettings:
    """Build :class:`StagingSettings` from the environment, or exit non-zero.

    A :class:`pydantic.ValidationError` (a missing ``MONGODB_URI`` /
    ``STAGING_ADMIN_TOKEN``, a non-integer ``PORT``, ...) is turned into a
    one-line stderr message and ``SystemExit(2)`` -- the operator needs the
    variable name, not a traceback.
    """
    try:
        return StagingSettings()  # type: ignore[call-arg]  # values come from env
    except ValidationError as exc:
        missing = ", ".join(".".join(str(p) for p in err["loc"]) for err in exc.errors())
        print(
            f"staging_cloud: invalid configuration ({missing}); "
            "set the required environment variables (see .env.example)",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


def pick_repo(settings: StagingSettings) -> Repo:
    """Return the backend the settings select. Imports ``motor`` only if needed."""
    if settings.use_memory_backend:
        from staging_cloud.memory_repo import MemoryRepo

        _logger.info("using in-process memory:// backend (no persistence)")
        return MemoryRepo()
    # Imported lazily so the memory-backed pytest suite and local runs never
    # need `motor` / `dnspython` installed.
    from staging_cloud.repo import MongoRepo

    _logger.info("connecting to MongoDB database %s", settings.mongodb_db)
    return MongoRepo(settings.mongodb_uri, settings.mongodb_db)


def run() -> None:
    """Load config, wire the app, and block serving it. The ``python -m`` target."""
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    repo = pick_repo(settings)
    app = build_app(repo, settings)

    async def _startup(_app: web.Application) -> None:
        await repo.ensure_indexes()

    async def _cleanup(_app: web.Application) -> None:
        await repo.close()

    app.on_startup.append(_startup)
    app.on_cleanup.append(_cleanup)

    _logger.info(
        "staging cloud listening on http://%s:%d (env=%s)",
        settings.host,
        settings.port,
        settings.env_name,
    )
    web.run_app(app, host=settings.host, port=settings.port, print=None)


if __name__ == "__main__":
    run()
