"""Export DragonEye Post-Install image pack (Site Photos + TeamViewer)."""

from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage

from audits.thumbs import latest_thumbs_for_poles, normalize_thumb_label

logger = logging.getLogger(__name__)

_LANE_RE = re.compile(r"^de_l(\d+)$", re.IGNORECASE)
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_ID_DE_RE = re.compile(r"(?i)\b(?:I-)?DE[-_ ]?(\d+)\b")
_FX_RE = re.compile(r"(?i)\bFX\s*(\d+)\b")

# Cache Site Documents root — SharePoint/OneDrive discovery is expensive.
_ROOT_CACHE_LOCK = threading.Lock()
_ROOT_CACHE: dict[str, object] = {"path": None, "checked_at": 0.0, "miss": False}
_ROOT_CACHE_TTL_SEC = 300.0


def _pole_keys(pole_number: str, identifier: str = "") -> list[str]:
    keys: list[str] = []
    for raw in (pole_number, identifier):
        key = (raw or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _zip_name_for_label(label: str, src_path: str) -> str:
    ext = Path(src_path).suffix.lower()
    if ext not in _IMAGE_EXTS:
        ext = ".png"
    return f"{label.lower()}{ext}"


def collect_de_tv_shots_for_poles(poles: list[str]) -> dict[str, object]:
    """Return {label: AuditScreenshot} for DE TV lane thumbs across pole keys."""
    thumbs = latest_thumbs_for_poles(poles)
    by_label: dict[str, object] = {}
    for pole in poles:
        for label, shot in (thumbs.get(pole) or {}).items():
            key = normalize_thumb_label(shot) or (label or "").strip().lower()
            if not key:
                continue
            if key.startswith("de_l") or key == "de_tv":
                if key not in by_label:
                    by_label[key] = shot

    if "de_l1" not in by_label and "de_tv" in by_label:
        by_label["de_l1"] = by_label["de_tv"]

    lane_items = {k: v for k, v in by_label.items() if _LANE_RE.match(k)}
    if lane_items:
        return dict(
            sorted(lane_items.items(), key=lambda kv: int(_LANE_RE.match(kv[0]).group(1)))
        )
    if "de_tv" in by_label:
        return {"de_tv": by_label["de_tv"]}
    return {}


def _logical_site_documents_drive_candidates() -> list[Path]:
    """Return X:/Site Documents only for drives that exist (avoid hanging on missing letters)."""
    out: list[Path] = []
    try:
        import ctypes

        bitmask = int(ctypes.windll.kernel32.GetLogicalDrives())  # type: ignore[attr-defined]
        for i in range(26):
            if bitmask & (1 << i):
                out.append(Path(f"{chr(ord('A') + i)}:/Site Documents"))
    except Exception:
        out.append(Path("C:/Site Documents"))
    return out


def _is_dir_fast(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _known_site_documents_candidates(home: Path) -> list[Path]:
    """High-probability paths first — no recursive walks."""
    return [
        home
        / "Blue Line Solutions, LLC"
        / "PSCU Production - Documents"
        / "Site Documents",
        home
        / "Blue Line Solutions"
        / "PSCU Production - Documents"
        / "Site Documents",
        home / "Site Documents",
        home / "Documents" / "Site Documents",
        home / "Desktop" / "Site Documents",
        home / "OneDrive" / "Site Documents",
        home / "OneDrive - Blue Line Solutions" / "Site Documents",
        home / "OneDrive - Blue Line Solutions, LLC" / "Site Documents",
        *_logical_site_documents_drive_candidates(),
    ]


def _shallow_find_site_documents(root: Path, max_depth: int = 3) -> Path | None:
    """Breadth-first search for a 'Site Documents' folder (no Path.rglob)."""
    if max_depth < 0:
        return None
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        current, depth = queue.pop(0)
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    name = entry.name
                    if name.startswith("."):
                        continue
                    child = Path(entry.path)
                    if name.lower() == "site documents":
                        return child
                    if depth < max_depth:
                        queue.append((child, depth + 1))
        except OSError:
            continue
    return None


def clear_site_documents_root_cache() -> None:
    with _ROOT_CACHE_LOCK:
        _ROOT_CACHE["path"] = None
        _ROOT_CACHE["checked_at"] = 0.0
        _ROOT_CACHE["miss"] = False


def discover_site_documents_root(*, use_cache: bool = True) -> Path | None:
    """Find a Site Documents folder (SharePoint sync / mapped drive / OneDrive)."""
    configured = (
        (getattr(settings, "DE_PI_SITE_DOCUMENTS_ROOT", None) or "").strip()
        or (os.environ.get("DE_PI_SITE_DOCUMENTS_ROOT") or "").strip()
    )
    if configured:
        p = Path(configured)
        if _is_dir_fast(p):
            return p
        logger.warning(
            "[DE] DE_PI_SITE_DOCUMENTS_ROOT is set but not a directory: %s",
            configured,
        )
        return None

    now = time.monotonic()
    if use_cache:
        with _ROOT_CACHE_LOCK:
            age = now - float(_ROOT_CACHE["checked_at"] or 0.0)
            if age < _ROOT_CACHE_TTL_SEC:
                if _ROOT_CACHE["miss"]:
                    return None
                cached = _ROOT_CACHE["path"]
                if cached is not None and _is_dir_fast(Path(str(cached))):
                    return Path(str(cached))

    home = Path.home()
    for cand in _known_site_documents_candidates(home):
        if _is_dir_fast(cand):
            with _ROOT_CACHE_LOCK:
                _ROOT_CACHE["path"] = str(cand)
                _ROOT_CACHE["checked_at"] = now
                _ROOT_CACHE["miss"] = False
            return cand

    # Fallback: shallow BFS under company / OneDrive sync roots only.
    scan_roots: list[Path] = []
    try:
        with os.scandir(home) as it:
            for entry in it:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                name = entry.name.lower()
                if name.startswith("onedrive") or "blue line" in name:
                    scan_roots.append(Path(entry.path))
    except OSError:
        pass

    for root in scan_roots:
        found = _shallow_find_site_documents(root, max_depth=3)
        if found is not None:
            with _ROOT_CACHE_LOCK:
                _ROOT_CACHE["path"] = str(found)
                _ROOT_CACHE["checked_at"] = now
                _ROOT_CACHE["miss"] = False
            return found

    with _ROOT_CACHE_LOCK:
        _ROOT_CACHE["path"] = None
        _ROOT_CACHE["checked_at"] = now
        _ROOT_CACHE["miss"] = True
    return None


def pole_folder_candidates(pole_number: str, identifier: str = "", fx_number: str = "") -> list[str]:
    out: list[str] = []

    def add(s: str) -> None:
        s = (s or "").strip()
        if not s:
            return
        for e in out:
            if e.lower() == s.lower():
                return
        out.append(s)

    for src in (pole_number, identifier):
        m = _ID_DE_RE.search(src or "")
        if m:
            add(m.group(1))
    add(pole_number)
    add(identifier)
    for src in (fx_number, pole_number, identifier):
        m = _FX_RE.search(src or "")
        if m:
            add(f"FX{m.group(1)}")
            add(f"FX {m.group(1)}")
    return out


def find_pole_site_documents_dir(
    pole_number: str,
    identifier: str = "",
    fx_number: str = "",
) -> Path | None:
    root = discover_site_documents_root()
    if root is None:
        return None
    # Direct candidate lookups only — do not scan the whole Site Documents tree
    # (thousands of pole folders; Windows paths are case-insensitive anyway).
    for cand in pole_folder_candidates(pole_number, identifier, fx_number):
        p = root / cand
        if _is_dir_fast(p):
            return p
    return None


def _list_images(dir_path: Path) -> list[Path]:
    if not _is_dir_fast(dir_path):
        return []
    files: list[Path] = []
    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                suffix = Path(entry.name).suffix.lower()
                if suffix in _IMAGE_EXTS:
                    files.append(Path(entry.path))
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name.lower())


def _count_images(dir_path: Path) -> int:
    if not _is_dir_fast(dir_path):
        return 0
    n = 0
    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if Path(entry.name).suffix.lower() in _IMAGE_EXTS:
                    n += 1
    except OSError:
        return 0
    return n


def collect_site_photos(pole_dir: Path) -> list[Path]:
    """Return images from `{pole}/Site Photos/` only."""
    return _list_images(pole_dir / "Site Photos")


def site_documents_status(
    pole_number: str,
    identifier: str = "",
    fx_number: str = "",
) -> dict[str, object]:
    """UI status for whether Site Photos are available for this site."""
    root = discover_site_documents_root()
    if root is None:
        return {
            "root_found": False,
            "pole_found": False,
            "path": "",
            "site_photos": 0,
            "ok": False,
            "message": "Site Documents root not found (map drive / set DE_PI_SITE_DOCUMENTS_ROOT)",
        }

    pole_dir = find_pole_site_documents_dir(pole_number, identifier, fx_number)
    if pole_dir is None:
        cands = ", ".join(pole_folder_candidates(pole_number, identifier, fx_number)[:4]) or "—"
        return {
            "root_found": True,
            "pole_found": False,
            "path": str(root),
            "site_photos": 0,
            "ok": False,
            "message": f"No pole folder under Site Documents (tried {cands})",
        }

    photos_dir = pole_dir / "Site Photos"
    if not _is_dir_fast(photos_dir):
        return {
            "root_found": True,
            "pole_found": True,
            "path": str(pole_dir),
            "site_photos": 0,
            "ok": False,
            "message": "Pole folder found but Site Photos folder is missing",
        }
    photo_n = _count_images(photos_dir)
    if photo_n > 0:
        message = f"{photo_n} photo(s) in Site Photos"
    else:
        message = "Site Photos folder found but empty"
    return {
        "root_found": True,
        "pole_found": True,
        "path": str(photos_dir),
        "site_photos": photo_n,
        "ok": photo_n > 0,
        "message": message,
    }


def _unique_zip_name(used: set[str], folder: str, filename: str) -> str:
    base = Path(filename).name
    # Guard against path traversal in stored names.
    base = base.replace("\\", "_").replace("/", "_")
    if not base or base.startswith("."):
        base = "image.png"
    stem = Path(base).stem
    ext = Path(base).suffix.lower() or ".png"
    candidate = f"{folder}/{stem}{ext}"
    n = 2
    while candidate.lower() in used:
        candidate = f"{folder}/{stem}_{n}{ext}"
        n += 1
    used.add(candidate.lower())
    return candidate


def build_de_pi_pack_zip(
    *,
    pole_number: str,
    identifier: str = "",
    fx_number: str = "",
) -> tuple[bytes, list[str], dict[str, int]]:
    """Build Post-Install pack zip: Site Photos + TeamViewer lanes.

    Zip layout (for IMS):
      tv/de_l1.png …
      site_photos/<original names>

    Raises ValueError when neither TV nor Site Photos images are available.
    """
    poles = _pole_keys(pole_number, identifier)
    if not poles:
        raise ValueError("pole or identifier is required")

    shots = collect_de_tv_shots_for_poles(poles)
    pole_dir = find_pole_site_documents_dir(pole_number, identifier, fx_number)
    site_photos: list[Path] = []
    if pole_dir is not None:
        site_photos = collect_site_photos(pole_dir)

    if not shots and not site_photos:
        raise ValueError(
            "no TeamViewer captures or Site Photos found for this site"
        )

    buf = io.BytesIO()
    names: list[str] = []
    used: set[str] = set()
    counts = {"tv": 0, "site_photos": 0}

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for label, shot in shots.items():
            image = getattr(shot, "image", None)
            if image is None or not str(image):
                continue
            storage_name = str(image)
            try:
                with default_storage.open(storage_name, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                logger.warning(
                    "[DE] export open failed label=%s path=%s err=%s",
                    label,
                    storage_name,
                    type(exc).__name__,
                )
                continue
            if not data:
                continue
            entry = f"tv/{_zip_name_for_label(label, storage_name)}"
            used.add(entry.lower())
            zf.writestr(entry, data)
            names.append(entry)
            counts["tv"] += 1

        for img in site_photos:
            try:
                data = img.read_bytes()
            except OSError:
                continue
            entry = _unique_zip_name(used, "site_photos", img.name)
            zf.writestr(entry, data)
            names.append(entry)
            counts["site_photos"] += 1

    if not names:
        raise ValueError("images were found but could not be read")

    logger.info(
        "[DE] post-install pack zip poles=%s site_docs=%s tv=%s site_photos=%s",
        ",".join(poles),
        str(pole_dir) if pole_dir else "-",
        counts["tv"],
        counts["site_photos"],
    )
    return buf.getvalue(), names, counts


# Back-compat alias used by older tests / callers.
def build_de_pi_tv_zip(pole_number: str, identifier: str = "") -> tuple[bytes, list[str]]:
    data, names, _counts = build_de_pi_pack_zip(pole_number=pole_number, identifier=identifier)
    return data, names


def export_filename(pole_number: str, identifier: str = "", fx_number: str = "") -> str:
    base = (fx_number or pole_number or identifier or "site").strip()
    safe = re.sub(r"[^\w.\-]+", "_", base).strip("_") or "site"
    return f"{safe}_post_install_images.zip"
