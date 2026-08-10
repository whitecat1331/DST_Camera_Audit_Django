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
    # DragonEye / VBE: editable TeamViewer mapping (FX + optional L1/L2).
    fx_number: str | None = None
    tv_lane: str | None = None
    editable_tv: bool = False


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


def _lane_token(raw: str | None) -> str | None:
    """Return L1/L2/… lane token from device text.

    Accepts explicit ``L1``/``L2`` (including glued ``FX1074L1``) and DragonEye
    directional CPU names ``FX1403L`` / ``FX1403R`` (mapped to L1 / L2).
    """
    text = (raw or "").strip().upper()
    if not text:
        return None
    m = re.search(r"\b(L\d+)\b", text)
    if m:
        return m.group(1)
    m = re.search(r"FX\d+(L\d+)\b", text)
    if m:
        return m.group(1)
    # FX####L / FX####R glued or spaced (E/W CPUs in PRTG).
    m = re.search(r"\bFX\d+\s*([LR])\b", text)
    if m:
        return "L1" if m.group(1) == "L" else "L2"
    return None


def _lanes_from_devices(devices: list[InstallationDevice], fx: str | None = None) -> list[str]:
    """Distinct L1/L2/… tokens from device names / lane_code for an FX (or all)."""
    found: list[str] = []
    seen: set[str] = set()
    fx_u = (fx or "").upper()
    for d in devices:
        blob = f"{d.name or ''} {d.unit_serial or ''} {d.lane_code or ''}"
        if fx_u and fx_u not in blob.upper():
            continue
        for src in (d.name, d.lane_code, d.unit_serial):
            lane = _lane_token(src)
            if lane and lane not in seen:
                seen.add(lane)
                found.append(lane)
    return sorted(found, key=lambda x: (len(x), x))


def _device_for_fx_lane(
    devices: list[InstallationDevice],
    fx: str,
    lane: str | None = None,
) -> InstallationDevice | None:
    """Prefer FX CPU matching lane; fall back to any device with that FX."""
    needle = (fx or "").upper()
    if not needle:
        return None
    lane_u = (lane or "").strip().upper()
    cpu_match: InstallationDevice | None = None
    any_match: InstallationDevice | None = None
    for d in devices:
        blob = f"{d.unit_serial or ''} {d.name or ''}".upper()
        if needle not in blob:
            continue
        name_u = (d.name or "").upper()
        is_cpu = "CPU" in name_u or (
            needle in name_u and "MODEM" not in name_u and "CAMERA" not in name_u
        )
        if lane_u:
            name_lane = _lane_token(d.name)
            # Explicit L# in the device name wins over enclosure lane_code
            # (VBE parents often stamp every child row as L1).
            if name_lane and name_lane != lane_u:
                continue
            lane_in_name = name_lane == lane_u
            lane_on_row = (d.lane_code or "").strip().upper() == lane_u
            if not (lane_in_name or lane_on_row):
                if any_match is None:
                    any_match = d
                continue
            if is_cpu:
                return d
            if cpu_match is None:
                cpu_match = d
            continue
        if is_cpu and cpu_match is None:
            cpu_match = d
        if any_match is None:
            any_match = d
    return cpu_match or any_match


