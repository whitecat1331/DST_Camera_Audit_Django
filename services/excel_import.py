"""Import camera inventory from Access Excel export."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd


def _cell(value: Any, default: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return default
    if pd.isna(value):
        return default
    return str(value).strip()


def _optional_float(value: Any) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if pd.isna(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if pd.isna(value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pole_number(value: Any) -> str:
    if value is None or pd.isna(value):
        return "111111"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value).strip() or "111111"


def read_camera_rows(excel_path: str | Path) -> list[dict[str, Any]]:
    """Parse Excel into dicts matching Camera model fields. Skips FL number '0'."""
    path = Path(excel_path)
    if not path.exists():
        raise FileNotFoundError(f"Camera Excel not found: {path}")

    df = pd.read_excel(path)
    rows: list[dict[str, Any]] = []

    for raw in df.itertuples():
        fl_number = _cell(raw._2)
        if fl_number == "0":
            continue

        rows.append(
            {
                "serial": _cell(raw._1),
                "fl_number": fl_number,
                "pole_number": _pole_number(raw._3),
                "state": _cell(getattr(raw, "State", "")),
                "agency": _cell(getattr(raw, "Agency", "")),
                "location": _cell(getattr(raw, "Location", "")),
                "status": _cell(getattr(raw, "Status", "")),
                "assigned": _cell(getattr(raw, "Assigned", "")),
                "lane_group": _cell(raw._9),
                "vendor": _cell(getattr(raw, "Vendor", "")),
                "model": _cell(getattr(raw, "Model", "")),
                "model_type": _cell(raw._12),
                "certification_expiry": _cell(raw._13),
                "lane_direction": _cell(raw._14),
                "last_report": _cell(raw._15),
                "priority": _optional_int(getattr(raw, "Priority", None)),
                "tier_by_value": _optional_float(raw._17),
                "daily_revenue": _optional_float(raw._18),
                "school_start_date": _cell(raw._19),
                "address_audit_status": _cell(raw._20),
                "gps_lat": _optional_float(getattr(raw, "GPS_Lat", None)),
                "gps_long": _optional_float(getattr(raw, "GPS_Lon", None)),
                "modem_ip_address": _cell(raw._23),
            }
        )

    return rows


def import_cameras_from_excel(excel_path: str | Path) -> tuple[int, int]:
    """Upsert cameras by serial. Returns (created_count, updated_count)."""
    from cameras.models import Camera

    rows = read_camera_rows(excel_path)
    created = 0
    updated = 0

    for data in rows:
        serial = data["serial"]
        if not serial:
            continue
        _, was_created = Camera.objects.update_or_create(serial=serial, defaults=data)
        if was_created:
            created += 1
        else:
            updated += 1

    return created, updated
