import json
import logging
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST
from django.utils import timezone
from django.http import FileResponse, Http404

from audits.models import AuditJob
from audits.runner import enqueue_audit_job, request_job_cancel
from cameras.models import Installation
from cameras.roles import role_at_least
from services.cbw_relays import turn_all_relays_on
from services.ip_map import DeviceType, pole_to_ip_address

logger = logging.getLogger(__name__)


@login_required
def audit_list(request):
    jobs = AuditJob.objects.select_related("created_by")[:100]
    return render(
        request,
        "audits/list.html",
        {"jobs": jobs, "can_audit": role_at_least(request.user, "technician")},
    )


@login_required
def audit_detail(request, pk):
    from audits.runner import _eastern_display, _parse_ovrc_timestamp_from_progress

    job = get_object_or_404(
        AuditJob.objects.prefetch_related("screenshots", "child_jobs")
        .select_related("created_by", "parent_job"),
        pk=pk,
    )
    ovrc_timestamp = _parse_ovrc_timestamp_from_progress(job.progress_message or "")
    # Hide legacy banners like "Done · FX1261: … ADT" once we have a parsed OvrC line.
    progress_display = (job.progress_message or "").strip()
    if ovrc_timestamp:
        progress_display = ""

    # Child jobs were historically marked finished before the fleet OvrC pass.
    # Prefer the OvrC screenshot wall time when it is later, so Finished >= OvrC capture.
    finished_dt = job.finished_at
    ovrc_shot = (
        job.screenshots.filter(label__istartswith="ovrc_")
        .order_by("-created_at")
        .first()
    )
    if ovrc_shot and ovrc_shot.created_at:
        if finished_dt is None or ovrc_shot.created_at > finished_dt:
            finished_dt = ovrc_shot.created_at

    return render(
        request,
        "audits/detail.html",
        {
            "job": job,
            "ovrc_timestamp": ovrc_timestamp,
            "progress_display": progress_display,
            "finished_est": _eastern_display(finished_dt) if finished_dt else "",
            "started_est": _eastern_display(job.started_at) if job.started_at else "",
            "created_est": _eastern_display(job.created_at) if job.created_at else "",
            "hide_shot_timestamps": bool(
                ovrc_timestamp
                or job.device_type
                in (
                    AuditJob.DeviceType.DST_SITE,
                    AuditJob.DeviceType.DST_AUDIT,
                    AuditJob.DeviceType.OVRC,
                )
            ),
        },
    )


def _shot_payload(shot) -> dict:
    captured = timezone.localtime(shot.created_at) if shot.created_at else None
    return {
        "label": shot.label,
        "url": shot.image.url if shot.image else "",
        "captured_at": captured.isoformat() if captured else None,
        "captured_at_display": (
            captured.strftime("%Y-%m-%d %H:%M:%S %Z") if captured else ""
        ),
    }


def _parse_dst_progress(target_host: str, progress_message: str) -> dict:
    """Derive phase / percent from dst_audit target_host + progress text.

    Phases: queued → power_on → settle → capture → ovrc → done
    """
    import re

    host = (target_host or "").strip().lower()
    msg = (progress_message or "").strip()
    msg_l = msg.lower()
    phase = "queued"
    current = 0
    total = 0
    percent = 0

    def _frac(prefix: str) -> tuple[int, int] | None:
        if not host.startswith(prefix):
            return None
        rest = host[len(prefix) :].strip()
        if "/" not in rest:
            return None
        a, _, b = rest.partition("/")
        try:
            return int(a), int(b)
        except ValueError:
            return None

    if host.startswith("done "):
        phase = "done"
        frac = _frac("done ")
        if frac:
            current, total = frac
        percent = 100
    elif host.startswith("ovrc "):
        phase = "ovrc"
        frac = _frac("ovrc ")
        if frac:
            current, total = frac
            percent = int(round(88 + (12 * current / total))) if total else 90
        else:
            percent = 90
    elif host.startswith("capture "):
        phase = "capture"
        frac = _frac("capture ")
        if frac:
            current, total = frac
            percent = int(round(55 + (33 * current / total))) if total else 55
    elif host.startswith("settle"):
        phase = "settle"
        percent = 48 if "skip" not in host else 52
    elif "settle skipped" in msg_l or "already on" in msg_l:
        phase = "settle"
        percent = 52
    elif host.startswith("power_on "):
        phase = "power_on"
        frac = _frac("power_on ")
        if frac:
            current, total = frac
            percent = int(round(40 * current / total)) if total else 5
    elif "waiting for audit worker" in msg_l or "worker slot" in msg_l:
        phase = "queued"
        percent = 2
    elif "phase 1" in msg_l or ("power" in msg_l and "ovrc local" not in msg_l):
        phase = "power_on"
        percent = 10
    elif "phase 2" in msg_l or "waiting" in msg_l:
        phase = "settle"
        percent = 48
    elif "ovrc local time" in msg_l or "fleet ovrc" in msg_l:
        phase = "ovrc"
        percent = 90
    elif "phase 3" in msg_l or "captur" in msg_l:
        phase = "capture"
        percent = 60

    # Prefer the last N/M in the message (skip "Phase 3/3" → use "4/100").
    matches = re.findall(r"(\d+)\s*/\s*(\d+)", msg)
    if matches and phase in {"power_on", "capture", "ovrc"} and total == 0:
        try:
            msg_cur, msg_tot = int(matches[-1][0]), int(matches[-1][1])
            if msg_tot > 0:
                current, total = msg_cur, msg_tot
                if phase == "capture":
                    percent = int(round(55 + (33 * current / total)))
                elif phase == "ovrc":
                    percent = int(round(88 + (12 * current / total)))
                else:
                    percent = int(round(40 * current / total))
        except ValueError:
            pass

    return {
        "phase": phase,
        "current": current,
        "total": total,
        "percent": max(0, min(100, percent)),
    }


