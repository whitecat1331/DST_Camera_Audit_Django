"""GUI file explorer for audit screenshots and VBE Daily Checks."""

from __future__ import annotations

import logging

from urllib.parse import quote

from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponseBadRequest
from django.shortcuts import render
from django.views.decorators.http import require_GET

from services.capture_explorer import (
    breadcrumb,
    explorer_roots,
    get_root,
    list_directory,
    open_file,
)

logger = logging.getLogger(__name__)


@login_required
@require_GET
def capture_explorer(request):
    roots = explorer_roots()
    if not roots:
        return render(
            request,
            "captures/explorer.html",
            {
                "roots": [],
                "root_key": "",
                "rel_path": "",
                "crumbs": [("", "")],
                "entries": [],
                "error": "No capture folders configured.",
                "preview": "",
                "parent_path": "",
            },
        )

    root_key = (request.GET.get("root") or roots[0].key).strip()
    root = get_root(root_key) or roots[0]
    rel_path = (request.GET.get("path") or "").replace("\\", "/").strip("/")
    preview = (request.GET.get("preview") or "").replace("\\", "/").strip("/")
    error = ""
    entries = []
    try:
        entries = list_directory(root, rel_path)
    except FileNotFoundError:
        error = "Folder not found."
        rel_path = ""
        try:
            entries = list_directory(root, "")
        except Exception:  # noqa: BLE001
            entries = []
    except (NotADirectoryError, PermissionError, ValueError) as exc:
        error = str(exc)
        logger.warning("[EXPLORER] list failed root=%s path=%s err=%s", root.key, rel_path, exc)

    parent_path = ""
    if rel_path:
        parts = rel_path.split("/")
        parent_path = "/".join(parts[:-1])

    preview_url = ""
    if preview:
        if root.key == "audits":
            from django.conf import settings

            preview_url = f"{settings.MEDIA_URL}audits/{quote(preview, safe='/')}"
        else:
            preview_url = (
                f"/explorer/file/?root={quote(root.key)}&path={quote(preview, safe='/')}"
            )

    return render(
        request,
        "captures/explorer.html",
        {
            "roots": roots,
            "root_key": root.key,
            "root_label": root.label,
            "root_abspath": str(root.path),
            "rel_path": rel_path,
            "crumbs": breadcrumb(rel_path),
            "entries": entries,
            "error": error,
            "preview": preview,
            "preview_url": preview_url,
            "parent_path": parent_path,
            "folder_count": sum(1 for e in entries if e.is_dir),
            "image_count": sum(1 for e in entries if e.is_image),
        },
    )


@login_required
@require_GET
def capture_explorer_file(request):
    """Serve an image from an allowed explorer root (VBE shared folder, etc.)."""
    root_key = (request.GET.get("root") or "").strip()
    rel_path = (request.GET.get("path") or "").replace("\\", "/").strip("/")
    root = get_root(root_key)
    if root is None:
        raise Http404("unknown root")
    if not rel_path:
        return HttpResponseBadRequest("path required")
    try:
        path, content_type = open_file(root, rel_path)
    except FileNotFoundError as exc:
        raise Http404("file not found") from exc
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    return FileResponse(path.open("rb"), content_type=content_type, filename=path.name)
