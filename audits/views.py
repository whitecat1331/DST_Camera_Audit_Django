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
from audits.runner import enqueue_audit_job
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
    """Start LTI (CBW+VNC) or DragonEye (TeamViewer) capture for a pole."""
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

    inst = (
        Installation.objects.filter(pole_number=pole_number, is_active=True)
        .order_by("-last_synced_at")
        .first()
    )

    if inst is not None and inst.is_dragoneye:
        fx = inst.fx_number or ""
        job = AuditJob.objects.create(
            pole_number=pole_number,
            target_host=fx,
            device_type=AuditJob.DeviceType.DE_BUNDLE,
            created_by=request.user,
        )
        enqueue_audit_job(job.pk)
        logger.info(
            "[AUDIT] capture-de job=%s pole=%s fx=%s user=%s",
            job.pk,
            pole_number,
            fx,
            request.user.get_username(),
        )
        return JsonResponse(
            {
                "ok": True,
                "job_id": job.pk,
                "status_url": f"/audits/{job.pk}/status/",
                "detail_url": f"/audits/{job.pk}/",
                "mode": "dragoneye",
            }
        )

    try:
        host = pole_to_ip_address(pole_number, DeviceType.CBW)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    job = AuditJob.objects.create(
        pole_number=pole_number,
        target_host=host,
        device_type=AuditJob.DeviceType.POLE_BUNDLE,
        created_by=request.user,
    )
    enqueue_audit_job(job.pk)
    logger.info(
        "[AUDIT] capture-pole job=%s pole=%s host=%s user=%s",
        job.pk,
        pole_number,
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
        relays = turn_all_relays_on(
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
        "[AUDIT] relays on pole=%s relays=%s user=%s",
        pole_number,
        relays,
        request.user.get_username(),
    )
    return JsonResponse({"ok": True, "pole": pole_number, "relays": relays})