@login_required
@require_GET
def audit_job_status(request, pk):
    job = get_object_or_404(AuditJob.objects.prefetch_related("screenshots"), pk=pk)
    shots = [_shot_payload(s) for s in job.screenshots.all() if s.image]
    elapsed_s = None
    if job.started_at:
        end = job.finished_at or timezone.now()
        elapsed_s = max(0, int((end - job.started_at).total_seconds()))
    if job.status == AuditJob.Status.FAILED:
        message = job.error_message or job.progress_message or job.get_status_display()
    elif job.status == AuditJob.Status.CANCELLED:
        message = job.progress_message or job.error_message or "Cancelled"
    elif job.progress_message:
        message = job.progress_message
    else:
        message = job.get_status_display()

    payload = {
        "id": job.pk,
        "status": job.status,
        "message": message,
        "progress": job.progress_message,
        "error_message": job.error_message,
        "elapsed_s": elapsed_s,
        "screenshots": shots,
        "device_type": job.device_type,
        "pole_number": job.pole_number,
        "target_host": job.target_host,
    }

    if job.device_type == AuditJob.DeviceType.DST_AUDIT:
        progress = _parse_dst_progress(job.target_host, job.progress_message)
        if job.status == AuditJob.Status.SUCCEEDED:
            progress["phase"] = "done"
            progress["percent"] = 100
        elif job.status == AuditJob.Status.FAILED:
            progress["phase"] = "failed"
        elif job.status == AuditJob.Status.CANCELLED:
            progress["phase"] = "cancelled"
        children = list(
            job.child_jobs.order_by("created_at").prefetch_related("screenshots")[:500]
        )
        site_rows = []
        for child in children:
            child_shots = [_shot_payload(s) for s in child.screenshots.all() if s.image]
            kind = (child.target_host or "").strip().lower()
            if kind not in {"lti", "de"}:
                kind = "—"
            site_rows.append(
                {
                    "id": child.pk,
                    "pole": child.pole_number,
                    "kind": kind,
                    "status": child.status,
                    "message": child.progress_message or child.get_status_display(),
                    "error_message": child.error_message,
                    "detail_url": f"/audits/{child.pk}/",
                    "screenshots": child_shots,
                    "shot_count": len(child_shots),
                }
            )
        payload["dst"] = {
            **progress,
            "sites": site_rows,
            "sites_done": sum(
                1
                for s in site_rows
                if s["status"] in ("succeeded", "failed", "cancelled")
            ),
            "sites_ok": sum(1 for s in site_rows if s["status"] == "succeeded"),
            "sites_failed": sum(1 for s in site_rows if s["status"] == "failed"),
        }

    if job.device_type == AuditJob.DeviceType.CONFIRM_BATCH:
        children = list(
            job.child_jobs.order_by("created_at").prefetch_related("screenshots")[:500]
        )
        site_rows = []
        for child in children:
            child_shots = [_shot_payload(s) for s in child.screenshots.all() if s.image]
            kind = (child.target_host or "").strip().lower()
            if kind not in {"lti", "de"}:
                kind = "—"
            site_rows.append(
                {
                    "id": child.pk,
                    "pole": child.pole_number,
                    "kind": kind,
                    "status": child.status,
                    "message": child.progress_message or child.get_status_display(),
                    "error_message": child.error_message,
                    "detail_url": f"/audits/{child.pk}/",
                    "screenshots": child_shots,
                    "shot_count": len(child_shots),
                }
            )
        host = (job.target_host or "").strip()
        current = 0
        total = len(site_rows)
        if "/" in host:
            a, _, b = host.partition("/")
            try:
                current, total = int(a), int(b)
            except ValueError:
                pass
        if job.status == AuditJob.Status.SUCCEEDED:
            percent = 100
        elif job.status in (AuditJob.Status.FAILED, AuditJob.Status.CANCELLED):
            percent = 100 if total and current >= total else (
                int(round(100 * current / total)) if total else 0
            )
        else:
            percent = int(round(100 * current / total)) if total else 5
        payload["confirm"] = {
            "current": current,
            "total": total or len(site_rows),
            "percent": max(0, min(100, percent)),
            "sites": site_rows,
            "sites_done": sum(
                1
                for s in site_rows
                if s["status"] in ("succeeded", "failed", "cancelled")
            ),
            "sites_ok": sum(1 for s in site_rows if s["status"] == "succeeded"),
            "sites_failed": sum(1 for s in site_rows if s["status"] == "failed"),
        }

    return JsonResponse(payload)


@login_required
@require_POST
def audit_start(request):
    if not role_at_least(request.user, "technician"):
        raise PermissionDenied

    pole_number = request.POST.get("pole_number", "").strip()
    device_type = request.POST.get("device_type", "").strip()
    installation_id = request.POST.get("installation_id", "").strip()
    lane = request.POST.get("lane", "1").strip() or "1"

    allowed = {
        AuditJob.DeviceType.CBW,
        AuditJob.DeviceType.TF_VNC,
        AuditJob.DeviceType.POLE_BUNDLE,
    }
    if device_type not in allowed:
        messages.error(request, "Invalid device type.")
        return redirect(request.POST.get("next") or "audit_list")

    if not pole_number and installation_id:
        installation = get_object_or_404(Installation, pk=installation_id)
        pole_number = installation.pole_number

    if not pole_number:
        messages.error(request, "Pole number is required.")
        return redirect(request.POST.get("next") or "audit_list")

    try:
        if device_type == AuditJob.DeviceType.CBW:
            host = pole_to_ip_address(pole_number, DeviceType.CBW)
        elif device_type == AuditJob.DeviceType.POLE_BUNDLE:
            host = pole_to_ip_address(pole_number, DeviceType.CBW)
        else:
            host = pole_to_ip_address(pole_number, DeviceType.TF_CPU, lane=int(lane))
    except (ValueError, TypeError) as exc:
        messages.error(request, f"Could not derive IP: {exc}")
        return redirect(request.POST.get("next") or "audit_list")

    job = AuditJob.objects.create(
        pole_number=pole_number,
        target_host=host,
        device_type=device_type,
        created_by=request.user,
    )
    enqueue_audit_job(job.pk)
    logger.info(
        "[AUDIT] started job=%s type=%s pole=%s host=%s user=%s",
        job.pk,
        device_type,
        pole_number,
        host,
        request.user.get_username(),
    )
    messages.success(
        request,
        f"Started {job.get_device_type_display()} audit for pole {pole_number} ({host})",
    )
    return redirect("audit_detail", pk=job.pk)


