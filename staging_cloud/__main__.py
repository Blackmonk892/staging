"""``python -m staging_cloud`` -- start the staging control-plane HTTP server.

All configuration is environment-driven (see :class:`staging_cloud.settings.StagingSettings`
and ``.env.example``); there are no command-line flags, matching how the Docker
image and Render invoke it.
"""

from __future__ import annotations

from staging_cloud.server import run

if __name__ == "__main__":
    run()
