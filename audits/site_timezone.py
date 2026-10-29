"""The timezone a site's devices should keep: what their clocks are checked against.

Order:
1. The agency list, for states split between two zones (Florida, Tennessee).
2. The installation's GPS (timezonefinder), only when the zone is valid for the
   state: some coordinates are wrong. In 2026-10 GPS put 4 Iowa sites in
   Eastern, 3 Louisiana sites in Eastern and 1 Georgia site in Central.
3. The state's zone.

Returns (IANA zone, how it was decided). An unknown state falls back to Eastern
and says so.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

EASTERN, CENTRAL, MOUNTAIN, PACIFIC = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
)

# Fleet states → (default zone, zones GPS may pick).
STATE_ZONES: dict[str, tuple[str, frozenset[str]]] = {
    "California": (PACIFIC, frozenset({PACIFIC})),
    "Connecticut": (EASTERN, frozenset({EASTERN})),
    "Florida": (EASTERN, frozenset({EASTERN, CENTRAL})),
    "Georgia": (EASTERN, frozenset({EASTERN})),
    "Iowa": (CENTRAL, frozenset({CENTRAL})),
    "Louisiana": (CENTRAL, frozenset({CENTRAL})),
    "Ohio": (EASTERN, frozenset({EASTERN})),
    "Rhode Island": (EASTERN, frozenset({EASTERN})),
    "Tennessee": (EASTERN, frozenset({EASTERN, CENTRAL})),
    "Virginia": (EASTERN, frozenset({EASTERN})),
}

# Agencies in split states (IMS `agency` is the town or county), checked against
# GPS 2026-10-29. FL's panhandle west of the Apalachicola and Middle/West TN are Central.
AGENCY_ZONES: dict[tuple[str, str], str] = {
    ("Florida", "Blountstown"): CENTRAL,  # Calhoun County
    ("Florida", "Gadsden County"): EASTERN,
    ("Florida", "Hallandale Beach"): EASTERN,
    ("Florida", "Holmes Beach"): EASTERN,
    ("Florida", "Hollywood"): EASTERN,
    ("Florida", "Pembroke Park"): EASTERN,
    ("Florida", "Shalimar"): CENTRAL,  # Okaloosa County
    ("Florida", "Sneads"): CENTRAL,  # Jackson County
    ("Florida", "Sunrise"): EASTERN,
    ("Florida", "Wauchula"): EASTERN,
    ("Tennessee", "Burns"): CENTRAL,  # Dickson County
    ("Tennessee", "Charleston"): EASTERN,  # Bradley County
    ("Tennessee", "Estill Springs"): CENTRAL,  # Franklin County
    ("Tennessee", "Knoxville"): EASTERN,
    ("Tennessee", "Smithville TN"): CENTRAL,  # DeKalb County
    ("Tennessee", "Soddy Daisy"): EASTERN,  # Hamilton County
    ("Tennessee", "Tusculum"): EASTERN,  # Greene County
    ("Tennessee", "Woodbury"): CENTRAL,  # Cannon County
}

_finder = None
_finder_lock = threading.Lock()


def _gps_zone(lat, lon) -> str | None:
    global _finder
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not lat or not lon:
        return None
    try:
        with _finder_lock:
            if _finder is None:
                from timezonefinder import TimezoneFinder

                _finder = TimezoneFinder()
            return _finder.timezone_at(lat=lat, lng=lon)
    except Exception:  # noqa: BLE001 — GPS is a hint; the state list still answers
        logger.warning("[TZ] GPS lookup failed", exc_info=True)
        return None


def site_timezone(inst, *, use_gps: bool = True) -> tuple[str, str]:
    """(IANA zone, reason) for an Installation (or anything with state/agency/gps_*).

    use_gps=False skips the GPS lookup: the review server has no timezonefinder
    and only needs this for runs captured before site_tz was stored.
    """
    state = (getattr(inst, "state", "") or "").strip()
    agency = (getattr(inst, "agency", "") or "").strip()
    if (state, agency) in AGENCY_ZONES:
        return AGENCY_ZONES[(state, agency)], f"agency list ({agency}, {state})"
    default, allowed = STATE_ZONES.get(state, (None, frozenset()))
    gps = _gps_zone(getattr(inst, "gps_lat", None), getattr(inst, "gps_long", None)) if use_gps else None
    if gps and gps in allowed:
        return gps, "GPS"
    if default:
        why = f"state ({state})"
        if gps:
            why += f"; GPS said {gps}, not valid for {state}"
        return default, why
    return EASTERN, f"unknown state {state!r}: assumed Eastern"