@login_required
@require_POST
def capture_pole(request):
    """Start a capture job. Modes: cbw | vnc | tv | ovrc (default: legacy auto)."""
    if not role_at_least(request.user, "technician"):
        return JsonResponse({"error": "forbidden"}, status=403)

    if request.content_type and "application/json" in request.content_type:
        try:
            body = json.loads(request.body.decode() or "{}")
        except json.JSONDecodeError:
            body = {}
        pole_number = str(body.get("pole_number") or "").strip()
        installation_id = str(body.get("installation_id") or "").strip()
        mode = str(body.get("mode") or "").strip().lower()
    else:
        pole_number = request.POST.get("pole_number", "").strip()
        installation_id = request.POST.get("installation_id", "").strip()
        mode = request.POST.get("mode", "").strip().lower()

    # Normalize aliases from the UI.
    if mode in {"teamviewer", "de", "dragoneye", "fx"}:
        mode = "tv"
    if mode in {"vnc_bundle", "tf_vnc"}:
        mode = "vnc"

    inst = None
    if installation_id:
        try:
            inst = Installation.objects.filter(pk=int(installation_id), is_active=True).first()
        except (TypeError, ValueError):
            inst = None
    if inst is None and pole_number:
        inst = (
            Installation.objects.filter(pole_number=pole_number, is_active=True)
            .order_by("-last_synced_at")
            .first()
        )

    # Legacy: no mode → DragonEye TV bundle or LTI full pole.
    if not mode:
        if inst is not None and inst.is_dragoneye:
            mode = "tv"
        else:
            mode = "pole"

    job_pole = ""
    if inst is not None:
        job_pole = (inst.pole_number or inst.identifier or pole_number or "").strip()
    if not job_pole:
        job_pole = pole_number

    if mode == "tv":
        if inst is None or not inst.is_dragoneye:
            return JsonResponse({"error": "TeamViewer capture requires a DragonEye / FX site"}, status=400)
        if not job_pole:
            return JsonResponse({"error": "installation has no pole or identifier"}, status=400)
        fx = inst.fx_number or ""
        job = AuditJob.objects.create(
            pole_number=job_pole,
            target_host=fx,
            device_type=AuditJob.DeviceType.DE_BUNDLE,
            created_by=request.user,
        )
        enqueue_audit_job(job.pk)
        logger.info(
            "[AUDIT] capture-tv job=%s pole=%s fx=%s user=%s",
            job.pk,
            job_pole,
            fx,
            request.user.get_username(),
        )
        return JsonResponse(
            {
                "ok": True,
                "job_id": job.pk,
                "status_url": f"/audits/{job.pk}/status/",
                "detail_url": f"/audits/{job.pk}/",
                "mode": "tv",
                "pole": job_pole,
            }
        )

    if mode == "ovrc":
        if inst is None or not inst.is_dragoneye:
            return JsonResponse({"error": "OvrC capture requires a DragonEye / FX site"}, status=400)
        if not job_pole:
            return JsonResponse({"error": "installation has no pole or identifier"}, status=400)
        if not (inst.fx_numbers or []):
            return JsonResponse({"error": "no FX serial on this installation"}, status=400)
        if not (getattr(settings, "OVRC_USERNAME", "") and getattr(settings, "OVRC_PASSWORD", "")):
            return JsonResponse({"error": "OVRC_USERNAME / OVRC_PASSWORD not configured"}, status=400)
        fx = ",".join(inst.fx_numbers)
        job = AuditJob.objects.create(
            pole_number=job_pole,
            target_host=fx,
            device_type=AuditJob.DeviceType.OVRC,
            created_by=request.user,
        )
        enqueue_audit_job(job.pk)
        logger.info(
            "[AUDIT] capture-ovrc job=%s pole=%s fx=%s user=%s",
            job.pk,
            job_pole,
            fx,
            request.user.get_username(),
        )
        return JsonResponse(
            {
                "ok": True,
                "job_id": job.pk,
                "status_url": f"/audits/{job.pk}/status/",
                "detail_url": f"/audits/{job.pk}/",
                "mode": "ovrc",
                "pole": job_pole,
            }
        )

    if not job_pole and not pole_number:
        return JsonResponse({"error": "pole_number is required"}, status=400)
    pole = pole_number or job_pole

    if mode == "cbw":
        try:
            host = pole_to_ip_address(pole, DeviceType.CBW)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        job = AuditJob.objects.create(
            pole_number=pole,
            target_host=host,
            device_type=AuditJob.DeviceType.CBW,
            created_by=request.user,
        )
        enqueue_audit_job(job.pk)
        logger.info(
            "[AUDIT] capture-cbw job=%s pole=%s host=%s user=%s",
            job.pk,
            pole,
            host,
            request.user.get_username(),
        )
        return JsonResponse(
            {
                "ok": True,
                "job_id": job.pk,
                "status_url": f"/audits/{job.pk}/status/",
                "detail_url": f"/audits/{job.pk}/",
                "mode": "cbw",
                "pole": pole,
            }
        )

    if mode == "vnc":
        try:
            host = pole_to_ip_address(pole, DeviceType.TF_CPU, lane=1)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        job = AuditJob.objects.create(
            pole_number=pole,
            target_host=host,
            device_type=AuditJob.DeviceType.VNC_BUNDLE,
            created_by=request.user,
        )
        enqueue_audit_job(job.pk)
        logger.info(
            "[AUDIT] capture-vnc job=%s pole=%s host=%s user=%s",
            job.pk,
            pole,
            host,
            request.user.get_username(),
        )
        return JsonResponse(
            {
                "ok": True,
                "job_id": job.pk,
                "status_url": f"/audits/{job.pk}/status/",
                "detail_url": f"/audits/{job.pk}/",
                "mode": "vnc",
                "pole": pole,
            }
        )

    if mode == "pole":
        try:
            host = pole_to_ip_address(pole, DeviceType.CBW)
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        job = AuditJob.objects.create(
            pole_number=pole,
            target_host=host,
            device_type=AuditJob.DeviceType.POLE_BUNDLE,
            created_by=request.user,
        )
        enqueue_audit_job(job.pk)
        logger.info(
            "[AUDIT] capture-pole job=%s pole=%s host=%s user=%s",
            job.pk,
            pole,
            host,
            request.user.get_username(),
        )
        return JsonResponse(
            {
                "ok": True,
                "job_id": job.pk,
                "status_url": f"/audits/{job.pk}/status/",
                "detail_url": f"/audits/{job.pk}/",
                "mode": "lti",
                "pole": pole,
            }
        )

    return JsonResponse(
        {"error": f"unknown mode {mode!r}; use cbw, vnc, tv, ovrc"},
        status=400,
    )


def _vbe_daily_checks_root_ready():
    """Ensure the shared VBE Daily Checks folder is configured and writable."""
    from services.vbe_daily_checks import daily_checks_root

    root = daily_checks_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


@login_required
@require_POST
def start_vbe_daily_check(request):
    """Capture one VBE site into the shared VBE Daily Checks folder."""
    if not role_at_least(request.user, "technician"):
        return JsonResponse({"error": "forbidden"}, status=403)

    if request.content_type and "application/json" in request.content_type:
        try:
            body = json.loads(request.body.decode() or "{}")
        except json.JSONDecodeError:
            body = {}
        installation_id = str(body.get("installation_id") or "").strip()
    else:
        installation_id = request.POST.get("installation_id", "").strip()

    if not installation_id:
        return JsonResponse({"error": "installation_id is required"}, status=400)

    try:
        inst = Installation.objects.filter(pk=int(installation_id), is_active=True).first()
    except (TypeError, ValueError):
        inst = None
    if inst is None:
        return JsonResponse({"error": "installation not found"}, status=404)
    if not inst.is_vbe:
        return JsonResponse({"error": "installation is not a VBE site"}, status=400)

    key = (inst.identifier or inst.pole_number or "").strip()
    if not key:
        return JsonResponse({"error": "installation has no identifier or pole"}, status=400)

    if AuditJob.objects.filter(
        device_type=AuditJob.DeviceType.VBE_DAILY_ALL,
        status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
    ).exists():
        return JsonResponse(
            {"error": "VBE Daily Checks (all sites) already running"},
            status=409,
        )

    if AuditJob.objects.filter(
        device_type=AuditJob.DeviceType.VBE_DAILY,
        pole_number=key,
        status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
    ).exists():
        return JsonResponse(
            {"error": f"VBE Check already running for {key}"},
            status=409,
        )

    try:
        root = _vbe_daily_checks_root_ready()
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({"error": f"VBE Daily Checks folder: {exc}"}, status=400)

    job = AuditJob.objects.create(
        pole_number=key,
        target_host=inst.fx_number or "",
        device_type=AuditJob.DeviceType.VBE_DAILY,
        created_by=request.user,
    )
    enqueue_audit_job(job.pk)
    logger.info(
        "[AUDIT] vbe-daily job=%s site=%s fx=%s user=%s root=%s",
        job.pk,
        key,
        inst.fx_number or "",
        request.user.get_username(),
        root,
    )
    return JsonResponse(
        {
            "ok": True,
            "job_id": job.pk,
            "sites": 1,
            "status_url": f"/audits/{job.pk}/status/",
            "detail_url": f"/audits/{job.pk}/",
            "mode": "vbe_daily",
            "pole": key,
        }
    )


