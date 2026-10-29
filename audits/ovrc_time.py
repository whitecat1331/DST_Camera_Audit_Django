"""Structured OvrC device time for DST audit jobs.

OvrC's dashboard shows the FX's LOCAL DATE AND TIME with the zone the device
believes it is in, e.g. ``Sun, Aug 02, 2026 01:11 AM EDT``. The DST signal is
the **wall clock** the device shows, so drift is measured wall-clock to
wall-clock against America/New_York at the moment of the scrape — a device that
is right in absolute time but labelled ADT still shows the wrong hour.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# UTC offsets (hours) for the abbreviations OvrC prints for North American FX units.
ZONE_OFFSETS_HOURS = {
    "ADT": -3, "AST": -4,
    "EDT": -4, "EST": -5,
    "CDT": -5, "CST": -6,
    "MDT": -6, "MST": -7,
    "PDT": -7, "PST": -8,
    "UTC": 0, "GMT": 0,
}

# AuditJob columns written from one reading (keys of ovrc_fields()).
OVRC_FIELD_NAMES = (
    "ovrc_fx",
    "ovrc_local_time_raw",
    "ovrc_zone",
    "ovrc_device_time",
    "ovrc_captured_at",
    "drift_seconds",
)

_FORMATS = (
    "%a, %b %d, %Y %I:%M %p",  # Sun, Aug 02, 2026 01:11 AM
    "%b %d, %Y, %I:%M %p",  # legacy converted line: Aug 1, 2026, 11:47 PM
    "%b %d, %Y %I:%M %p",
)
_LINE_RE = re.compile(r"^OvrC\s+(?P<fx>FX[\w-]*)\s*:\s*(?P<raw>.+)$", re.IGNORECASE)


def parse_ovrc_local(raw: str) -> tuple[datetime | None, str]:
    """Return (naive wall-clock datetime, zone abbreviation) from an OvrC scrape."""
    text = " ".join((raw or "").split())
    if not text:
        return None, ""
    zone = ""
    head, _, last = text.rpartition(" ")
    if last.isalpha() and last.upper() in ZONE_OFFSETS_HOURS:
        zone, text = last.upper(), head
    text = re.sub(r"\b([ap])\.m\.", lambda m: m.group(1).upper() + "M", text, flags=re.I)
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt), zone
        except ValueError:
            continue
    return None, zone


def ovrc_fields(fx: str, raw: str, captured_at: datetime | None) -> dict:
    """Model field values for one OvrC reading (keys match ``AuditJob`` fields)."""
    wall, zone = parse_ovrc_local(raw)
    device_time = None
    if wall is not None and zone:
        device_time = wall.replace(
            tzinfo=dt_timezone(timedelta(hours=ZONE_OFFSETS_HOURS[zone]))
        )
    drift = None
    if wall is not None and captured_at is not None:
        eastern_wall = captured_at.astimezone(EASTERN).replace(tzinfo=None)
        drift = int((wall - eastern_wall).total_seconds())
    return {
        "ovrc_fx": (fx or "").strip().upper()[:32],
        "ovrc_local_time_raw": " ".join((raw or "").split())[:64],
        "ovrc_zone": zone,
        "ovrc_device_time": device_time,
        "ovrc_captured_at": captured_at,
        "drift_seconds": drift,
    }


def ovrc_fields_from_line(line: str, captured_at: datetime | None) -> dict | None:
    """Parse a stored ``OvrC FX1260: <raw>`` progress line, or None if absent."""
    m = _LINE_RE.match((line or "").strip().split(" · ")[0])
    if not m:
        return None
    return ovrc_fields(m.group("fx"), m.group("raw"), captured_at)
