"""Shared field types for the agent <-> cloud Pydantic contracts.

Vendored verbatim from ``nzerox.agent.contracts._base`` (NZeroC repo). Holds
only the small annotated-type primitives that more than one contract file needs
-- a UTC-normalising ``datetime`` and the two string-shaped hash/version fields
carried in the update manifest. Nothing here does I/O or has behaviour.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator

# Official SemVer 2.0.0 grammar (https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string).
# Anchored so a trailing-garbage string like "1.2.3-oops!" is rejected rather
# than partially matched.
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _to_utc(value: datetime) -> datetime:
    """Reject naive datetimes and normalise any aware datetime to UTC.

    Raises ``ValueError`` if the datetime has no timezone, because a wall-clock
    time with no offset is ambiguous the moment it leaves the machine that
    produced it -- every timestamp on the wire is UTC.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("datetime must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


def _check_semver(value: str) -> str:
    """Return ``value`` unchanged iff it is a valid SemVer 2.0.0 string."""
    if not _SEMVER_RE.match(value):
        raise ValueError(f"not a valid SemVer version: {value!r}")
    return value


def _check_sha256(value: str) -> str:
    """Normalise to lower-case hex and verify it is exactly 64 hex chars."""
    lowered = value.lower()
    if not _SHA256_RE.match(lowered):
        raise ValueError("sha256 must be 64 hexadecimal characters")
    return lowered


# A ``datetime`` field that refuses naive values and stores UTC.
UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]

# A ``str`` field constrained to SemVer 2.0.0 syntax.
SemVerStr = Annotated[str, AfterValidator(_check_semver)]

# A ``str`` field constrained to a 64-char lower-case hex SHA-256 digest.
Sha256Str = Annotated[str, AfterValidator(_check_sha256)]