@login_required
@require_POST
def start_vbe_daily_checks(request):
    """Capture every active VBE site into the shared VBE Daily Checks folder."""
    if not role_at_least(request.user, "technician"):
        return JsonResponse({"error": "forbidden"}, status=403)

    running = AuditJob.objects.filter(
        device_type=AuditJob.DeviceType.VBE_DAILY_ALL,
        status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
    ).exists()
    if running:
        return JsonResponse({"error": "VBE Daily Checks already running"}, status=409)

    from audits.runner import _active_vbe_installations

    try:
        root = _vbe_daily_checks_root_ready()
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({"error": f"VBE Daily Checks folder: {exc}"}, status=400)

    vbes = _active_vbe_installations()
    if not vbes:
        return JsonResponse({"error": "No active VBE installations"}, status=400)

    job = AuditJob.objects.create(
        pole_number="VBE-ALL",
        target_host=f"0/{len(vbes)} sites",
        device_type=AuditJob.DeviceType.VBE_DAILY_ALL,
        created_by=request.user,
    )
    enqueue_audit_job(job.pk)
    logger.info(
        "[AUDIT] vbe-daily-all job=%s sites=%s user=%s root=%s",
        job.pk,
        len(vbes),
        request.user.get_username(),
        root,
    )
    return JsonResponse(
        {
            "ok": True,
            "job_id": job.pk,
            "sites": len(vbes),
            "status_url": f"/audits/{job.pk}/status/",
            "detail_url": f"/audits/{job.pk}/",
            "mode": "vbe_daily_all",
        }
    )


def _dst_recent_audit_rows(limit: int = 12) -> list[dict]:
    """One row per parent DST audit, with nested child site jobs for drill-down."""
    from audits.models import AuditJob
    from audits.runner import (
        _eastern_display,
        _parse_ovrc_timestamp_from_progress,
    )

    recent = (
        AuditJob.objects.filter(device_type=AuditJob.DeviceType.DST_AUDIT)
        .prefetch_related("child_jobs", "child_jobs__screenshots")
        .order_by("-created_at")[:limit]
    )
    rows: list[dict] = []
    for parent in recent:
        children_payload = []
        for child in parent.child_jobs.all():
            kind = (child.target_host or "").strip().lower()
            if kind not in {"lti", "de"}:
                kind = ""
            shots = list(child.screenshots.all())
            ovrc_ts = _parse_ovrc_timestamp_from_progress(child.progress_message or "")
            # List rows stay compact — OvrC clock is only on the site detail page.
            if child.status == AuditJob.Status.SUCCEEDED:
                short_msg = (
                    "Done with warnings"
                    if (child.error_message or "").strip()
                    else "Done"
                )
            else:
                short_msg = child.get_status_display()
            children_payload.append(
                {
                    "id": child.pk,
                    "label": f"#{child.pk}",
                    "site": child.pole_number or "—",
                    "kind": kind,
                    "status": child.status,
                    "status_display": child.get_status_display(),
                    "message": short_msg,
                    "ovrc_timestamp": ovrc_ts,
                    "detail_url": f"/audits/{child.pk}/",
                    "shot_count": len(shots),
                }
            )
        poles = [c["site"] for c in children_payload if c["site"] and c["site"] != "—"]
        if len(poles) <= 3:
            sites_summary = ", ".join(poles) if poles else "no sites yet"
        else:
            sites_summary = f"{', '.join(poles[:2])} +{len(poles) - 2} more"
        when = parent.finished_at or parent.created_at
        rows.append(
            {
                "id": parent.pk,
                "label": f"#{parent.pk}",
                "status": parent.status,
                "status_display": parent.get_status_display(),
                "when": when.isoformat() if when else "",
                "when_display": _eastern_display(when),
                "message": (
                    "Done"
                    if parent.status == AuditJob.Status.SUCCEEDED
                    and not (parent.error_message or "").strip()
                    else (
                        parent.progress_message
                        if parent.progress_message
                        and not str(parent.progress_message).startswith("OvrC ")
                        else parent.get_status_display()
                    )
                ),
                "site_count": len(children_payload),
                "sites_summary": sites_summary,
                "detail_url": f"/audits/{parent.pk}/",
                "children": children_payload,
            }
        )
    return rows

@login_required
def dst_audit_page(request):
    """DST Audit orchestration GUI — power on fleet, then capture with timestamps."""
    from audits.runner import _active_dst_installations, _dst_site_kind

    can_audit = role_at_least(request.user, "technician")
    scope = (request.GET.get("scope") or "all").strip().lower()
    state = (request.GET.get("state") or "").strip()
    agency = (request.GET.get("agency") or "").strip()
    q = (request.GET.get("q") or "").strip()
    if scope not in {"all", "lti", "de"}:
        scope = "all"

    sites = _active_dst_installations(scope=scope, state=state, agency=agency)
    preview = []
    lti_count = 0
    de_count = 0
    q_lower = q.lower()
    for inst in sites:
        kind = _dst_site_kind(inst) or "?"
        serials = _dst_serials_for_inst(inst)
        serials_text = " · ".join(serials) if serials else ""
        if q_lower:
            hay = " ".join(
                [
                    (inst.pole_number or ""),
                    (inst.identifier or ""),
                    (inst.primary_platform or ""),
                    (inst.state or ""),
                    (inst.agency or ""),
                    (inst.location or ""),
                    (inst.serial_number or ""),
                    (inst.camera_a or ""),
                    (inst.camera_b or ""),
                    (inst.camera_c or ""),
                    serials_text,
                    kind,
                    " ".join(inst.fx_numbers or []),
                ]
            ).lower()
            if q_lower not in hay:
                continue
        if kind == "lti":
            lti_count += 1
        elif kind == "de":
            de_count += 1
        preview.append(
            {
                "id": inst.pk,
                "pole": (inst.pole_number or "").strip(),
                "identifier": (inst.identifier or "").strip(),
                "platform": (inst.primary_platform or "").strip(),
                "state": (inst.state or "").strip(),
                "agency": (inst.agency or "").strip(),
                "kind": kind,
                "fx": ", ".join(inst.fx_numbers or []),
                "serial": (inst.serial_number or "").strip(),
                "serials": serials_text,
                "camera_a": (inst.camera_a or "").strip(),
                "camera_b": (inst.camera_b or "").strip(),
            }
        )

    states = list(
        Installation.objects.filter(is_active=True)
        .exclude(state="")
        .values_list("state", flat=True)
        .distinct()
        .order_by("state")
    )
    agencies = list(
        Installation.objects.filter(is_active=True)
        .exclude(agency="")
        .values_list("agency", flat=True)
        .distinct()
        .order_by("agency")
    )

    active_job = (
        AuditJob.objects.filter(
            device_type=AuditJob.DeviceType.DST_AUDIT,
            status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
        )
        .order_by("-created_at")
        .first()
    )
    recent_rows = _dst_recent_audit_rows(12)

    settle = int(getattr(settings, "DST_POWER_SETTLE_SECONDS", 90))

    return render(
        request,
        "audits/dst_audit.html",
        {
            "can_audit": can_audit,
            "scope": scope,
            "state": state,
            "agency": agency,
            "q": q,
            "states": states,
            "agencies": agencies,
            "sites": preview,
            "site_count": len(preview),
            "lti_count": lti_count,
            "de_count": de_count,
            "active_job": active_job,
            "recent_jobs": recent_rows,
            "settle_seconds": settle,
            "has_filters": bool(q or state or agency or (scope and scope != "all")),
        },
    )


