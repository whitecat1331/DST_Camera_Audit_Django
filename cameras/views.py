from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib import messages
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from django.utils.safestring import mark_safe
import logging

import folium
from folium.plugins import MarkerCluster

from audits.models import AuditJob
from cameras.models import DragonEyeTeamViewerId, Installation, SyncState
from cameras.roles import require_ims_role, role_at_least
from services.dragoneye_ids import ensure_default_csv_loaded, replace_mappings_from_csv
from services.ims_client import check_ims_connection
from services.ims_sync import sync_installations_from_ims

logger = logging.getLogger(__name__)


@login_required
def dashboard(request):
    recent_jobs = AuditJob.objects.select_related("created_by")[:10]
    last_sync = SyncState.objects.filter(key="last_ims_sync").first()
    sync_counts = SyncState.objects.filter(key="last_ims_sync_counts").first()
    ims_status = check_ims_connection()
    seeded = ensure_default_csv_loaded(settings.BASE_DIR)
    if seeded:
        messages.info(
            request,
            f"Loaded DragonEye TeamViewer IDs from project CSV ({seeded['count']} rows).",
        )
    tv_count = DragonEyeTeamViewerId.objects.count()
    tv_source = (
        DragonEyeTeamViewerId.objects.order_by("-updated_at")
        .values_list("source_filename", flat=True)
        .first()
        or ""
    )
    context = {
        "installation_count": Installation.objects.filter(is_active=True).count(),
        "mapped_count": Installation.objects.filter(is_active=True)
        .exclude(gps_lat__isnull=True)
        .exclude(gps_long__isnull=True)
        .count(),
        "job_count": AuditJob.objects.count(),
        "running_count": AuditJob.objects.filter(
            status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING]
        ).count(),
        "ims_status": ims_status,
        "recent_jobs": recent_jobs,
        "last_sync": last_sync.value if last_sync else None,
        "sync_counts": sync_counts.value if sync_counts else None,
        "can_sync": role_at_least(request.user, "admin"),
        "can_audit": role_at_least(request.user, "technician"),
        "dragoneye_tv_count": tv_count,
        "dragoneye_tv_source": tv_source,
    }
    return render(request, "cameras/dashboard.html", context)


@login_required
@require_POST
def upload_dragoneye_tv_csv(request):
    """Replace FX → TeamViewer ID mappings from uploaded CSV."""
    if not role_at_least(request.user, "technician"):
        messages.error(request, "Forbidden")
        return redirect("dashboard")

    upload = request.FILES.get("csv_file")
    if upload is None:
        messages.error(request, "Choose a DragonEye Teamviewer IDs.csv file")
        return redirect("dashboard")
    name = (upload.name or "").lower()
    if not name.endswith(".csv"):
        messages.error(request, "File must be a .csv")
        return redirect("dashboard")

    try:
        raw = upload.read()
        result = replace_mappings_from_csv(raw, source_filename=upload.name)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[DE] CSV upload failed")
        messages.error(request, f"CSV import failed: {type(exc).__name__}")
        return redirect("dashboard")

    logger.info(
        "[DE] CSV uploaded count=%s user=%s file=%s",
        result["count"],
        request.user.get_username(),
        upload.name,
    )
    messages.success(
        request,
        f"Imported {result['count']} TeamViewer IDs ({result['with_lane']} with lane).",
    )
    return redirect("dashboard")


