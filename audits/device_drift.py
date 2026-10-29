"""Per-device drift: the device's wall clock against its site's wall clock.

drift = what the device showed − what a correct clock in the site's zone showed
at the moment of capture. A device a DST hour off shows ±3600 s.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from audits.ovrc_time import EASTERN

# Within this is a pass. OvrC shows minutes only (±60 s) and screenshot rows
# are saved a few seconds after the grab, so only clear drift fails.
PASS_SECONDS = 120


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name else EASTERN
    except (ZoneInfoNotFoundError, ValueError):
        return EASTERN


def expected_wall(captured_at: datetime, tz_name: str) -> datetime:
    """The site's wall clock at captured_at (naive)."""
    return captured_at.astimezone(zone(tz_name)).replace(tzinfo=None)


def drift_seconds(device_time_iso: str, captured_at: datetime | None, tz_name: str) -> int | None:
    if not device_time_iso or captured_at is None:
        return None
    try:
        wall = datetime.fromisoformat(device_time_iso)
    except ValueError:
        return None
    return int((wall - expected_wall(captured_at, tz_name)).total_seconds())


def verdict(drift: int | None) -> str:
    """"pass" / "fail", or "unread" when the device clock wasn't read."""
    if drift is None:
        return "unread"
    return "pass" if abs(drift) <= PASS_SECONDS else "fail"