def _dst_serials_for_inst(inst) -> list[str]:
    """Distinct camera / unit serials for the DST eligible-sites table."""
    skip = {"", "n/a", "na", "?", "-", "none", "null"}
    out: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None, *, prefix: str = "") -> None:
        text = (raw or "").strip()
        if not text or text.lower() in skip:
            return
        key = text.upper()
        if key in seen:
            return
        seen.add(key)
        out.append(f"{prefix}{text}" if prefix else text)

    add(inst.serial_number)
    add(inst.camera_a, prefix="A ")
    add(inst.camera_b, prefix="B ")
    add(getattr(inst, "camera_c", None), prefix="C ")
    for fx in inst.fx_numbers or []:
        add(fx)
    return out


@login_required
def dst_recent_audits(request):
    """JSON: recent parent DST audits with nested site children."""
    return JsonResponse({"jobs": _dst_recent_audit_rows(12)})


@login_required
@require_POST
def start_dst_audit(request):
    """Start a fleet DST audit (power-all → settle → capture → OvrC times)."""
    if not role_at_least(request.user, "technician"):
        return JsonResponse({"error": "forbidden"}, status=403)

    if request.content_type and "application/json" in request.content_type:
        try:
            body = json.loads(request.body.decode() or "{}")
        except json.JSONDecodeError:
            body = {}
        scope = str(body.get("scope") or "all").strip().lower()
        state = str(body.get("state") or "").strip()
        agency = str(body.get("agency") or "").strip()
        raw_ids = body.get("installation_ids") or body.get("ids") or []
    else:
        scope = (request.POST.get("scope") or "all").strip().lower()
        state = (request.POST.get("state") or "").strip()
        agency = (request.POST.get("agency") or "").strip()
        raw_ids = request.POST.getlist("installation_ids")

    if scope not in {"all", "lti", "de"}:
        return JsonResponse({"error": "scope must be all, lti, or de"}, status=400)

    installation_ids: list[int] = []
    if isinstance(raw_ids, str):
        raw_ids = [p for p in raw_ids.replace(";", ",").split(",") if p.strip()]
    if not isinstance(raw_ids, (list, tuple)):
        return JsonResponse({"error": "installation_ids must be a list"}, status=400)
    for x in raw_ids:
        try:
            installation_ids.append(int(x))
        except (TypeError, ValueError):
            return JsonResponse({"error": f"invalid installation id: {x!r}"}, status=400)
    # Preserve order, drop duplicates.
    seen: set[int] = set()
    installation_ids = [i for i in installation_ids if not (i in seen or seen.add(i))]

    if not installation_ids:
        return JsonResponse(
            {"error": "Select at least one site to audit"},
            status=400,
        )

    running = AuditJob.objects.filter(
        device_type=AuditJob.DeviceType.DST_AUDIT,
        status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
    ).exists()
    if running:
        return JsonResponse({"error": "A DST Audit is already running"}, status=409)

    from audits.runner import (
        _active_dst_installations,
        _encode_dst_audit_meta,
        write_dst_selection,
    )

    sites = _active_dst_installations(
        scope=scope,
        state=state,
        agency=agency,
        installation_ids=installation_ids,
    )
    if not sites:
        return JsonResponse(
            {"error": "None of the selected sites are eligible for DST audit"},
            status=400,
        )

    # Only persist IDs that survived eligibility checks.
    resolved_ids = [inst.pk for inst in sites]
    meta = _encode_dst_audit_meta(scope, state, agency)
    job = AuditJob.objects.create(
        pole_number="DST-ALL",
        target_host=meta,
        device_type=AuditJob.DeviceType.DST_AUDIT,
        created_by=request.user,
        progress_message=f"Queued — {len(sites)} site(s)",
    )
    write_dst_selection(job.pk, resolved_ids)
    enqueue_audit_job(job.pk)
    logger.info(
        "[DST] start job=%s sites=%s selected=%s scope=%s state=%s agency=%s user=%s",
        job.pk,
        len(sites),
        len(resolved_ids),
        scope,
        state or "*",
        agency or "*",
        request.user.get_username(),
    )
    return JsonResponse(
        {
            "ok": True,
            "job_id": job.pk,
            "sites": len(sites),
            "scope": scope,
            "status_url": f"/audits/{job.pk}/status/",
            "detail_url": f"/audits/{job.pk}/",
            "page_url": f"/audits/dst/?job={job.pk}",
            "mode": "dst_audit",
        }
    )


@login_required
@require_POST
def cancel_audit_job(request, pk):
    """Cancel a pending/running audit and tear down TeamViewer sessions/notes."""
    if not role_at_least(request.user, "technician"):
        return JsonResponse({"error": "forbidden"}, status=403)

    job = get_object_or_404(AuditJob, pk=pk)
    if job.status not in (AuditJob.Status.PENDING, AuditJob.Status.RUNNING):
        return JsonResponse(
            {
                "ok": True,
                "id": job.pk,
                "status": job.status,
                "message": f"Job already {job.status}",
            }
        )

    if not request_job_cancel(job.pk):
        job.refresh_from_db()
        return JsonResponse(
            {
                "ok": True,
                "id": job.pk,
                "status": job.status,
                "message": f"Job already {job.status}",
            }
        )

    logger.info(
        "[AUDIT] cancel job=%s type=%s pole=%s user=%s",
        job.pk,
        job.device_type,
        job.pole_number,
        request.user.get_username(),
    )
    job.refresh_from_db()
    return JsonResponse(
        {
            "ok": True,
            "id": job.pk,
            "status": job.status,
            "message": "Cancel requested — closing TeamViewer sessions",
            "status_url": f"/audits/{job.pk}/status/",
        }
    )