@login_required
@ensure_csrf_cookie
def camera_list(request):
    qs = Installation.objects.filter(is_active=True)
    q = request.GET.get("q", "").strip()
    state = request.GET.get("state", "").strip()
    agency = request.GET.get("agency", "").strip()
    status = request.GET.get("status", "").strip()
    platform = request.GET.get("platform", "").strip()
    sort = request.GET.get("sort", "pole").strip() or "pole"
    try:
        page_size = int(request.GET.get("page_size", "50"))
    except ValueError:
        page_size = 50
    if page_size not in (25, 50, 100):
        page_size = 50

    if q:
        qs = qs.filter(
            Q(serial_number__icontains=q)
            | Q(identifier__icontains=q)
            | Q(pole_number__icontains=q)
            | Q(location__icontains=q)
            | Q(fl_number__icontains=q)
            | Q(ip_address__icontains=q)
        )
    if state:
        qs = qs.filter(state=state)
    if agency:
        qs = qs.filter(agency=agency)
    if status:
        qs = qs.filter(status=status)
    if platform:
        qs = qs.filter(
            Q(primary_platform=platform) | Q(all_platforms__icontains=platform)
        )

    sort_map = {
        "pole": ("pole_number", "identifier"),
        "identifier": ("identifier", "pole_number"),
        "platform": ("primary_platform", "pole_number"),
        "agency": ("agency", "pole_number"),
        "status": ("status", "pole_number"),
        "state": ("state", "pole_number"),
    }
    qs = qs.order_by(*sort_map.get(sort, sort_map["pole"]))
    qs = qs.prefetch_related("devices__sensors", "components")

    paginator = Paginator(qs, page_size)
    page = paginator.get_page(request.GET.get("page"))

    from audits.thumbs import latest_thumbs_for_poles
    from services.device_layers import build_device_layers

    poles = [inst.pole_number for inst in page if inst.pole_number]
    thumbs = latest_thumbs_for_poles(poles)
    for inst in page:
        inst.latest_thumbs = thumbs.get(inst.pole_number or "", {})
        inst.device_layers = build_device_layers(inst)
        for layer in inst.device_layers:
            layer.thumb = (
                inst.latest_thumbs.get(layer.thumb_key) if layer.thumb_key else None
            )

    query = request.GET.copy()
    query.pop("page", None)

    context = {
        "page": page,
        "total": paginator.count,
        "q": q,
        "state": state,
        "agency": agency,
        "status": status,
        "platform": platform,
        "sort": sort,
        "page_size": page_size,
        "querystring": query.urlencode(),
        "can_audit": role_at_least(request.user, "technician"),
        "states": Installation.objects.filter(is_active=True)
        .exclude(state="")
        .values_list("state", flat=True)
        .distinct()
        .order_by("state"),
        "agencies": Installation.objects.filter(is_active=True)
        .exclude(agency="")
        .values_list("agency", flat=True)
        .distinct()
        .order_by("agency"),
        "statuses": Installation.objects.filter(is_active=True)
        .exclude(status="")
        .values_list("status", flat=True)
        .distinct()
        .order_by("status"),
        "platforms": Installation.objects.filter(is_active=True)
        .exclude(primary_platform="")
        .values_list("primary_platform", flat=True)
        .distinct()
        .order_by("primary_platform"),
        "has_filters": bool(q or state or agency or status or platform),
    }
    return render(request, "cameras/list.html", context)


@login_required
def camera_detail(request, pk):
    installation = get_object_or_404(
        Installation.objects.prefetch_related("devices__sensors", "components"),
        pk=pk,
        is_active=True,
    )
    recent_jobs = AuditJob.objects.filter(pole_number=installation.pole_number)[:10]

    from audits.thumbs import latest_thumbs_for_poles
    from services.device_layers import build_device_layers

    thumbs = latest_thumbs_for_poles(
        [installation.pole_number] if installation.pole_number else []
    )
    installation.latest_thumbs = thumbs.get(installation.pole_number or "", {})
    device_layers = build_device_layers(installation)
    for layer in device_layers:
        layer.thumb = (
            installation.latest_thumbs.get(layer.thumb_key) if layer.thumb_key else None
        )

    ordered = list(
        Installation.objects.filter(is_active=True)
        .order_by("pole_number", "identifier", "pk")
        .values_list("pk", flat=True)
    )
    prev_pk = next_pk = None
    try:
        idx = ordered.index(installation.pk)
        if idx > 0:
            prev_pk = ordered[idx - 1]
        if idx < len(ordered) - 1:
            next_pk = ordered[idx + 1]
        position = idx + 1
    except ValueError:
        position = None

    return render(
        request,
        "cameras/detail.html",
        {
            "installation": installation,
            "recent_jobs": recent_jobs,
            "device_layers": device_layers,
            "can_audit": role_at_least(request.user, "technician"),
            "prev_pk": prev_pk,
            "next_pk": next_pk,
            "nav_position": position,
            "nav_total": len(ordered),
        },
    )


@login_required
def camera_map(request):
    installations = (
        Installation.objects.filter(is_active=True)
        .exclude(gps_lat__isnull=True)
        .exclude(gps_long__isnull=True)
    )
    fmap = folium.Map(location=[39.5, -98.35], zoom_start=4)
    cluster = MarkerCluster().add_to(fmap)

    for inst in installations:
        label = inst.identifier or inst.serial_number or str(inst.ims_id)
        popup = (
            f"<b>{label}</b><br>"
            f"Pole: {inst.pole_number}<br>"
            f"{inst.agency} / {inst.state}<br>"
            f"{inst.location}<br>"
            f'<a href="/cameras/{inst.pk}/">Details</a>'
        )
        folium.Marker(
            location=[inst.gps_lat, inst.gps_long],
            popup=folium.Popup(popup, max_width=280),
            tooltip=f"{inst.pole_number} — {label}",
        ).add_to(cluster)

    map_html = mark_safe(fmap._repr_html_())
    return render(
        request,
        "cameras/map.html",
        {"map_html": map_html, "count": installations.count()},
    )


@require_ims_role("admin")
@require_POST
def sync_installations_view(request):
    logger.info("[SYNC] dashboard sync requested by user=%s", request.user.get_username())
    try:
        created, updated, deactivated = sync_installations_from_ims()
        messages.success(
            request,
            f"IMS sync complete: {created} created, {updated} updated, {deactivated} deactivated",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[SYNC] dashboard sync failed user=%s", request.user.get_username())
        messages.error(request, f"IMS sync failed: {exc}")
    return redirect("dashboard")
