"""One-page review of a DST audit round (any signed-in user)."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from audits.models import AuditRound
from audits.review import DRIFT_FLAG_SECONDS, FILTERS, build_round_rows, filter_rows, summarize


@login_required
@require_GET
def review_round(request, pk: int | None = None):
    rounds = list(AuditRound.objects.order_by("-created_at"))
    current = get_object_or_404(AuditRound, pk=pk) if pk is not None else (rounds[0] if rounds else None)
    show = request.GET.get("show", "all")
    if show not in FILTERS:
        show = "all"
    rows = build_round_rows(current) if current else []
    return render(
        request,
        "audits/review.html",
        {
            "rounds": rounds,
            "current": current,
            "rows": filter_rows(rows, show),
            "summary": summarize(rows),
            "show": show,
            "filters": FILTERS,
            "drift_flag_minutes": DRIFT_FLAG_SECONDS // 60,
        },
    )
