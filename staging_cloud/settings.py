"""Runtime configuration for the staging cloud, sourced from environment variables.

:class:`StagingSettings` is a ``pydantic-settings`` model: every field maps to an
uppercase environment variable of the same name (case-insensitive). Two fields
have no default -- ``MONGODB_URI`` and ``STAGING_ADMIN_TOKEN`` -- so a missing
one raises ``ValidationError`` at process start rather than failing later with a
confusing runtime error.

Why instantiated explicitly and passed into ``build_app`` (no module-level
global): the pytest suite builds many apps with different settings in one
process, and a global would leak between them. ``server.run()`` and ``admin.py``
each construct it once.

``.env.example`` documents every variable with placeholder values; a real
``.env`` is git-ignored, and production secrets are injected through Render's
service environment-variable configuration (``sync: false`` in ``render.yaml``),
never committed.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel understood by `server.pick_repo`: select the in-process dict-backed
# repo instead of connecting to Mongo. Used by the pytest suite and local runs.
MEMORY_URI = "memory://"


class StagingSettings(BaseSettings):
    """All staging-cloud tunables. Field name == env var (case-insensitive)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    mongodb_uri: str = Field(
        description="MongoDB connection string, or 'memory://' for the in-process backend"
    )
    mongodb_db: str = Field(
        default="nzerox_staging", description="Database name inside the Mongo deployment"
    )
    host: str = Field(default="0.0.0.0", description="Bind address (Render needs 0.0.0.0)")
    port: int = Field(default=8080, description="Bind port; Render injects $PORT")
    staging_admin_token: str = Field(
        description="Shared secret for every /admin/* route and the dashboard"
    )
    pairing_code_ttl_seconds: int = Field(
        default=3600, gt=0, description="Default lifetime of a minted pairing code"
    )
    credential_ttl_days: int = Field(
        default=30, gt=0, description="Lifetime of an issued / rotated bearer credential"
    )
    heartbeat_history_ttl_days: int = Field(
        default=7, gt=0, description="How long individual heartbeat rows are retained"
    )
    update_channel: str = Field(
        default="default", description="Release channel key for the update_manifest doc"
    )
    video_max_segments_per_camera: int = Field(
        default=20,
        gt=0,
        description=(
            "Most recent MPEG-TS segments kept in memory per camera by the "
            "/v1/ingest/video test receiver (~2 s each). Bounds the footprint; "
            "nothing older is retained."
        ),
    )
    env_name: str = Field(default="staging", description="Free-text environment label for /healthz")
    log_level: str = Field(default="INFO", description="Root logging level")

    @property
    def credential_ttl_seconds(self) -> int:
        """Credential lifetime expressed in seconds for :func:`domain.expiry_after`."""
        return self.credential_ttl_days * 86_400

    @property
    def use_memory_backend(self) -> bool:
        """True when ``MONGODB_URI`` selects the in-process dict-backed repo."""
        return self.mongodb_uri.strip().lower() == MEMORY_URI
