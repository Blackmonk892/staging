"""Vendored copies of the NZeroC agent<->cloud wire contracts.

This standalone staging cloud speaks the *exact* HTTP contract the NZeroC
Windows agent already uses, but must build and run with no access to the NZeroC
source tree. So the small set of Pydantic / dataclass models the request
handlers validate against are copied here, byte-compatible with their upstream
originals:

===============================  ============================================
this module                      upstream original (NZeroC repo)
===============================  ============================================
``_base``                        ``nzerox.agent.contracts._base``
``enrollment``                   ``nzerox.agent.contracts.enrollment``
``heartbeat`` (``PendingCommand``)  ``nzerox.agent.contracts.heartbeat``
``updates``                      ``nzerox.agent.contracts.updates``
``camera_sync``                  ``nzerox.agent.contracts.camera_sync``
``_streaming``                   ``nzerox.agent.streaming.contracts`` +
                                 ``nzerox.config.schema.StreamProfile``
``video_segment``                ``nzerox.contracts.video_segment``
===============================  ============================================

Only the field shapes / validators the staging cloud actually exercises are
kept; parts of the upstream heartbeat contract that the staging cloud parses
leniently (``HeartbeatRequest`` and friends) are deliberately *not* vendored.
If the NZeroC wire contract changes, update these files to match.
"""

from __future__ import annotations
