"""One-page review of a DST audit round (any signed-in user)."""

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from audits.models import AuditJob, AuditRound, InstallationAuditReview
from audits.review import (
    DRIFT_FLAG_SECONDS,
    FILTERS,
    build_round_rows,
    filter_rows,
    summarize,
    tech_progress,
)

NOTES_MAX = 4000


@login_required
@require_GET
def review_round(request, pk: int | None = None):
    rounds = list(AuditRound.objects.order_by("-created_at"))
    current = get_object_or_404(AuditRound, pk=pk) if pk is not None else (rounds[0] if rounds else None)
    show = request.GET.get("show", "all")
    if show not in FILTERS:
        show = "all"
    rows = build_round_rows(current) if current else []
    reviewers = list(current.reviewers) if current else []
    progress = tech_progress(rows, reviewers)
    techs = [p["name"] for p in progress]
    tech = request.GET.get("tech", "")
    if tech not in {"", "mine", "unassigned", *techs}:
        tech = ""
    return render(
        request,
        "audits/review.html",
        {
            "rounds": rounds,
            "current": current,
            "rows": filter_rows(rows, show, tech, request.user.username),
            "summary": summarize(rows),
            "progress": progress,
            "reviewers": reviewers,
            "techs": techs,
            "tech": tech,
            "show": show,
            "filters": FILTERS,
            "drift_flag_minutes": DRIFT_FLAG_SECONDS // 60,
        },
    )


def _state(review: InstallationAuditReview) -> dict:
    return {
        "camera_key": review.camera_key,
        "version": review.version,
        "assigned_to": review.assigned_to,
        "completed": review.completed,
        "completed_by": review.completed_by,
        "notes": review.notes,
        "notes_updated_by": review.notes_updated_by,
    }


def _review_for(round_: AuditRound, key: str) -> InstallationAuditReview | None:
    """The camera's review row, created on first edit (cameras published before
    assignment existed have none). Only cameras that are in the round qualify."""
    review = InstallationAuditReview.objects.filter(round=round_, camera_key=key).first()
    if review is not None:
        return review
    job = (
        AuditJob.objects.filter(
            round=round_, device_type=AuditJob.DeviceType.DST_SITE, pole_number=key
        )
        .order_by("-created_at")
        .first()
    )
    if job is None:
        return None
    try:
        with transaction.atomic():
            return InstallationAuditReview.objects.create(
                round=round_, camera_key=key, kind=(job.target_host or "").strip().lower()[:8]
            )
    except IntegrityError:  # a teammate created it first
        return InstallationAuditReview.objects.get(round=round_, camera_key=key)


@login_required
@require_POST
def review_update(request, pk: int):
    """Set one field of a camera's review: ``completed``, ``notes`` or ``assigned_to``.

    Body: JSON ``{camera_key, field, value, prev?}``. Complete and assignee are
    explicit sets (last write wins, and the response carries the current state).
    Notes are compare-and-set against ``prev``, the text the editor opened with,
    so a teammate's concurrent note is never overwritten: a mismatch is a 409
    with the current state.
    """
    round_ = get_object_or_404(AuditRound, pk=pk)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "Invalid JSON."}, status=400)
    key = str(body.get("camera_key") or "").strip()
    field, value = body.get("field"), body.get("value")
    review = _review_for(round_, key) if key else None
    if review is None:
        return JsonResponse({"error": "Camera is not in this round."}, status=404)

    user, now = request.user.username, timezone.now()
    rows = InstallationAuditReview.objects.filter(pk=review.pk)
    if field == "completed":
        if not isinstance(value, bool):
            return JsonResponse({"error": "completed must be true or false."}, status=400)
        rows.update(
            completed=value,
            completed_by=user if value else "",
            completed_at=now if value else None,
            version=F("version") + 1,
        )
    elif field == "assigned_to":
        value = str(value or "").strip()
        if value and value not in round_.reviewers:
            return JsonResponse({"error": f"{value} is not a reviewer for this round."}, status=400)
        rows.update(assigned_to=value, assigned_by=user, assigned_at=now, version=F("version") + 1)
    elif field == "notes":
        value = str(value or "").replace("\r\n", "\n")
        if len(value) > NOTES_MAX:
            return JsonResponse({"error": f"Notes are limited to {NOTES_MAX} characters."}, status=400)
        prev = str(body.get("prev") or "").replace("\r\n", "\n")
        if value != review.notes:
            changed = rows.filter(notes=prev).update(
                notes=value, notes_updated_by=user, notes_updated_at=now, version=F("version") + 1
            )
            if not changed:
                review.refresh_from_db()
                return JsonResponse(
                    {
                        "error": f"{review.notes_updated_by or 'Someone'} changed these notes "
                        "while you were editing. Your text is still in the editor: Save "
                        "again to replace theirs, or Cancel to keep theirs.",
                        "state": _state(review),
                    },
                    status=409,
                )
    else:
        return JsonResponse({"error": "Unknown field."}, status=400)
    review.refresh_from_db()
    return JsonResponse({"state": _state(review)})
