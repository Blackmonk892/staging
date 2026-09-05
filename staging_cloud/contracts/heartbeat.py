"""Heartbeat contracts -- the subset the staging cloud validates against.

Vendored from ``nzerox.agent.contracts.heartbeat`` (NZeroC repo). The staging
cloud parses ``POST /v1/agents/{id}/heartbeat`` request bodies *leniently*
(stored verbatim, a few fields read with ``.get()``), so ``HeartbeatRequest``
and its nested models are intentionally **not** vendored -- only
``PendingCommand``, which ``POST /admin/commands/{id}`` validates before queuing.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PendingCommand(BaseModel):
    """One out-of-band action the cloud wants the agent to perform.

    ``payload`` is intentionally an open ``dict`` -- its shape depends on
    ``kind`` and is validated by whichever command handler consumes it.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["revoke", "pin_version", "rotate_credential", "force_sync"] = Field(
        description="Which action to perform"
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="kind-specific parameters, validated by the command handler",
    )
