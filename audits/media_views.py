"""Serve published images from the shared DB (review instance)."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.views.decorators.http import require_GET

from audits.models import StoredFile


@login_required
@require_GET
def stored_media(request, name: str):
    """Image bytes (or ``?thumb=1`` JPEG thumbnail) for a published storage name."""
    want_thumb = request.GET.get("thumb") == "1"
    column = "thumb" if want_thumb else "data"
    row = (
        StoredFile.objects.filter(name=name)
        .only("sha256", "content_type", column)
        .first()
    )
    if row is None:
        raise Http404(name)
    payload = getattr(row, column)
    if want_thumb and not payload:
        # No thumbnail (unreadable image at publish time) — fall back to the original.
        row = StoredFile.objects.filter(pk=row.pk).only("sha256", "content_type", "data").first()
        payload, want_thumb = row.data, False

    etag = f'"{row.sha256}{"-t" if want_thumb else ""}"'
    if request.headers.get("If-None-Match") == etag:
        return HttpResponseNotModified()
    response = HttpResponse(
        bytes(payload), content_type="image/jpeg" if want_thumb else row.content_type
    )
    response["ETag"] = etag
    # Content is addressed by its hash (ETag) and never edited in place by the
    # review instance; private because it sits behind sign-in.
    response["Cache-Control"] = "private, max-age=86400"
    return response