@login_required
@require_POST
def turn_relays(request):
    if not role_at_least(request.user, "technician"):
        return JsonResponse({"error": "forbidden"}, status=403)

    if request.content_type and "application/json" in request.content_type:
        try:
            body = json.loads(request.body.decode() or "{}")
        except json.JSONDecodeError:
            body = {}
        pole_number = str(body.get("pole_number") or "").strip()
    else:
        pole_number = request.POST.get("pole_number", "").strip()

    if not pole_number:
        return JsonResponse({"error": "pole_number is required"}, status=400)

    try:
        result = turn_all_relays_on(
            pole_number,
            settings.CBW_USERNAME,
            settings.CBW_PASSWORDS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[AUDIT] relays failed pole=%s user=%s err=%s",
            pole_number,
            request.user.get_username(),
            type(exc).__name__,
        )
        return JsonResponse({"error": str(exc)}, status=500)

    logger.info(
        "[AUDIT] relays %s pole=%s relays=%s user=%s",
        result.action,
        pole_number,
        result.relays,
        request.user.get_username(),
    )
    return JsonResponse(
        {
            "ok": True,
            "pole": pole_number,
            "action": result.action,
            "relays": result.relays,
        }
    )


@login_required
@require_POST
def turn_ovrc_dcam(request):
    """Turn OvrC WattBox 'DCAM System' ON for an FX site (no-op if already on)."""
    if not role_at_least(request.user, "technician"):
        return JsonResponse({"error": "forbidden"}, status=403)

    if request.content_type and "application/json" in request.content_type:
        try:
            body = json.loads(request.body.decode() or "{}")
        except json.JSONDecodeError:
            body = {}
        installation_id = str(body.get("installation_id") or "").strip()
        pole_number = str(body.get("pole_number") or "").strip()
    else:
        installation_id = request.POST.get("installation_id", "").strip()
        pole_number = request.POST.get("pole_number", "").strip()

    inst = None
    if installation_id:
        try:
            inst = Installation.objects.filter(pk=int(installation_id), is_active=True).first()
        except (TypeError, ValueError):
            inst = None
    if inst is None and pole_number:
        inst = (
            Installation.objects.filter(pole_number=pole_number, is_active=True)
            .order_by("-last_synced_at")
            .first()
        )
        if inst is None:
            inst = (
                Installation.objects.filter(identifier=pole_number, is_active=True)
                .order_by("-last_synced_at")
                .first()
            )

    if inst is None:
        return JsonResponse({"error": "installation not found"}, status=404)
    if not inst.is_dragoneye:
        return JsonResponse({"error": "Turn On (OvrC) is only for FX / DragonEye sites"}, status=400)

    fx_list = list(inst.fx_numbers or [])
    if not fx_list:
        return JsonResponse({"error": "no FX serial on this installation"}, status=400)

    ovrc_user = (getattr(settings, "OVRC_USERNAME", "") or "").strip()
    ovrc_pass = (getattr(settings, "OVRC_PASSWORD", "") or "").strip()
    if not ovrc_user or not ovrc_pass:
        return JsonResponse({"error": "OVRC_USERNAME / OVRC_PASSWORD not configured"}, status=400)

    from services.ovrc_capture import ensure_dcam_on_for_fxes

    try:
        results = ensure_dcam_on_for_fxes(
            fx_list,
            username=ovrc_user,
            password=ovrc_pass,
            base_url=getattr(settings, "OVRC_BASE_URL", "") or "https://app.ovrc.com",
            headless=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[AUDIT] ovrc-dcam failed site=%s fx=%s user=%s",
            inst.identifier or inst.pole_number,
            ",".join(fx_list),
            request.user.get_username(),
        )
        return JsonResponse({"error": str(exc)}, status=500)

    payload = [
        {"fx": r.fx_number, "action": r.action, "detail": r.detail}
        for r in results
    ]
    logger.info(
        "[AUDIT] ovrc-dcam site=%s results=%s user=%s",
        inst.identifier or inst.pole_number,
        payload,
        request.user.get_username(),
    )
    return JsonResponse(
        {
            "ok": True,
            "pole": (inst.pole_number or inst.identifier or "").strip(),
            "results": payload,
        }
    )


def _confirm_recent_rows(limit: int = 12) -> list[dict]:
    from audits.runner import _eastern_display

    recent = (
        AuditJob.objects.filter(
            device_type__in=[
                AuditJob.DeviceType.CONFIRM_BATCH,
                AuditJob.DeviceType.CONFIRM_SITE,
            ],
            parent_job__isnull=True,
        )
        .prefetch_related("child_jobs")
        .order_by("-created_at")[:limit]
    )
    rows: list[dict] = []
    for job in recent:
        when = job.finished_at or job.created_at
        children = list(job.child_jobs.all()) if job.device_type == AuditJob.DeviceType.CONFIRM_BATCH else []
        if children:
            poles = [c.pole_number for c in children if c.pole_number]
            if len(poles) <= 3:
                sites_summary = ", ".join(poles) if poles else "no sites yet"
            else:
                sites_summary = f"{', '.join(poles[:2])} +{len(poles) - 2} more"
            site_count = len(children)
        else:
            sites_summary = job.pole_number or "—"
            site_count = 1
        rows.append(
            {
                "id": job.pk,
                "label": f"#{job.pk}",
                "status": job.status,
                "status_display": job.get_status_display(),
                "when": when.isoformat() if when else "",
                "when_display": _eastern_display(when),
                "message": job.progress_message or job.get_status_display(),
                "site_count": site_count,
                "sites_summary": sites_summary,
                "detail_url": f"/audits/{job.pk}/",
                "device_type": job.device_type,
            }
        )
    return rows


@login_required
def confirm_captures_page(request):
    """Confirm camera captures — single site or IMS installation-list batch."""
    from audits.runner import _active_dst_installations, _dst_site_kind

    can_audit = role_at_least(request.user, "technician")
    q = (request.GET.get("q") or "").strip()
    scope = (request.GET.get("scope") or "all").strip().lower()
    if scope not in {"all", "lti", "de"}:
        scope = "all"

    sites = _active_dst_installations(scope=scope)
    preview = []
    lti_count = 0
    de_count = 0
    q_lower = q.lower()
    for inst in sites:
        kind = _dst_site_kind(inst) or "?"
        serials = _dst_serials_for_inst(inst)
        serials_text = " · ".join(serials) if serials else ""
        if q_lower:
            hay = " ".join(
                [
                    (inst.pole_number or ""),
                    (inst.identifier or ""),
                    (inst.primary_platform or ""),
                    (inst.state or ""),
                    (inst.agency or ""),
                    (inst.location or ""),
                    (inst.serial_number or ""),
                    serials_text,
                    kind,
                    " ".join(inst.fx_numbers or []),
                ]
            ).lower()
            if q_lower not in hay:
                continue
        if kind == "lti":
            lti_count += 1
        elif kind == "de":
            de_count += 1
        preview.append(
            {
                "id": inst.pk,
                "pole": (inst.pole_number or "").strip(),
                "identifier": (inst.identifier or "").strip(),
                "platform": (inst.primary_platform or "").strip(),
                "state": (inst.state or "").strip(),
                "agency": (inst.agency or "").strip(),
                "kind": kind,
                "fx": ", ".join(inst.fx_numbers or []),
                "serials": serials_text,
            }
        )

    active_job = (
        AuditJob.objects.filter(
            device_type__in=[
                AuditJob.DeviceType.CONFIRM_BATCH,
                AuditJob.DeviceType.CONFIRM_SITE,
            ],
            parent_job__isnull=True,
            status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
        )
        .order_by("-created_at")
        .first()
    )

    return render(
        request,
        "audits/confirm_captures.html",
        {
            "can_audit": can_audit,
            "q": q,
            "scope": scope,
            "sites": preview,
            "site_count": len(preview),
            "lti_count": lti_count,
            "de_count": de_count,
            "active_job": active_job,
            "recent_jobs": _confirm_recent_rows(12),
            "has_filters": bool(q or (scope and scope != "all")),
        },
    )


