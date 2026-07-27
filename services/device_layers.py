"""Build per-IP device layers for an installation (audit IPs + IMS enrichment)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from cameras.models import Installation, InstallationDevice


@dataclass
class SensorChip:
    name: str
    last_value: str


@dataclass
class DeviceLayer:
    key: str
    label: str
    ip: str | None
    thumb_key: str | None = None  # cbw | vnc_l1 | vnc_l2 | None
    device: InstallationDevice | None = None
    sensors: list[SensorChip] = field(default_factory=list)
    matched: bool = False
    ds_number: str | None = None
    display_name: str | None = None


def _norm_ip(value: str | None) -> str:
    text = (value or "").strip()
    if not text or text.lower() in {"n/a", "na", "?", "-"}:
        return ""
    return text


def _sensor_chips(device: InstallationDevice | None, limit: int = 4) -> list[SensorChip]:
    if device is None:
        return []
    chips: list[SensorChip] = []
    for s in device.sensors.all()[:limit]:
        chips.append(SensorChip(name=s.name or "sensor", last_value=s.last_value or "—"))
    return chips


def _match_by_host(devices: list[InstallationDevice], ip: str | None) -> InstallationDevice | None:
    needle = _norm_ip(ip)
    if not needle:
        return None
    for d in devices:
        if _norm_ip(d.host) == needle:
            return d
    return None


def _match_by_name(devices: list[InstallationDevice], *needles: str) -> InstallationDevice | None:
    needles_l = [n.lower() for n in needles if n]
    if not needles_l:
        return None
    for d in devices:
        blob = f"{d.name or ''} {d.device_type or ''}".lower()
        if all(n in blob for n in needles_l):
            return d
    return None


def _first_serial_part(raw: str | None) -> str | None:
    """Extract first DS/serial token from camera_a/b (split on | or /)."""
    text = (raw or "").strip()
    if not text or text.lower() in {"n/a", "na", "?"}:
        return None
    part = re.split(r"[|/]", text, maxsplit=1)[0].strip()
    return part or None


def _unit_serial_parts(raw: str | None) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    return [p.strip() for p in text.split(",") if p.strip()]


def _ds_for_lane(
    lane: int,
    camera_field: str | None,
    device: InstallationDevice | None,
) -> str | None:
    """Prefer inventory camera_* serial; else enclosure unit_serial comma groups."""
    from_cam = _first_serial_part(camera_field)
    if from_cam:
        return from_cam
    if device is None:
        return None
    parts = _unit_serial_parts(device.unit_serial)
    idx = 0 if lane == 1 else 1
    if idx < len(parts):
        return _first_serial_part(parts[idx]) or parts[idx]
    if len(parts) == 1 and lane == 1:
        return _first_serial_part(parts[0]) or parts[0]
    return None


def _display_name(ds_number: str | None, device: InstallationDevice | None) -> str | None:
    if ds_number:
        return ds_number
    if device is not None:
        return (device.name or device.device_type or "").strip() or None
    return None


def build_device_layers(installation: Installation) -> list[DeviceLayer]:
    """Return CBW/VNC layers (LTI) or TeamViewer lanes (DragonEye)."""
    if installation.is_dragoneye:
        return _build_dragoneye_layers(installation)
    return _build_lti_layers(installation)


def _build_dragoneye_layers(installation: Installation) -> list[DeviceLayer]:
    from services.dragoneye_ids import mappings_for_fx

    devices = list(installation.devices.all())
    used: set[int] = set()
    layers: list[DeviceLayer] = []
    fx = installation.fx_number
    mappings = mappings_for_fx(fx) if fx else []

    def add_layer(
        key: str,
        label: str,
        ip: str | None,
        thumb_key: str | None,
        device: InstallationDevice | None,
        *,
        ds_number: str | None = None,
        display_name: str | None = None,
    ) -> None:
        if device:
            used.add(device.pk)
        layers.append(
            DeviceLayer(
                key=key,
                label=label,
                ip=(ip or "").strip() or None,
                thumb_key=thumb_key,
                device=device,
                sensors=_sensor_chips(device),
                matched=device is not None or bool(ds_number) or bool(thumb_key),
                ds_number=ds_number,
                display_name=display_name or _display_name(ds_number, device),
            )
        )

    if mappings:
        for m in mappings:
            lane_bit = f" {m.lane}" if m.lane else ""
            add_layer(
                key=f"de_{m.lane.lower() or 'tv'}",
                label=f"TeamViewer{lane_bit}",
                ip=m.teamviewer_id,
                thumb_key=m.thumb_key,
                device=None,
                ds_number=fx,
                display_name=m.label or fx,
            )
    else:
        add_layer(
            key="de_tv",
            label="TeamViewer",
            ip=None,
            thumb_key="de_tv",
            device=None,
            ds_number=fx,
            display_name=fx or "Upload DragonEye CSV",
        )

    for d in devices:
        if d.pk in used:
            continue
        add_layer(
            f"dev_{d.pk}",
            d.name or d.device_type or "Device",
            _norm_ip(d.host) or None,
            None,
            d,
            ds_number=_first_serial_part((d.unit_serial or "").split(",")[0]) or None,
        )
    return layers


def _build_lti_layers(installation: Installation) -> list[DeviceLayer]:
    """Return CBW + present VNC lanes + matched IMS devices (skip empty L2/etc.)."""
    devices = list(installation.devices.all())
    used: set[int] = set()
    layers: list[DeviceLayer] = []

    cbw = installation.derived_cbw_ip
    derived_tf = installation.derived_tf_cpu_ips
    inv_tf1 = _norm_ip(installation.tf_a_ip) or None
    inv_tf2 = _norm_ip(installation.tf_b_ip) or None
    derived_tf1 = derived_tf[0][1] if len(derived_tf) > 0 else None
    derived_tf2 = derived_tf[1][1] if len(derived_tf) > 1 else None

    def add_layer(
        key: str,
        label: str,
        ip: str | None,
        thumb_key: str | None,
        device: InstallationDevice | None,
        *,
        ds_number: str | None = None,
    ) -> None:
        if device:
            used.add(device.pk)
        display = _display_name(ds_number, device)
        layers.append(
            DeviceLayer(
                key=key,
                label=label,
                ip=_norm_ip(ip) or None,
                thumb_key=thumb_key,
                device=device,
                sensors=_sensor_chips(device),
                matched=device is not None or bool(ds_number),
                ds_number=ds_number,
                display_name=display,
            )
        )

    cbw_dev = _match_by_host(devices, cbw) or _match_by_name(devices, "cbw")
    if cbw or cbw_dev:
        add_layer("cbw", "CBW", cbw, "cbw", cbw_dev)

    cam_a = _first_serial_part(installation.camera_a)
    cam_b = _first_serial_part(installation.camera_b)

    tf1_candidate = inv_tf1 or derived_tf1
    tf2_candidate = inv_tf2 or derived_tf2

    l1_dev = _match_by_host(devices, tf1_candidate)
    if l1_dev is None and (cam_a or inv_tf1):
        l1_dev = _match_by_name(devices, "tf", "cpu")

    l2_dev = _match_by_host(devices, tf2_candidate)
    if l2_dev is None and (cam_b or inv_tf2):
        named = _match_by_name(devices, "tf", "cpu")
        if named is not None and (l1_dev is None or named.pk != l1_dev.pk):
            l2_dev = named

    ds1 = _ds_for_lane(1, installation.camera_a, l1_dev)
    ds2 = _ds_for_lane(2, installation.camera_b, l2_dev)
    if ds2 is None and l2_dev is None and l1_dev is not None:
        parts = _unit_serial_parts(l1_dev.unit_serial)
        if len(parts) >= 2:
            ds2 = _first_serial_part(parts[1]) or parts[1]

    lane1_present = bool(ds1 or l1_dev or inv_tf1 or cam_a)
    lane2_present = bool(ds2 or l2_dev or inv_tf2 or cam_b)

    if lane1_present:
        add_layer(
            "vnc_l1",
            "VNC L1",
            inv_tf1 or derived_tf1,
            "vnc_l1",
            l1_dev,
            ds_number=ds1,
        )
    if lane2_present:
        add_layer(
            "vnc_l2",
            "VNC L2",
            inv_tf2 or derived_tf2,
            "vnc_l2",
            l2_dev,
            ds_number=ds2,
        )

    ims_ip = _norm_ip(installation.ip_address) or None
    shown_ips = {_norm_ip(cbw)} if cbw else set()
    if lane1_present:
        shown_ips.add(_norm_ip(inv_tf1 or derived_tf1))
    if lane2_present:
        shown_ips.add(_norm_ip(inv_tf2 or derived_tf2))
    shown_ips.discard("")
    if ims_ip and ims_ip not in shown_ips:
        ims_dev = _match_by_host(devices, ims_ip)
        if ims_dev:
            add_layer("ims_ip", "IMS IP", ims_ip, None, ims_dev)

    for d in devices:
        if d.pk in used:
            continue
        add_layer(
            f"dev_{d.pk}",
            d.name or d.device_type or "Device",
            _norm_ip(d.host) or None,
            None,
            d,
            ds_number=_first_serial_part((d.unit_serial or "").split(",")[0]) or None,
        )

    return layers