def resolve_de_tv_capture_targets(installation: Installation) -> list[dict[str, str]]:
    """Return ordered TeamViewer capture targets for a DE installation.

    Each item: fx, lane, teamviewer_id, thumb_key, label.

    Legacy unlaned CSV rows (lane="") are bound to the first device lane
    (usually L1 from FX####L) so screenshots save as ``de_l1`` and match the UI.
    """
    from services.dragoneye_ids import mappings_for_fx

    devices = list(installation.devices.all())
    out: list[dict[str, str]] = []
    for fx in installation.fx_numbers:
        mappings = mappings_for_fx(fx)
        by_lane = {(m.lane or "").strip().upper(): m for m in mappings}
        device_lanes = _lanes_from_devices(devices, fx) or _lanes_from_devices(devices)

        if device_lanes:
            lanes = list(device_lanes)
        elif by_lane:
            numbered = sorted([k for k in by_lane if k], key=lambda x: (len(x), x))
            lanes = numbered if numbered else [""]
        else:
            lanes = [""]

        for extra in by_lane:
            if extra and extra not in lanes:
                lanes.append(extra)

        unlaned = by_lane.get("")
        unlaned_consumed = False
        for lane in lanes:
            m = by_lane.get(lane)
            if m is None and unlaned is not None and not unlaned_consumed:
                m = unlaned
                unlaned_consumed = True
            if m is None or not (m.teamviewer_id or "").strip():
                continue
            if lane:
                thumb = f"de_{lane.lower()}"
            else:
                thumb = m.thumb_key
            out.append(
                {
                    "fx": fx,
                    "lane": lane,
                    "teamviewer_id": m.teamviewer_id,
                    "thumb_key": thumb,
                    "label": (m.label or f"{fx} {lane}".strip()).strip(),
                }
            )
    return out


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
    fx_list = installation.fx_numbers

    def add_layer(
        key: str,
        label: str,
        ip: str | None,
        thumb_key: str | None,
        device: InstallationDevice | None,
        *,
        ds_number: str | None = None,
        display_name: str | None = None,
        fx_number: str | None = None,
        tv_lane: str | None = None,
        editable_tv: bool = False,
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
                fx_number=(fx_number or "").strip().upper() or None,
                tv_lane=(tv_lane or "").strip().upper() or None,
                editable_tv=editable_tv,
            )
        )

    if fx_list:
        for fx in fx_list:
            mappings = mappings_for_fx(fx)
            by_lane = {(m.lane or "").strip().upper(): m for m in mappings}
            device_lanes = _lanes_from_devices(devices, fx)
            if not device_lanes:
                device_lanes = _lanes_from_devices(devices)

            # Prefer one editable TV slot per CPU lane (L1/L2 from FX####L/R).
            # Fall back to mapping lanes / a single unlaned slot.
            if device_lanes:
                lanes = list(device_lanes)
            elif by_lane:
                numbered = sorted(
                    [k for k in by_lane if k],
                    key=lambda x: (len(x), x),
                )
                lanes = numbered if numbered else [""]
            else:
                lanes = [""]

            for extra in by_lane:
                if extra and extra not in lanes:
                    lanes.append(extra)

            unlaned = by_lane.get("")
            unlaned_consumed = False
            for lane in lanes:
                m = by_lane.get(lane)
                if m is None and unlaned is not None and not unlaned_consumed:
                    # Legacy CSV row with no lane: attach once to the first slot.
                    m = unlaned
                    unlaned_consumed = True
                lane_bit = f" {lane}" if lane else ""
                lane_key = (lane or "tv").lower()
                matched_dev = _device_for_fx_lane(devices, fx, lane or None)
                if m is not None:
                    thumb = m.thumb_key
                    # Prefer explicit lane thumb when we assigned an unlaned row.
                    if lane and (m.lane or "").strip() == "":
                        thumb = f"de_{lane.lower()}"
                    display = m.label or (f"{fx} {lane}".strip() if lane else fx)
                    tv_id = m.teamviewer_id
                else:
                    thumb = "de_tv" if not lane else f"de_{lane.lower()}"
                    display = f"{fx} {lane}".strip() if lane else fx
                    tv_id = None
                add_layer(
                    key=f"de_{fx.lower()}_{lane_key}",
                    label=f"TeamViewer{lane_bit}",
                    ip=tv_id,
                    thumb_key=thumb,
                    device=matched_dev,
                    ds_number=fx,
                    display_name=display,
                    fx_number=fx,
                    tv_lane=lane or None,
                    editable_tv=True,
                )
            # One OvrC local-time slot per FX (filled by the OvrC capture button).
            add_layer(
                key=f"ovrc_{fx.lower()}",
                label="OvrC",
                ip=None,
                thumb_key=f"ovrc_{fx.lower()}",
                device=None,
                ds_number=fx,
                display_name=f"{fx} local time",
                fx_number=fx,
            )
    else:
        add_layer(
            key="de_tv",
            label="TeamViewer",
            ip=None,
            thumb_key="de_tv",
            device=None,
            ds_number=None,
            display_name="Upload DragonEye CSV",
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
