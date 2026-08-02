"""Safe filesystem browsing for audit screenshots and VBE Daily Checks."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.conf import settings

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


@dataclass(frozen=True)
class ExplorerRoot:
    key: str
    label: str
    path: Path


@dataclass
class ExplorerEntry:
    name: str
    rel_path: str
    is_dir: bool
    is_image: bool
    size: int
    modified: datetime | None
    media_url: str | None = None
    meta: str = ""


def explorer_roots() -> list[ExplorerRoot]:
    roots: list[ExplorerRoot] = []
    audits = Path(settings.MEDIA_ROOT) / "audits"
    audits.mkdir(parents=True, exist_ok=True)
    roots.append(ExplorerRoot(key="audits", label="Audit screenshots", path=audits.resolve()))

    raw = (getattr(settings, "VBE_DAILY_CHECKS_ROOT", "") or "").strip()
    if raw:
        vbe = Path(raw)
        try:
            if vbe.exists():
                roots.append(
                    ExplorerRoot(key="vbe", label="VBE Daily Checks", path=vbe.resolve())
                )
        except OSError:
            pass
    return roots


def get_root(key: str) -> ExplorerRoot | None:
    for root in explorer_roots():
        if root.key == key:
            return root
    return None


def safe_join(root: ExplorerRoot, rel_path: str) -> Path:
    """Resolve rel_path under root; raise ValueError on escape attempts."""
    rel = (rel_path or "").replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p and p not in (".", "..")]
    candidate = root.path.joinpath(*parts).resolve()
    try:
        candidate.relative_to(root.path)
    except ValueError as exc:
        raise ValueError("path escapes root") from exc
    return candidate


def rel_of(root: ExplorerRoot, absolute: Path) -> str:
    try:
        return absolute.resolve().relative_to(root.path).as_posix()
    except ValueError:
        return ""


def breadcrumb(rel_path: str) -> list[tuple[str, str]]:
    """Return [(label, rel_path), ...] including root as ('', '')."""
    crumbs: list[tuple[str, str]] = [("", "")]
    parts = [p for p in (rel_path or "").replace("\\", "/").split("/") if p]
    acc: list[str] = []
    for part in parts:
        acc.append(part)
        crumbs.append((part, "/".join(acc)))
    return crumbs


def _audit_job_meta(job_id_name: str) -> str:
    if not job_id_name.isdigit():
        return ""
    try:
        from audits.models import AuditJob

        job = AuditJob.objects.filter(pk=int(job_id_name)).only(
            "pole_number", "device_type", "status", "finished_at"
        ).first()
    except Exception:  # noqa: BLE001
        return ""
    if job is None:
        return ""
    bits = [
        job.pole_number or "—",
        job.get_device_type_display(),
        job.get_status_display(),
    ]
    if job.finished_at:
        bits.append(job.finished_at.strftime("%Y-%m-%d %H:%M"))
    return " · ".join(bits)


def list_directory(root: ExplorerRoot, rel_path: str = "") -> list[ExplorerEntry]:
    target = safe_join(root, rel_path)
    if not target.exists():
        raise FileNotFoundError(str(target))
    if not target.is_dir():
        raise NotADirectoryError(str(target))

    entries: list[ExplorerEntry] = []
    try:
        children = list(target.iterdir())
    except OSError as exc:
        raise PermissionError(str(exc)) from exc

    for child in children:
        try:
            st = child.stat()
            modified = datetime.fromtimestamp(st.st_mtime)
            size = st.st_size
        except OSError:
            modified = None
            size = 0
        is_dir = child.is_dir()
        is_image = (not is_dir) and child.suffix.lower() in IMAGE_SUFFIXES
        child_rel = rel_of(root, child)
        media_url = None
        if is_image and root.key == "audits":
            media_url = f"{settings.MEDIA_URL}audits/{child_rel}"
        meta = ""
        if is_dir and root.key == "audits" and not rel_path:
            meta = _audit_job_meta(child.name)
        entries.append(
            ExplorerEntry(
                name=child.name,
                rel_path=child_rel,
                is_dir=is_dir,
                is_image=is_image,
                size=size,
                modified=modified,
                media_url=media_url,
                meta=meta,
            )
        )

    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return entries


def open_file(root: ExplorerRoot, rel_path: str) -> tuple[Path, str]:
    """Return (absolute path, content_type) for a readable image/file under root."""
    target = safe_join(root, rel_path)
    if not target.is_file():
        raise FileNotFoundError(str(target))
    if target.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError("only image files can be previewed")
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return target, content_type
