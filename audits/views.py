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
    """Derive phase / percent from dst_audit target_host + progress text."""
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
    elif host.startswith("capture "):
        phase = "capture"
        frac = _frac("capture ")
        if frac:
            current, total = frac
            percent = int(round(60 + (40 * current / total))) if total else 60
    elif host.startswith("settle"):
        phase = "settle"
        percent = 55 if "skip" not in host else 58
    elif "settle skipped" in msg_l or "already on" in msg_l:
        phase = "settle"
        percent = 58
    elif host.startswith("power_on "):
        phase = "power_on"
        frac = _frac("power_on ")
        if frac:
            current, total = frac
            percent = int(round(50 * current / total)) if total else 5
    elif "waiting for audit worker" in msg_l or "worker slot" in msg_l:
        phase = "queued"
        percent = 2
    elif "phase 1" in msg_l or "power" in msg_l:
        phase = "power_on"
        percent = 10
    elif "phase 2" in msg_l or "waiting" in msg_l:
        phase = "settle"
        percent = 55
    elif "phase 3" in msg_l or "captur" in msg_l:
        phase = "capture"
        percent = 65

    # Prefer the last N/M in the message (skip "Phase 3/3" → use "4/100").
    matches = re.findall(r"(\d+)\s*/\s*(\d+)", msg)
    if matches and phase in {"power_on", "capture"} and total == 0:
        try:
            msg_cur, msg_tot = int(matches[-1][0]), int(matches[-1][1])
            if msg_tot > 0:
                current, total = msg_cur, msg_tot
                if phase == "capture":
                    percent = int(round(60 + (40 * current / total)))
                else:
                    percent = int(round(50 * current / total))
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
    """Start a fleet DST audit (power-on → settle → capture with timestamps)."""
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
