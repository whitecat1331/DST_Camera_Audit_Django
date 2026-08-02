"""VBE Daily Checks export paths and filenames (OneDrive / shared folder layout).

Layout:
  {root}/{YYYY}/{MM MonthName YYYY}/VBE {NNNN}/{M.D.YY} {AM|PM} Check Lane {n}.png

Month folders historically mix "02February 2026" and "02 February 2026";
lookup accepts both, new folders use the spaced form.

IMS often has two rows per truck:
  - canonical site id  I-VBE-0012  (folder → VBE 0012)
  - pole-linked id     I-VBE-255176 (pole number baked into identifier)
Folder names must always use the site number (0012), never the pole.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_VBE_NUM_RE = re.compile(r"VBE[-\s]?(\d+)", re.IGNORECASE)
_CANONICAL_VBE_ID_RE = re.compile(r"^(?:I-)?VBE[-\s]?(\d{1,4})$", re.IGNORECASE)
_LANE_NUM_RE = re.compile(r"L(\d+)", re.IGNORECASE)

# Pole-style ASE ids are 5–6 digits (255170, 266081). Real VBE site numbers stay < 10000.
_MAX_VBE_SITE_NUM = 9999


def daily_checks_root() -> Path:
    raw = (getattr(settings, "VBE_DAILY_CHECKS_ROOT", "") or "").strip()
    if not raw:
        raise ValueError(
            "VBE_DAILY_CHECKS_ROOT is not configured — set it in .env to the "
            "shared 'VBE Daily Checks' folder"
        )
    return Path(raw)


def is_canonical_vbe_identifier(identifier: str | None) -> bool:
    """True for I-VBE-0012 / VBE0012; False for pole-baked I-VBE-255176."""
    m = _CANONICAL_VBE_ID_RE.match((identifier or "").strip())
    if not m:
        return False
    return int(m.group(1)) <= _MAX_VBE_SITE_NUM


def _site_nums_from_text(raw: str | None) -> list[int]:
    """Extract VBE numbers, preferring site-style (< 10000) over pole-style."""
    found = [int(m.group(1)) for m in _VBE_NUM_RE.finditer(raw or "")]
    site = [n for n in found if n <= _MAX_VBE_SITE_NUM]
    return site if site else found


def vbe_site_folder_name(
    identifier: str | None,
    *,
    fallback: str = "",
    location: str = "",
) -> str:
    """I-VBE-0012 / VBE0012 / loc 'VBE0012 L1' → 'VBE 0012'.

    Never prefers pole-baked ids like I-VBE-255176 when a real site number is available.
    """
    # Location often has the real site even when identifier is pole-based.
    for raw in (location, identifier, fallback):
        nums = _site_nums_from_text(raw)
        if nums:
            return f"VBE {nums[0]:04d}"
    raise ValueError(f"Cannot derive VBE folder name from {identifier!r}")


def resolve_vbe_export_key(installation) -> str:
    """Return the best identifier string to derive the Daily Checks folder from."""
    ident = (getattr(installation, "identifier", None) or "").strip()
    location = (getattr(installation, "location", None) or "").strip()
    pole = (getattr(installation, "pole_number", None) or "").strip()

    if is_canonical_vbe_identifier(ident):
        return ident

    # Location like "VBE0012 L1" → use that.
    loc_nums = _site_nums_from_text(location)
    if loc_nums:
        return f"I-VBE-{loc_nums[0]:04d}"

    # Match FX serial(s) to a canonical I-VBE-00NN row.
    try:
        from django.db.models import Q

        from cameras.models import Installation

        fx_list = list(getattr(installation, "fx_numbers", None) or [])
        if fx_list:
            qs = Installation.objects.filter(is_active=True).filter(
                Q(primary_platform__iexact="VBE") | Q(identifier__istartswith="I-VBE-")
            )
            for other in qs.order_by("identifier"):
                if not is_canonical_vbe_identifier(other.identifier):
                    continue
                other_fx = set(other.fx_numbers or [])
                if other_fx.intersection(fx_list):
                    return (other.identifier or "").strip()
    except Exception:  # noqa: BLE001
        logger.exception("[VBE-DAILY] FX→canonical lookup failed for %s", ident)

    # Last resort: still return something so export can try (may raise in folder_name).
    return ident or location or pole


def lane_number(lane: str | None, thumb_key: str | None = None) -> int:
    for raw in (lane, thumb_key):
        m = _LANE_NUM_RE.search(raw or "")
        if m:
            return int(m.group(1))
    return 1


def capture_filename(when: datetime, lane: int) -> str:
    """e.g. '2.25.26 AM Check Lane 1.png' (no zero-padding on M/D)."""
    meridiem = "AM" if when.hour < 12 else "PM"
    return (
        f"{when.month}.{when.day}.{when.year % 100:02d} "
        f"{meridiem} Check Lane {lane}.png"
    )


def _month_folder_candidates(year_dir: Path, when: datetime) -> list[Path]:
    name = _MONTH_NAMES[when.month - 1]
    return [
        year_dir / f"{when.month:02d} {name} {when.year}",
        year_dir / f"{when.month:02d}{name} {when.year}",
    ]


def resolve_month_dir(year_dir: Path, when: datetime) -> Path:
    candidates = _month_folder_candidates(year_dir, when)
    for path in candidates:
        if path.is_dir():
            return path
    preferred = candidates[0]
    preferred.mkdir(parents=True, exist_ok=True)
    logger.info("[VBE-DAILY] created month folder %s", preferred)
    return preferred


def ensure_vbe_capture_dir(
    *,
    identifier: str,
    when: datetime | None = None,
    location: str = "",
) -> Path:
    """Return (and create) .../YYYY/MM Month YYYY/VBE NNNN/."""
    when = when or datetime.now()
    root = daily_checks_root()
    year_dir = root / f"{when.year}"
    year_dir.mkdir(parents=True, exist_ok=True)
    month_dir = resolve_month_dir(year_dir, when)
    site_dir = month_dir / vbe_site_folder_name(identifier, location=location)
    site_dir.mkdir(parents=True, exist_ok=True)
    return site_dir


def export_daily_check_png(
    source: Path,
    *,
    identifier: str,
    lane: str | None = None,
    thumb_key: str | None = None,
    when: datetime | None = None,
    location: str = "",
) -> Path:
    """Copy a captured PNG into the VBE Daily Checks tree. Returns dest path."""
    when = when or datetime.now()
    dest_dir = ensure_vbe_capture_dir(
        identifier=identifier,
        when=when,
        location=location,
    )
    dest = dest_dir / capture_filename(when, lane_number(lane, thumb_key))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    logger.info("[VBE-DAILY] exported %s → %s", source.name, dest)
    return dest
