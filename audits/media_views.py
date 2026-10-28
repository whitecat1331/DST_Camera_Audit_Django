"""Serve published images from the shared DB (review instance)."""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.views.decorators.http import require_GET

from audits.models import StoredFile

# Like IMS's in-memory caches in front of the small Supabase pooler: a review
# page fires a burst of thumbnail requests, and each one used to be its own DB
# query. Published images are immutable in practice (a re-publish with new
# bytes is picked up after the TTL), so keep recent ones in process memory,
# bounded by total bytes, per gunicorn worker.
_CACHE_BYTES = int(os.environ.get("DST_MEDIA_CACHE_MB", "48")) * 1024 * 1024
_CACHE_TTL = float(os.environ.get("DST_MEDIA_CACHE_SECONDS", "600"))
_cache: OrderedDict[tuple[str, str], tuple[float, str, str, bytes]] = OrderedDict()
_cache_size = 0
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        if time.monotonic() - hit[0] > _CACHE_TTL:
            _cache_drop(key)
            return None
        _cache.move_to_end(key)
        return hit[1:]


def _cache_drop(key) -> None:
    global _cache_size
    old = _cache.pop(key, None)
    if old is not None:
        _cache_size -= len(old[3])


def _cache_put(key, sha256: str, content_type: str, payload: bytes) -> None:
    global _cache_size
    if len(payload) > _CACHE_BYTES // 4:
        return  # one huge original shouldn't evict every thumbnail
    with _cache_lock:
        _cache_drop(key)
        _cache[key] = (time.monotonic(), sha256, content_type, payload)
        _cache_size += len(payload)
        while _cache_size > _CACHE_BYTES and _cache:
            _cache_drop(next(iter(_cache)))


def _load(name: str, want_thumb: bool) -> tuple[str, str, bytes, bool] | None:
    """(sha256, content_type, payload, is_thumb) for a stored file, cached."""
    key = (name, "thumb" if want_thumb else "data")
    hit = _cache_get(key)
    if hit is not None:
        return (*hit, want_thumb)
    column = key[1]
    row = StoredFile.objects.filter(name=name).only("sha256", "content_type", column).first()
    if row is None:
        return None
    payload, is_thumb = getattr(row, column), want_thumb
    if want_thumb and not payload:
        # No thumbnail (unreadable image at publish time): serve the original.
        return _load(name, False)
    content_type = "image/jpeg" if is_thumb else row.content_type
    _cache_put(key, row.sha256, content_type, bytes(payload))
    return row.sha256, content_type, bytes(payload), is_thumb


@login_required
@require_GET
def stored_media(request, name: str):
    """Image bytes (or ``?thumb=1`` JPEG thumbnail) for a published storage name."""
    loaded = _load(name, request.GET.get("thumb") == "1")
    if loaded is None:
        raise Http404(name)
    sha256, content_type, payload, is_thumb = loaded
    etag = f'"{sha256}{"-t" if is_thumb else ""}"'
    if request.headers.get("If-None-Match") == etag:
        return HttpResponseNotModified()
    response = HttpResponse(payload, content_type=content_type)
    response["ETag"] = etag
    # Content is addressed by its hash (ETag) and never edited in place by the
    # review instance; private because it sits behind sign-in.
    response["Cache-Control"] = "private, max-age=86400"
    return response