@login_required
@require_POST
def preview_confirm_ims_list(request):
    """Parse an IMS installations Excel export and return match preview JSON."""
    if not role_at_least(request.user, "technician"):
        return JsonResponse({"error": "forbidden"}, status=403)

    upload = request.FILES.get("file") or request.FILES.get("ims_list")
    if upload is None:
        return JsonResponse({"error": "Upload an IMS installations .xlsx file"}, status=400)
    name = (getattr(upload, "name", "") or "").lower()
    if not name.endswith((".xlsx", ".xlsm")):
        return JsonResponse({"error": "File must be an Excel .xlsx export"}, status=400)

    from services.ims_export_match import match_ims_export_to_installations

    try:
        report = match_ims_export_to_installations(upload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[CONFIRM] IMS list parse failed")
        return JsonResponse({"error": f"Could not read Excel: {exc}"}, status=400)

    rows = []
    for item in report.rows:
        rows.append(
            {
                "excel_row": item.row.excel_row,
                "sheet": item.row.source_sheet,
                "ims_id": item.row.ims_id or item.ims_id,
                "identifier": item.identifier or item.row.identifier,
                "pole": item.pole or item.row.pole,
                "serial": item.row.serial,
                "agency": item.row.agency,
                "state": item.row.state,
                "installation_id": item.installation_id,
                "kind": item.kind,
                "match_by": item.match_by,
                "status": item.status,
                "reason": item.reason,
            }
        )

    return JsonResponse(
        {
            "ok": True,
            "sheets": report.sheet_names,
            "matched_ids": report.matched_installation_ids,
            "matched_count": report.matched_count,
            "unmatched_count": report.unmatched_count,
            "ineligible_count": report.ineligible_count,
            "row_count": len(rows),
            "rows": rows,
        }
    )


@login_required
@require_POST
def start_confirm_captures(request):
    """Start confirm capture for one site or a batch of installation IDs."""
    if not role_at_least(request.user, "technician"):
        return JsonResponse({"error": "forbidden"}, status=403)

    if request.content_type and "application/json" in request.content_type:
        try:
            body = json.loads(request.body.decode() or "{}")
        except json.JSONDecodeError:
            body = {}
        raw_ids = body.get("installation_ids") or body.get("ids") or []
        installation_id = body.get("installation_id")
    else:
        raw_ids = request.POST.getlist("installation_ids")
        installation_id = request.POST.get("installation_id")

    installation_ids: list[int] = []
    if installation_id not in (None, ""):
        try:
            installation_ids.append(int(installation_id))
        except (TypeError, ValueError):
            return JsonResponse({"error": f"invalid installation_id: {installation_id!r}"}, status=400)

    if isinstance(raw_ids, str):
        raw_ids = [p for p in raw_ids.replace(";", ",").split(",") if p.strip()]
    if not isinstance(raw_ids, (list, tuple)):
        return JsonResponse({"error": "installation_ids must be a list"}, status=400)
    for x in raw_ids:
        try:
            installation_ids.append(int(x))
        except (TypeError, ValueError):
            return JsonResponse({"error": f"invalid installation id: {x!r}"}, status=400)

    seen: set[int] = set()
    installation_ids = [i for i in installation_ids if not (i in seen or seen.add(i))]
    if not installation_ids:
        return JsonResponse({"error": "Select at least one camera to confirm"}, status=400)

    running = AuditJob.objects.filter(
        device_type__in=[
            AuditJob.DeviceType.CONFIRM_BATCH,
            AuditJob.DeviceType.CONFIRM_SITE,
            AuditJob.DeviceType.VBE_DAILY_ALL,
            AuditJob.DeviceType.DST_AUDIT,
        ],
        parent_job__isnull=True,
        status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
    ).exists()
    if running:
        return JsonResponse(
            {"error": "Another fleet capture is already running (Confirm / VBE / DST)"},
            status=409,
        )

    from audits.runner import (
        _active_dst_installations,
        _dst_site_key,
        write_confirm_selection,
    )

    sites = _active_dst_installations(installation_ids=installation_ids)
    if not sites:
        return JsonResponse(
            {"error": "None of the selected sites are eligible for confirm capture"},
            status=400,
        )

    resolved_ids = [inst.pk for inst in sites]

    if len(sites) == 1:
        inst = sites[0]
        key = _dst_site_key(inst)
        job = AuditJob.objects.create(
            pole_number=key,
            target_host=_dst_site_kind_safe(inst),
            device_type=AuditJob.DeviceType.CONFIRM_SITE,
            created_by=request.user,
            progress_message=f"Queued — confirm {key}",
        )
        enqueue_audit_job(job.pk)
        logger.info(
            "[CONFIRM] start single job=%s site=%s user=%s",
            job.pk,
            key,
            request.user.get_username(),
        )
        return JsonResponse(
            {
                "ok": True,
                "job_id": job.pk,
                "sites": 1,
                "status_url": f"/audits/{job.pk}/status/",
                "detail_url": f"/audits/{job.pk}/",
                "page_url": f"/audits/confirm-captures/?job={job.pk}",
                "mode": "confirm_site",
            }
        )

    job = AuditJob.objects.create(
        pole_number="CONFIRM",
        target_host=f"0/{len(sites)}",
        device_type=AuditJob.DeviceType.CONFIRM_BATCH,
        created_by=request.user,
        progress_message=f"Queued — {len(sites)} site(s)",
    )
    write_confirm_selection(job.pk, resolved_ids)
    enqueue_audit_job(job.pk)
    logger.info(
        "[CONFIRM] start batch job=%s sites=%s user=%s",
        job.pk,
        len(sites),
        request.user.get_username(),
    )
    return JsonResponse(
        {
            "ok": True,
            "job_id": job.pk,
            "sites": len(sites),
            "status_url": f"/audits/{job.pk}/status/",
            "detail_url": f"/audits/{job.pk}/",
            "page_url": f"/audits/confirm-captures/?job={job.pk}",
            "mode": "confirm_batch",
        }
    )


def _rejection_recent_rows(limit: int = 12) -> list[dict]:
    from audits.runner import _eastern_display

    recent = (
        AuditJob.objects.filter(
            device_type=AuditJob.DeviceType.REJECTION_REPORT,
            parent_job__isnull=True,
        )
        .prefetch_related("child_jobs")
        .order_by("-created_at")[:limit]
    )
    rows: list[dict] = []
    for job in recent:
        children = list(job.child_jobs.all())
        when = job.finished_at or job.created_at
        rows.append(
            {
                "id": job.pk,
                "label": f"#{job.pk}",
                "status": job.status,
                "status_display": job.get_status_display(),
                "when_display": _eastern_display(when),
                "message": job.progress_message or job.get_status_display(),
                "site_count": len(children),
                "detail_url": f"/audits/{job.pk}/",
                "download_url": f"/audits/rejection-report/{job.pk}/download/",
            }
        )
    return rows


@login_required
def rejection_report_page(request):
    """Rejection CSV report — upload, match, run, and download the result."""
    can_audit = role_at_least(request.user, "technician")
    active_job = (
        AuditJob.objects.filter(
            device_type=AuditJob.DeviceType.REJECTION_REPORT,
            parent_job__isnull=True,
            status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
        )
        .order_by("-created_at")
        .first()
    )
    return render(
        request,
        "audits/rejection_report.html",
        {
            "can_audit": can_audit,
            "active_job": active_job,
            "active_download_url": (
                f"/audits/rejection-report/{active_job.pk}/download/"
                if active_job
                else ""
            ),
            "recent_jobs": _rejection_recent_rows(12),
        },
    )


def _read_rejection_csv_upload(request) -> tuple[str, Any]:
    upload = request.FILES.get("file") or request.FILES.get("rejection_csv")
    if upload is None:
        return "Upload a rejection .csv file", None
    name = (getattr(upload, "name", "") or "").lower()
    if not name.endswith(".csv"):
        return "File must be a .csv", None
    return "", upload


def _serialize_rejection_rows(report) -> list[dict]:
    out = []
    for item in report.rows:
        out.append(
            {
                "serial": item.row.serial,
                "count": item.row.count,
                "reason": item.row.reason,
                "source_row": item.row.source_row,
                "status": item.status,
                "installation_id": item.installation_id,
                "identifier": item.identifier,
                "pole": item.pole,
                "kind": item.kind,
                "match_by": item.match_by,
                "reason_note": item.reason,
            }
        )
    return out


@login_required
@require_POST
def preview_rejection_report(request):
    """Parse + match a rejection CSV and return a preview JSON."""
    if not role_at_least(request.user, "technician"):
        return JsonResponse({"error": "forbidden"}, status=403)

    err, upload = _read_rejection_csv_upload(request)
    if err:
        return JsonResponse({"error": err}, status=400)

    from services.rejection_csv import parse_rejection_csv
    from services.rejection_match import match_rejection_rows

    try:
        rows = parse_rejection_csv(upload.read())
        report = match_rejection_rows(rows)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[REJECTION] preview parse failed")
        return JsonResponse({"error": f"Could not read CSV: {exc}"}, status=400)

    preview_rows = []
    for item in report.rows:
        preview_rows.append(
            {
                "serial": item.row.serial,
                "count": item.row.count,
                "reason": item.row.reason,
                "status": item.status,
                "kind": item.kind,
                "identifier": item.identifier,
                "pole": item.pole,
                "match_by": item.match_by,
                "reason_note": item.reason,
            }
        )

    return JsonResponse(
        {
            "ok": True,
            "row_count": len(preview_rows),
            "matched_count": report.matched_count,
            "unmatched_count": report.unmatched_count,
            "ineligible_count": report.ineligible_count,
            "duplicate_count": report.duplicate_count,
            "rows": preview_rows,
        }
    )


@login_required
@require_POST
def start_rejection_report(request):
    """Parse the CSV again, persist rows, and enqueue the report job."""
    if not role_at_least(request.user, "technician"):
        return JsonResponse({"error": "forbidden"}, status=403)

    err, upload = _read_rejection_csv_upload(request)
    if err:
        return JsonResponse({"error": err}, status=400)

    from services.rejection_csv import parse_rejection_csv
    from services.rejection_match import match_rejection_rows

    try:
        rows = parse_rejection_csv(upload.read())
        report = match_rejection_rows(rows)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[REJECTION] start parse failed")
        return JsonResponse({"error": f"Could not read CSV: {exc}"}, status=400)

    if not report.rows:
        return JsonResponse({"error": "CSV contained no rows"}, status=400)

    running = AuditJob.objects.filter(
        device_type__in=[
            AuditJob.DeviceType.REJECTION_REPORT,
            AuditJob.DeviceType.CONFIRM_BATCH,
            AuditJob.DeviceType.CONFIRM_SITE,
            AuditJob.DeviceType.VBE_DAILY_ALL,
            AuditJob.DeviceType.DST_AUDIT,
        ],
        parent_job__isnull=True,
        status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
    ).exists()
    if running:
        return JsonResponse(
            {"error": "Another fleet capture is already running (Rejection / Confirm / VBE / DST)"},
            status=409,
        )

    from audits.runner import write_rejection_selection

    serialized = _serialize_rejection_rows(report)
    job = AuditJob.objects.create(
        pole_number="REJECTION",
        target_host=f"0/{len(serialized)}",
        device_type=AuditJob.DeviceType.REJECTION_REPORT,
        created_by=request.user,
        progress_message=f"Queued — {len(serialized)} row(s)",
    )
    write_rejection_selection(job.pk, serialized)
    enqueue_audit_job(job.pk)
    logger.info(
        "[REJECTION] start job=%s rows=%s user=%s",
        job.pk,
        len(serialized),
        request.user.get_username(),
    )
    return JsonResponse(
        {
            "ok": True,
            "job_id": job.pk,
            "rows": len(serialized),
            "status_url": f"/audits/{job.pk}/status/",
            "detail_url": f"/audits/{job.pk}/",
            "download_url": f"/audits/rejection-report/{job.pk}/download/",
        }
    )


@login_required
@require_GET
def download_rejection_report(request, pk):
    """Serve the generated .xlsx report for a finished rejection job."""
    job = get_object_or_404(AuditJob, pk=pk, device_type=AuditJob.DeviceType.REJECTION_REPORT)
    from audits.runner import _rejection_report_xlsx_path

    path = _rejection_report_xlsx_path(job.pk)
    if not path.exists():
        raise Http404("Report not ready")
    return FileResponse(
        path.open("rb"),
        as_attachment=True,
        filename=f"rejection_report_{job.pk}.xlsx",
    )


def _dst_site_kind_safe(inst) -> str:
    from audits.runner import _dst_site_kind

    return _dst_site_kind(inst) or ""
