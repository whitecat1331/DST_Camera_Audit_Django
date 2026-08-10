"""Latest capture thumbnails keyed by pole (LTI + DragonEye)."""

from __future__ import annotations

from datetime import datetime

from django.db.models import Max

from audits.models import AuditJob, AuditScreenshot

THUMB_LABELS = ("cbw", "vnc_l1", "vnc_l2", "de_tv", "de_l1", "de_l2", "de_l3")


def normalize_thumb_label(shot: AuditScreenshot) -> str | None:
    label = (shot.label or "").strip().lower()
    if label in THUMB_LABELS:
        return label
    if label.startswith("de_l") and label[4:].isdigit():
        return label
    if label.startswith("ovrc_"):
        return label
    if label in {"date_time", "date/time"}:
        return "cbw"
    if label == "vnc_screenshot":
        if shot.job.device_type == AuditJob.DeviceType.TF_VNC:
            return "vnc_l1"
        if shot.job.device_type == AuditJob.DeviceType.POLE_BUNDLE:
            return None
        return "vnc_l1"
    return None


def latest_thumbs_for_poles(poles: list[str]) -> dict[str, dict[str, AuditScreenshot]]:
    """Return {pole: {thumb_key: AuditScreenshot}} for latest shots."""
    poles = [p for p in poles if p]
    out: dict[str, dict[str, AuditScreenshot]] = {p: {} for p in poles}
    if not poles:
        return out

    shots = (
        AuditScreenshot.objects.filter(job__pole_number__in=poles)
        .exclude(image="")
        .select_related("job")
        .order_by("-created_at")
    )
    for shot in shots:
        pole = shot.job.pole_number
        if pole not in out:
            continue
        key = normalize_thumb_label(shot)
        if key and key not in out[pole]:
            out[pole][key] = shot
    # Legacy unlaned DE captures use de_tv; L1 UI slots expect de_l1.
    for pole, by_key in out.items():
        if "de_l1" not in by_key and "de_tv" in by_key:
            by_key["de_l1"] = by_key["de_tv"]
    return out


def latest_vbe_check_at_for_keys(keys: list[str]) -> dict[str, datetime]:
    """Return {identifier_or_pole: finished_at} for the latest succeeded VBE Daily Check."""
    keys = [k.strip() for k in keys if (k or "").strip()]
    if not keys:
        return {}
    rows = (
        AuditJob.objects.filter(
            device_type=AuditJob.DeviceType.VBE_DAILY,
            status=AuditJob.Status.SUCCEEDED,
            pole_number__in=keys,
            finished_at__isnull=False,
        )
        .values("pole_number")
        .annotate(last=Max("finished_at"))
    )
    return {row["pole_number"]: row["last"] for row in rows if row["last"]}
