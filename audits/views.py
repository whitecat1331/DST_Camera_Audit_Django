import json
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST
from django.utils import timezone

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
    job = get_object_or_404(
        AuditJob.objects.prefetch_related("screenshots").select_related("created_by"),
        pk=pk,
    )
    return render(request, "audits/detail.html", {"job": job})


@login_required
@require_GET
def audit_job_status(request, pk):
    job = get_object_or_404(AuditJob.objects.prefetch_related("screenshots"), pk=pk)
    shots = [
        {"label": s.label, "url": s.image.url}
        for s in job.screenshots.all()
        if s.image
    ]
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
    return JsonResponse(
        {
            "id": job.pk,
            "status": job.status,
            "message": message,
            "progress": job.progress_message,
            "error_message": job.error_message,
            "elapsed_s": elapsed_s,
            "screenshots": shots,
        }
    )


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
