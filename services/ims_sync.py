"""Sync ASE installations from IMS into local SQLite."""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from cameras.models import (
    Installation,
    InstallationComponent,
    InstallationDevice,
    InstallationSensor,
    SyncState,
)
from services.ims_client import fetch_ase_installations

logger = logging.getLogger(__name__)


def _sync_devices(installation: Installation, devices: list[dict]) -> None:
    seen: set[int] = set()
    for raw in devices or []:
        try:
            ims_device_id = int(raw.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not ims_device_id:
            continue
        seen.add(ims_device_id)
        device, _ = InstallationDevice.objects.update_or_create(
            installation=installation,
            ims_device_id=ims_device_id,
            defaults={
                "enclosure_id": int(raw.get("enclosure_id") or 0),
                "enclosure_label": raw.get("enclosure_label") or "",
                "lane_code": raw.get("lane_code") or "",
                "unit_serial": raw.get("unit_serial") or "",
                "prtg_objid": raw.get("prtg_objid") or "",
                "name": raw.get("name") or "",
                "host": raw.get("host") or "",
                "device_type": raw.get("device_type") or "",
                "sort_order": int(raw.get("sort_order") or 0),
            },
        )
        InstallationSensor.objects.filter(device=device).delete()
        sensors = []
        for s in raw.get("sensors") or []:
            sensors.append(
                InstallationSensor(
                    device=device,
                    prtg_objid=s.get("prtg_objid") or "",
                    name=s.get("name") or "",
                    last_value=s.get("last_value") or "",
                    sensor_type=s.get("sensor_type") or "",
                )
            )
        if sensors:
            InstallationSensor.objects.bulk_create(sensors)

    if seen:
        InstallationDevice.objects.filter(installation=installation).exclude(
            ims_device_id__in=seen
        ).delete()
    else:
        InstallationDevice.objects.filter(installation=installation).delete()


def _sync_components(installation: Installation, components: list[dict]) -> None:
    InstallationComponent.objects.filter(installation=installation).delete()
    rows = []
    for i, raw in enumerate(components or []):
        rows.append(
            InstallationComponent(
                installation=installation,
                name=raw.get("name") or "",
                host=raw.get("host") or "",
                sort_order=int(raw.get("sort_order") or i),
            )
        )
    if rows:
        InstallationComponent.objects.bulk_create(rows)


def _inv_str(value) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"n/a", "na", "?", "-"}:
        return ""
    return text


def sync_installations_from_ims() -> tuple[int, int, int]:
    """Upsert IMS ASE installations. Returns (created, updated, deactivated)."""
    logger.info("[SYNC] ASE installation sync starting")
    rows = fetch_ase_installations(include_devices=True)
    logger.info("[SYNC] fetched %s installation row(s) from IMS", len(rows))
    seen_ids: set[int] = set()
    created = 0
    updated = 0
    device_count = 0
    now = timezone.now()

    for row in rows:
        ims_id = int(row["id"])
        seen_ids.add(ims_id)
        platforms = row.get("all_platforms") or []
        if isinstance(platforms, list):
            platforms_csv = ",".join(str(p) for p in platforms)
        else:
            platforms_csv = str(platforms)

        inv = row.get("inventory") or {}
        if not isinstance(inv, dict):
            inv = {}
        defaults = {
            "identifier": row.get("identifier") or "",
            "primary_platform": row.get("primary_platform") or "",
            "all_platforms": platforms_csv,
            "pole_number": str(row.get("pole_number") or ""),
            "serial_number": row.get("serial_number") or "",
            "fl_number": row.get("fl_number") or "",
            "ip_address": row.get("ip_address") or "",
            "state": row.get("state") or "",
            "agency": row.get("agency") or "",
            "location": row.get("location") or "",
            "status": row.get("status") or "",
            "gps_lat": row.get("gps_lat"),
            "gps_long": row.get("gps_lon"),
            "vendor": row.get("vendor") or "",
            "model": row.get("model") or "",
            "camera_a": _inv_str(inv.get("camera_a")),
            "camera_b": _inv_str(inv.get("camera_b")),
            "camera_c": _inv_str(inv.get("camera_c")),
            "tf_a_ip": _inv_str(inv.get("tf_a_ip")),
            "tf_b_ip": _inv_str(inv.get("tf_b_ip")),
            "ims_updated_at": row.get("updated_at") or "",
            "last_synced_at": now,
            "is_active": True,
        }
        devices = row.get("devices") or []
        device_count += len(devices)
        with transaction.atomic():
            installation, was_created = Installation.objects.update_or_create(
                ims_id=ims_id,
                defaults=defaults,
            )
            _sync_devices(installation, devices)
            _sync_components(installation, row.get("components") or [])
        if was_created:
            created += 1
        else:
            updated += 1

    deactivated = 0
    if seen_ids:
        qs = Installation.objects.filter(is_active=True).exclude(ims_id__in=seen_ids)
        deactivated = qs.update(is_active=False)

    SyncState.objects.update_or_create(
        key="last_ims_sync",
        defaults={"value": now.isoformat()},
    )
    SyncState.objects.update_or_create(
        key="last_ims_sync_counts",
        defaults={"value": f"created={created},updated={updated},deactivated={deactivated}"},
    )
    logger.info(
        "[SYNC] ASE sync complete created=%s updated=%s deactivated=%s devices=%s",
        created,
        updated,
        deactivated,
        device_count,
    )
    return created, updated, deactivated
