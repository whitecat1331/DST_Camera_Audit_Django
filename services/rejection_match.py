"""Match rejection-service CSV rows to local DST Installations.

Complements ``services.ims_export_match`` but for the three-column rejection
CSV (Row Label / Count of Reason / Rejection Reason). Row Label is the camera
serial — either ``DS######`` (LTI pole camera) or ``FX####`` (DragonEye).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from services.rejection_csv import RejectionRow

_SKIP_SERIALS = {"", "n/a", "na", "?", "-", "none", "null"}

_FX_RE = re.compile(r"^FX\d+$", re.IGNORECASE)
_DS_RE = re.compile(r"^DS\d+$", re.IGNORECASE)


def _norm(value: str | None) -> str:
    return (value or "").strip().upper()


def _first_serial_part(raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text or text.lower() in _SKIP_SERIALS:
        return None
    return re.split(r"[|/]", text, maxsplit=1)[0].strip() or None


def _serial_tokens(raw: str | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;/|\s]+", raw or ""):
        tok = part.strip()
        if not tok or tok.lower() in _SKIP_SERIALS:
            continue
        key = tok.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
    return out


def serial_family(serial: str) -> str:
    """Return 'fx', 'ds', or '' for a rejection serial."""
    text = _norm(serial)
    if _FX_RE.match(text):
        return "fx"
    if _DS_RE.match(text):
        return "ds"
    return ""


@dataclass
class RejectionMatch:
    row: RejectionRow
    installation_id: int | None = None
    identifier: str = ""
    pole: str = ""
    kind: str = ""  # lti | de | ""
    match_by: str = ""
    status: str = "unmatched"  # matched | unmatched | ineligible | duplicate
    reason: str = ""


@dataclass
class RejectionMatchReport:
    rows: list[RejectionMatch] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "matched")

    @property
    def unmatched_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "unmatched")

    @property
    def ineligible_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "ineligible")

    @property
    def duplicate_count(self) -> int:
        return sum(1 for r in self.rows if r.status == "duplicate")


def match_rejection_rows(rows: list[RejectionRow]) -> RejectionMatchReport:
    """Match each CSV row to an active DST installation by serial token."""
    from cameras.models import Installation

    from audits.runner import _dst_site_kind

    report = RejectionMatchReport()
    if not rows:
        return report

    active = list(Installation.objects.filter(is_active=True).prefetch_related("devices"))
    by_serial: dict[str, list] = {}

    def index(token: str, inst) -> None:
        key = _norm(token)
        if not key:
            return
        by_serial.setdefault(key, []).append(inst)

    for inst in active:
        for tok in _serial_tokens(inst.serial_number):
            index(tok, inst)
        for cam in (inst.camera_a, inst.camera_b, getattr(inst, "camera_c", "")):
            for tok in _serial_tokens(cam):
                index(tok, inst)
        for fx in inst.fx_numbers or []:
            index(fx, inst)
        for device in inst.devices.all():
            for tok in _serial_tokens(device.unit_serial):
                index(tok, inst)

    seen_pks: set[int] = set()
    for row in rows:
        result = RejectionMatch(row=row)
        serial = _norm(row.serial)
        family = serial_family(serial)

        hits: list = []
        seen_hit: set[int] = set()
        for tok in _serial_tokens(row.serial):
            for candidate in by_serial.get(_norm(tok), []):
                if candidate.pk in seen_hit:
                    continue
                seen_hit.add(candidate.pk)
                hits.append(candidate)

        if not hits:
            result.status = "unmatched"
            result.reason = "No matching DST installation"
            report.rows.append(result)
            continue
        if len(hits) > 1:
            result.status = "unmatched"
            result.reason = f"Ambiguous — matched {len(hits)} installations"
            report.rows.append(result)
            continue

        inst = hits[0]
        result.installation_id = inst.pk
        result.identifier = (inst.identifier or "").strip()
        result.pole = (inst.pole_number or "").strip()
        result.match_by = "serial"

        kind = _dst_site_kind(inst) or ""
        if family == "fx" and kind != "de":
            kind = ""
        elif family == "ds" and kind != "lti":
            kind = ""
        result.kind = kind

        if inst.pk in seen_pks:
            result.status = "duplicate"
            result.reason = "Already matched earlier in this file"
            report.rows.append(result)
            continue

        if not kind:
            result.status = "ineligible"
            result.reason = "Matched but not eligible for rejection capture"
            report.rows.append(result)
            continue

        seen_pks.add(inst.pk)
        result.status = "matched"
        result.reason = f"Matched by {result.match_by}"
        report.rows.append(result)

    return report


def resolve_ds_vnc_target(inst, ds_serial: str) -> tuple[str | None, str | None, str | None]:
    """Resolve a DS serial to a VNC host, lane key, and thumb key.

    Returns ``(host, lane_key, thumb_key)`` or ``(None, None, None)``.
    """
    from services.device_layers import _build_lti_layers

    needle = _norm(ds_serial)
    if not needle:
        return None, None, None

    for layer in _build_lti_layers(inst):
        if layer.key not in {"vnc_l1", "vnc_l2"} or not layer.ip:
            continue
        if _norm(layer.ds_number) == needle:
            return layer.ip, layer.key, layer.thumb_key

    # Fallback: compare directly against camera_a (L1) / camera_b (L2).
    if _norm(_first_serial_part(inst.camera_a)) == needle:
        host = _norm(inst.tf_a_ip) or _derived_tf_host(inst, 1)
        return host, "vnc_l1", "vnc_l1"
    if _norm(_first_serial_part(inst.camera_b)) == needle:
        host = _norm(inst.tf_b_ip) or _derived_tf_host(inst, 2)
        return host, "vnc_l2", "vnc_l2"
    return None, None, None


def _derived_tf_host(inst, lane: int) -> str | None:
    from services.ip_map import DeviceType, pole_to_ip_address

    pole = (inst.pole_number or "").strip()
    try:
        return pole_to_ip_address(pole, DeviceType.TF_CPU, lane)
    except (ValueError, TypeError):
        return None


def resolve_fx_tv_targets(inst, fx_number: str) -> list[dict[str, str]]:
    """Resolve TeamViewer capture targets for one FX serial on an installation."""
    from services.device_layers import resolve_de_tv_capture_targets

    needle = _norm(fx_number)
    targets = resolve_de_tv_capture_targets(inst)
    return [t for t in targets if _norm(t.get("fx", "")) == needle]
