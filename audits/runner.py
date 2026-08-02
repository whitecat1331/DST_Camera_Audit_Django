"""Background audit job runner with concurrency cap and live progress."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.db import close_old_connections
from django.utils import timezone

logger = logging.getLogger(__name__)

_semaphore = threading.Semaphore(getattr(settings, "AUDIT_MAX_CONCURRENT", 2))
_progress_lock = threading.Lock()
_cancel_lock = threading.Lock()
_cancel_flags: dict[int, threading.Event] = {}
_job_children: dict[int, set[int]] = {}


def enqueue_audit_job(job_id: int) -> None:
    logger.info("[AUDIT] enqueue job=%s", job_id)
    with _cancel_lock:
        _cancel_flags[job_id] = threading.Event()
    thread = threading.Thread(
        target=_run_job,
        args=(job_id,),
        name=f"audit-job-{job_id}",
        daemon=True,
    )
    thread.start()


def is_job_cancelled(job_id: int) -> bool:
    with _cancel_lock:
        ev = _cancel_flags.get(job_id)
        return bool(ev is not None and ev.is_set())


def request_job_cancel(job_id: int) -> bool:
    """Signal cancel for a pending/running job and tear down TeamViewer UI.

    Returns True when the job was active and cancel was accepted.
    """
    from audits.models import AuditJob
    from services.teamviewer_capture import cleanup_teamviewer_ui

    close_old_connections()
    try:
        job = AuditJob.objects.get(pk=job_id)
    except AuditJob.DoesNotExist:
        return False

    if job.status not in (AuditJob.Status.PENDING, AuditJob.Status.RUNNING):
        return False

    with _cancel_lock:
        ev = _cancel_flags.get(job_id)
        if ev is None:
            ev = threading.Event()
            _cancel_flags[job_id] = ev
        ev.set()

    # Also cancel live child jobs spawned by an all-sites run.
    child_ids: list[int] = []
    with _cancel_lock:
        child_ids = sorted(_job_children.get(job_id, set()))
        for child_id in child_ids:
            cev = _cancel_flags.get(child_id)
            if cev is None:
                cev = threading.Event()
                _cancel_flags[child_id] = cev
            cev.set()

    if child_ids:
        AuditJob.objects.filter(pk__in=child_ids).filter(
            status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
        ).update(
            status=AuditJob.Status.CANCELLED,
            error_message="Cancelled by user",
            progress_message="Cancelled",
            finished_at=timezone.now(),
        )

    AuditJob.objects.filter(pk=job_id).update(
        status=AuditJob.Status.CANCELLED,
        error_message="Cancelled by user",
        progress_message="Cancelling…",
    )
    try:
        closed = cleanup_teamviewer_ui()
    except Exception:  # noqa: BLE001
        logger.exception("[AUDIT] Job %s TeamViewer cleanup failed", job_id)
        closed = 0
    logger.info(
        "[AUDIT] cancel requested job=%s children=%s tv_closed=%s",
        job_id,
        child_ids,
        closed,
    )
    return True


def _clear_cancel_flag(job_id: int) -> None:
    with _cancel_lock:
        _cancel_flags.pop(job_id, None)
        _job_children.pop(job_id, None)
        for parent_id, kids in list(_job_children.items()):
            if job_id in kids:
                kids.discard(job_id)
                if not kids:
                    _job_children.pop(parent_id, None)


def _raise_if_cancelled(job_id: int) -> None:
    from services.teamviewer_capture import CaptureCancelled

    if is_job_cancelled(job_id):
        raise CaptureCancelled("Audit cancelled by user")


def reap_orphaned_audit_jobs() -> int:
    """Mark pending/running jobs as failed after a process restart.

    Audit workers are in-process daemon threads; a server restart leaves DB rows
    stuck in pending/running and blocks new VBE Checks until they are cleared.
    """
    from audits.models import AuditJob

    close_old_connections()
    now = timezone.now()
    qs = AuditJob.objects.filter(
        status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
    )
    count = qs.count()
    if not count:
        return 0
    qs.update(
        status=AuditJob.Status.FAILED,
        error_message="Interrupted by server restart",
        progress_message="Failed (orphaned)",
        finished_at=now,
    )
    logger.warning("[AUDIT] reaped %s orphaned pending/running job(s)", count)
    return count


def _set_progress(job_id: int, message: str) -> None:
    """Thread-safe progress update (reloads job row to avoid stale instances)."""
    from audits.models import AuditJob

    text = (message or "")[:255]
    with _progress_lock:
        close_old_connections()
        updated = AuditJob.objects.filter(pk=job_id).update(progress_message=text)
        if updated:
            logger.info("[AUDIT] Job %s progress: %s", job_id, text)


def _save_shot(job_id: int, filepath: Path, label: str) -> None:
    from audits.models import AuditJob, AuditScreenshot

    close_old_connections()
    job = AuditJob.objects.get(pk=job_id)
    with filepath.open("rb") as fh:
        shot = AuditScreenshot(job=job, label=label)
        shot.image.save(filepath.name, File(fh), save=True)
    logger.info("[AUDIT] Job %s saved screenshot label=%s path=%s", job_id, label, filepath.name)


def _lane2_present(pole_number: str) -> bool:
    """True when IMS inventory indicates a second TF lane."""
    from cameras.models import Installation

    close_old_connections()
    inst = (
        Installation.objects.filter(pole_number=pole_number, is_active=True)
        .only("camera_b", "tf_b_ip")
        .first()
    )
    if inst is None:
        return True  # unknown → keep legacy both-lanes behavior
    cam_b = (inst.camera_b or "").strip().lower()
    tf_b = (inst.tf_b_ip or "").strip().lower()
    if cam_b and cam_b not in {"n/a", "na", "?", "-"}:
        return True
    if tf_b and tf_b not in {"n/a", "na", "?", "-"}:
        return True
    return False


def _capture_cbw_step(
    job_id: int,
    host: str,
    output_dir: Path,
) -> tuple[str, Exception | None]:
    from services.cbw_capture import capture_cbw_with_passwords

    close_old_connections()
    t0 = time.perf_counter()
    try:
        _set_progress(job_id, f"CBW capture {host}…")
        path = capture_cbw_with_passwords(
            host=host,
            username=settings.CBW_USERNAME,
            passwords=settings.CBW_PASSWORDS,
            output_dir=output_dir,
            filename="cbw.png",
            on_progress=lambda msg: _set_progress(job_id, msg),
        )
        _save_shot(job_id, path, "cbw")
        _set_progress(job_id, "CBW screenshot saved")
        logger.info(
            "[AUDIT] Job %s CBW ok host=%s elapsed=%.1fs",
            job_id,
            host,
            time.perf_counter() - t0,
        )
        return "cbw", None
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[AUDIT] Job %s CBW failed host=%s after %.1fs",
            job_id,
            host,
            time.perf_counter() - t0,
        )
        _set_progress(job_id, f"CBW failed: {type(exc).__name__}")
        return "cbw", exc


def _capture_vnc_step(
    job_id: int,
    lane: int,
    host: str,
    label: str,
    output_dir: Path,
) -> tuple[str, Exception | None]:
    from services.vnc_capture import capture_vnc

    close_old_connections()
    t0 = time.perf_counter()
    try:
        _set_progress(job_id, f"VNC L{lane} connect {host}…")
        path = capture_vnc(
            output_dir=output_dir,
            host=host,
            password=settings.TF_VNC_PASSWORD,
            filename=f"{label}.png",
            on_progress=lambda msg: _set_progress(job_id, msg),
        )
        _save_shot(job_id, path, label)
        _set_progress(job_id, f"VNC L{lane} screenshot saved")
        logger.info(
            "[AUDIT] Job %s VNC L%s ok host=%s elapsed=%.1fs",
            job_id,
            lane,
            host,
            time.perf_counter() - t0,
        )
        return label, None
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[AUDIT] Job %s VNC L%s failed host=%s after %.1fs",
            job_id,
            lane,
            host,
            time.perf_counter() - t0,
        )
        _set_progress(job_id, f"VNC L{lane} failed: {type(exc).__name__}")
        return label, exc


def _run_pole_bundle_parallel(
    job_id: int,
    pole_number: str,
    output_dir: Path,
) -> list[str]:
    """Run CBW + each VNC lane concurrently. Returns error strings."""
    from services.ip_map import DeviceType, pole_to_ip_address

    cbw_host = pole_to_ip_address(pole_number, DeviceType.CBW)
    vnc_l1 = pole_to_ip_address(pole_number, DeviceType.TF_CPU, lane=1)
    include_l2 = _lane2_present(pole_number)
    vnc_l2 = (
        pole_to_ip_address(pole_number, DeviceType.TF_CPU, lane=2) if include_l2 else None
    )

    from audits.models import AuditJob

    hosts = [cbw_host, vnc_l1] + ([vnc_l2] if vnc_l2 else [])
    AuditJob.objects.filter(pk=job_id).update(target_host=";".join(hosts))
    logger.info(
        "[AUDIT] Job %s parallel targets cbw=%s vnc_l1=%s vnc_l2=%s",
        job_id,
        cbw_host,
        vnc_l1,
        vnc_l2 or "(skipped)",
    )

    steps: list[tuple] = [
        ("cbw", cbw_host, None),
        ("vnc_l1", vnc_l1, 1),
    ]
    if include_l2 and vnc_l2:
        steps.append(("vnc_l2", vnc_l2, 2))

    max_workers = min(
        len(steps),
        max(1, int(getattr(settings, "AUDIT_STEP_CONCURRENT", 3))),
    )
    _set_progress(
        job_id,
        f"Capturing {len(steps)} targets in parallel (workers={max_workers})…",
    )
    logger.info(
        "[AUDIT] Job %s starting %s parallel capture step(s) workers=%s",
        job_id,
        len(steps),
        max_workers,
    )

    errors: list[str] = []
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=f"audit-{job_id}",
    ) as pool:
        futures = {}
        for kind, host, lane in steps:
            if kind == "cbw":
                fut = pool.submit(_capture_cbw_step, job_id, host, output_dir)
            else:
                fut = pool.submit(
                    _capture_vnc_step,
                    job_id,
                    lane,
                    host,
                    kind,
                    output_dir,
                )
            futures[fut] = kind

        for fut in as_completed(futures):
            kind = futures[fut]
            try:
                label, exc = fut.result()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{kind}: {exc}")
                logger.exception("[AUDIT] Job %s step %s crashed", job_id, kind)
                continue
            if exc is not None:
                if label.startswith("vnc"):
                    lane_n = label.replace("vnc_l", "")
                    errors.append(f"VNC L{lane_n}: {exc}")
                else:
                    errors.append(f"CBW: {exc}")

    return errors


def _run_de_bundle(
    job_id: int,
    pole_number: str,
    output_dir: Path,
    *,
    daily_export: bool = False,
) -> list[str]:
    """Capture each DragonEye TeamViewer lane sequentially (TV is exclusive).

    When daily_export=True, also copy each PNG into the VBE Daily Checks
    shared folder tree (year / month / VBE NNNN / dated filename).
    """
    from cameras.models import Installation
    from services.dragoneye_ids import mappings_for_fx
    from services.teamviewer_capture import (
        CaptureCancelled,
        capture_dragoneye_via_teamviewer,
    )

    close_old_connections()
    _raise_if_cancelled(job_id)
    inst = (
        Installation.objects.filter(pole_number=pole_number, is_active=True)
        .order_by("-last_synced_at")
        .first()
    )
    if inst is None:
        # VBE/DE jobs may key pole_number to identifier when the parent has no pole.
        inst = (
            Installation.objects.filter(identifier=pole_number, is_active=True)
            .order_by("-last_synced_at")
            .first()
        )
    if inst is None:
        return [f"No installation for pole {pole_number}"]
    fx_list = inst.fx_numbers
    if not fx_list:
        return [f"No FX serial on installation pole={pole_number}"]

    all_mappings: list[tuple[str, object]] = []
    for fx in fx_list:
        for m in mappings_for_fx(fx):
            all_mappings.append((fx, m))
    if not all_mappings:
        joined = ", ".join(fx_list)
        return [
            f"No TeamViewer ID mapped for {joined} — upload DragonEye Teamviewer IDs.csv"
        ]

    tv_passwords = list(getattr(settings, "TEAMVIEWER_PASSWORDS", []) or [])
    cam_passwords = list(getattr(settings, "TV_CAMERA_PASSWORDS", []) or [])
    if not cam_passwords:
        # Fallback for older settings without TV_CAMERA_PASSWORDS.
        legacy = list(getattr(settings, "TV_PASSWORDS", []) or [])
        cam_passwords = [legacy[-1]] if legacy else list(tv_passwords[-1:] if tv_passwords else [])
    cam_user = getattr(settings, "TV_USERNAME", "") or ""
    tv_path = getattr(settings, "TEAMVIEWER_PATH", "") or ""

    if not tv_passwords and not cam_passwords:
        return ["TV_PASSWORD / TEAMVIEWER_PASSWORDS not configured in .env"]

    # Connection: try each TeamViewer password. Camera login: last TV_PASSWORD only.
    connect_passwords = tv_passwords or cam_passwords
    login_passwords = cam_passwords

    from audits.models import AuditJob

    hosts = [m.teamviewer_id for _, m in all_mappings]
    AuditJob.objects.filter(pk=job_id).update(target_host=";".join(hosts))
    mode_bit = "Daily Check " if daily_export else ""
    _set_progress(
        job_id,
        f"{mode_bit}DragonEye {', '.join(fx_list)}: {len(all_mappings)} TeamViewer lane(s)…",
    )

    export_id = (inst.identifier or pole_number or "").strip()
    export_location = (inst.location or "").strip()
    if daily_export:
        from services.vbe_daily_checks import resolve_vbe_export_key

        export_id = resolve_vbe_export_key(inst)
    errors: list[str] = []
    used_filenames: set[str] = set()
    for fx, m in all_mappings:
        _raise_if_cancelled(job_id)
        label = m.thumb_key
        lane_bit = m.lane or "TV"
        filename = f"{label}.png"
        if filename in used_filenames:
            # Multi-FX VBE: avoid overwriting L1/L2 thumbs from different cameras.
            filename = f"{fx.lower()}_{label}.png"
        used_filenames.add(filename)
        t0 = time.perf_counter()
        try:
            _set_progress(job_id, f"TeamViewer {fx} {lane_bit} ({m.teamviewer_id})…")
            path = capture_dragoneye_via_teamviewer(
                teamviewer_id=m.teamviewer_id,
                teamviewer_passwords=connect_passwords,
                camera_passwords=login_passwords,
                output_dir=output_dir,
                filename=filename,
                camera_username=cam_user,
                teamviewer_path=tv_path,
                on_progress=lambda msg: _set_progress(job_id, msg),
                should_cancel=lambda: is_job_cancelled(job_id),
            )
            _save_shot(job_id, path, label)
            if daily_export:
                from datetime import datetime

                from services.vbe_daily_checks import export_daily_check_png

                dest = export_daily_check_png(
                    path,
                    identifier=export_id,
                    lane=getattr(m, "lane", "") or "",
                    thumb_key=label,
                    when=datetime.now(),
                    location=export_location,
                )
                _set_progress(job_id, f"Exported {dest.name}")
            _set_progress(job_id, f"Saved {fx} {lane_bit}")
            logger.info(
                "[AUDIT] Job %s DE ok fx=%s lane=%s tv=%s daily=%s elapsed=%.1fs",
                job_id,
                fx,
                lane_bit,
                m.teamviewer_id,
                daily_export,
                time.perf_counter() - t0,
            )
        except CaptureCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "[AUDIT] Job %s DE failed fx=%s lane=%s tv=%s",
                job_id,
                fx,
                lane_bit,
                m.teamviewer_id,
            )
            errors.append(f"{fx} {lane_bit}: {exc}")
            _set_progress(job_id, f"Failed {fx} {lane_bit}: {type(exc).__name__}")
    return errors


def _run_vnc_bundle(job_id: int, pole_number: str, output_dir: Path) -> list[str]:
    """Capture VNC L1 (+ L2 when present) without CBW."""
    from services.ip_map import DeviceType, pole_to_ip_address

    vnc_l1 = pole_to_ip_address(pole_number, DeviceType.TF_CPU, lane=1)
    include_l2 = _lane2_present(pole_number)
    vnc_l2 = (
        pole_to_ip_address(pole_number, DeviceType.TF_CPU, lane=2) if include_l2 else None
    )

    from audits.models import AuditJob

    hosts = [vnc_l1] + ([vnc_l2] if vnc_l2 else [])
    AuditJob.objects.filter(pk=job_id).update(target_host=";".join(hosts))
    steps: list[tuple[str, str, int]] = [("vnc_l1", vnc_l1, 1)]
    if include_l2 and vnc_l2:
        steps.append(("vnc_l2", vnc_l2, 2))

    max_workers = min(
        len(steps),
        max(1, int(getattr(settings, "AUDIT_STEP_CONCURRENT", 3))),
    )
    _set_progress(job_id, f"VNC capturing {len(steps)} lane(s)…")
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_capture_vnc_step, job_id, lane, host, label, output_dir): label
            for label, host, lane in steps
        }
        for fut in as_completed(futures):
            label = futures[fut]
            _raise_if_cancelled(job_id)
            try:
                _, exc = fut.result()
                if exc is not None:
                    errors.append(f"{label}: {exc}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{label}: {exc}")
    return errors


def _capture_ovrc_times(job_id: int, fx_list: list[str], output_dir: Path) -> list[str]:
    """Scrape OvrC dashboard LOCAL DATE AND TIME for each FX."""
    ovrc_user = (getattr(settings, "OVRC_USERNAME", "") or "").strip()
    ovrc_pass = (getattr(settings, "OVRC_PASSWORD", "") or "").strip()
    if not ovrc_user or not ovrc_pass:
        return ["OVRC_USERNAME / OVRC_PASSWORD not configured in .env"]

    from services.ovrc_capture import capture_fx_times
    from services.teamviewer_capture import CaptureCancelled

    unique_fx = list(dict.fromkeys((fx or "").strip().upper() for fx in fx_list if fx))
    if not unique_fx:
        return ["No FX serials for OvrC capture"]

    errors: list[str] = []
    times: list[str] = []
    _set_progress(job_id, f"OvrC local time for {len(unique_fx)} FX…")
    t0 = time.perf_counter()
    try:
        results = capture_fx_times(
            unique_fx,
            username=ovrc_user,
            password=ovrc_pass,
            output_dir=output_dir,
            base_url=getattr(settings, "OVRC_BASE_URL", "") or "https://app.ovrc.com",
            headless=True,
            on_progress=lambda msg: _set_progress(job_id, msg),
        )
        for r in results:
            label = f"ovrc_{r.fx_number.lower()}"
            _save_shot(job_id, r.screenshot_path, label)
            _set_progress(job_id, f"OvrC {r.fx_number}: {r.local_time}")
            logger.info(
                "[AUDIT] Job %s OvrC ok fx=%s local_time=%s",
                job_id,
                r.fx_number,
                r.local_time,
            )
            times.append(f"{r.fx_number}: {r.local_time}")
        logger.info(
            "[AUDIT] Job %s OvrC done count=%s elapsed=%.1fs",
            job_id,
            len(results),
            time.perf_counter() - t0,
        )
        if times and not errors:
            # Keep the scraped clock(s) on the job for the UI / audit list.
            from audits.models import AuditJob

            AuditJob.objects.filter(pk=job_id).update(
                progress_message="; ".join(times)[:255]
            )
    except CaptureCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AUDIT] Job %s OvrC failed", job_id)
        errors.append(f"OvrC: {exc}")
        _set_progress(job_id, f"OvrC failed: {type(exc).__name__}")
    return errors


def _run_ovrc_only(job_id: int, pole_number: str, output_dir: Path) -> list[str]:
    """OvrC local-time screenshots for every FX on this installation."""
    from cameras.models import Installation

    close_old_connections()
    _raise_if_cancelled(job_id)
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
        return [f"No installation for {pole_number}"]
    fx_list = inst.fx_numbers
    if not fx_list:
        return [f"No FX serial on installation {pole_number}"]
    from audits.models import AuditJob

    AuditJob.objects.filter(pk=job_id).update(target_host=",".join(fx_list))
    return _capture_ovrc_times(job_id, fx_list, output_dir)


def _active_vbe_installations():
    """Canonical VBE site rows only (I-VBE-0012), not pole-baked duplicates (I-VBE-255176)."""
    from django.db.models import Q

    from cameras.models import Installation
    from services.vbe_daily_checks import is_canonical_vbe_identifier

    close_old_connections()
    rows = list(
        Installation.objects.filter(is_active=True)
        .filter(Q(primary_platform__iexact="VBE") | Q(identifier__istartswith="I-VBE-"))
        .order_by("identifier")
    )
    return [inst for inst in rows if is_canonical_vbe_identifier(inst.identifier)]


def _run_vbe_daily_all(job_id: int, output_dir: Path) -> tuple[list[str], int]:
    """Capture every active VBE site (all TeamViewer lanes) into Daily Checks folders.

    Creates a child AuditJob per site so list-card thumbs still update by identifier.
    Returns (errors, sites_ok).
    """
    from audits.models import AuditJob
    from services.teamviewer_capture import CaptureCancelled

    close_old_connections()
    _raise_if_cancelled(job_id)
    bulk = AuditJob.objects.get(pk=job_id)
    vbes = _active_vbe_installations()
    if not vbes:
        return ["No active VBE installations"], 0

    try:
        from services.vbe_daily_checks import daily_checks_root

        root = daily_checks_root()
        root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return [f"VBE Daily Checks folder: {exc}"], 0

    total = len(vbes)
    all_errors: list[str] = []
    sites_ok = 0
    _set_progress(job_id, f"VBE Daily Checks: 0/{total} sites…")

    for index, inst in enumerate(vbes, start=1):
        _raise_if_cancelled(job_id)
        key = (inst.identifier or inst.pole_number or str(inst.ims_id)).strip()
        if not key:
            all_errors.append(f"installation pk={inst.pk}: missing identifier")
            continue
        _set_progress(job_id, f"VBE Daily Checks {index}/{total}: {key}")
        child = AuditJob.objects.create(
            pole_number=key,
            target_host=inst.fx_number or "",
            device_type=AuditJob.DeviceType.VBE_DAILY,
            status=AuditJob.Status.RUNNING,
            created_by=bulk.created_by,
            started_at=timezone.now(),
            progress_message=f"Bulk job {job_id} ({index}/{total})",
        )
        with _cancel_lock:
            child_ev = threading.Event()
            _cancel_flags[child.pk] = child_ev
            _job_children.setdefault(job_id, set()).add(child.pk)
            if is_job_cancelled(job_id):
                child_ev.set()
        child_dir = output_dir / key.replace("/", "_").replace("\\", "_")
        child_dir.mkdir(parents=True, exist_ok=True)
        try:
            errors = _run_de_bundle(
                child.pk,
                key,
                child_dir,
                daily_export=True,
            )
            child.refresh_from_db()
            if child.status == AuditJob.Status.CANCELLED or is_job_cancelled(child.pk):
                raise CaptureCancelled("Audit cancelled by user")
            has_shots = child.screenshots.exists()
            if has_shots:
                sites_ok += 1
                child.status = AuditJob.Status.SUCCEEDED
                child.progress_message = (
                    f"Done with warnings ({len(errors)})" if errors else "Done"
                )
            else:
                child.status = AuditJob.Status.FAILED
                child.progress_message = "Failed"
            if errors:
                child.error_message = "; ".join(errors)[:2000]
                for err in errors:
                    all_errors.append(f"{key}: {err}")
            elif not has_shots:
                all_errors.append(f"{key}: no screenshots")
                child.error_message = "No screenshots captured"
            child.finished_at = timezone.now()
            child.save(
                update_fields=[
                    "status",
                    "error_message",
                    "progress_message",
                    "finished_at",
                    "target_host",
                ]
            )
        except CaptureCancelled:
            child.refresh_from_db()
            if child.status != AuditJob.Status.CANCELLED:
                child.status = AuditJob.Status.CANCELLED
                child.error_message = "Cancelled by user"
                child.progress_message = "Cancelled"
                child.finished_at = timezone.now()
                child.save(
                    update_fields=[
                        "status",
                        "error_message",
                        "progress_message",
                        "finished_at",
                    ]
                )
            _clear_cancel_flag(child.pk)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AUDIT] Job %s VBE daily child failed site=%s", job_id, key)
            child.status = AuditJob.Status.FAILED
            child.error_message = str(exc)[:2000]
            child.progress_message = "Failed"
            child.finished_at = timezone.now()
            child.save(
                update_fields=[
                    "status",
                    "error_message",
                    "progress_message",
                    "finished_at",
                ]
            )
            all_errors.append(f"{key}: {exc}")
        finally:
            _clear_cancel_flag(child.pk)

    _set_progress(job_id, f"VBE Daily Checks finished {sites_ok}/{total} site(s)")
    AuditJob.objects.filter(pk=job_id).update(target_host=f"{sites_ok}/{total} sites")
    if sites_ok == 0 and not all_errors:
        all_errors.append("No VBE sites captured")
    return all_errors, sites_ok


def _run_job(job_id: int) -> None:
    from audits.models import AuditJob
    from services.cbw_capture import capture_cbw_with_passwords
    from services.ip_map import DeviceType, pole_to_ip_address
    from services.teamviewer_capture import CaptureCancelled
    from services.vnc_capture import capture_vnc

    acquired = _semaphore.acquire(blocking=True)
    if not acquired:
        return

    try:
        close_old_connections()
        try:
            job = AuditJob.objects.get(pk=job_id)
        except AuditJob.DoesNotExist:
            logger.error("[AUDIT] Job %s not found", job_id)
            return

        if job.status == AuditJob.Status.CANCELLED or is_job_cancelled(job_id):
            AuditJob.objects.filter(pk=job_id).update(
                status=AuditJob.Status.CANCELLED,
                error_message="Cancelled by user",
                progress_message="Cancelled",
                finished_at=timezone.now(),
            )
            logger.info("[AUDIT] Job %s cancelled before start", job_id)
            return

        t0 = time.perf_counter()
        logger.info(
            "[AUDIT] Job %s running type=%s pole=%s host=%s",
            job_id,
            job.device_type,
            job.pole_number,
            job.target_host,
        )
        started = AuditJob.objects.filter(
            pk=job_id,
            status=AuditJob.Status.PENDING,
        ).update(
            status=AuditJob.Status.RUNNING,
            started_at=timezone.now(),
            error_message="",
            progress_message="Starting…",
        )
        if not started:
            job.refresh_from_db()
            if job.status == AuditJob.Status.CANCELLED or is_job_cancelled(job_id):
                AuditJob.objects.filter(pk=job_id).update(
                    status=AuditJob.Status.CANCELLED,
                    error_message="Cancelled by user",
                    progress_message="Cancelled",
                    finished_at=timezone.now(),
                )
                logger.info("[AUDIT] Job %s cancelled before start", job_id)
                return
            if job.status != AuditJob.Status.RUNNING:
                logger.warning(
                    "[AUDIT] Job %s unexpected status=%s at start",
                    job_id,
                    job.status,
                )
                return
        job.refresh_from_db()

        media_root = Path(settings.MEDIA_ROOT)
        output_dir = media_root / "audits" / str(job.pk)
        output_dir.mkdir(parents=True, exist_ok=True)

        def _finalize_cancelled() -> None:
            AuditJob.objects.filter(pk=job_id).update(
                status=AuditJob.Status.CANCELLED,
                error_message="Cancelled by user",
                progress_message="Cancelled",
                finished_at=timezone.now(),
            )
            logger.info(
                "[AUDIT] Job %s cancelled elapsed=%.1fs",
                job_id,
                time.perf_counter() - t0,
            )

        try:
            if job.device_type == AuditJob.DeviceType.POLE_BUNDLE:
                errors = _run_pole_bundle_parallel(job_id, job.pole_number, output_dir)
                job.refresh_from_db()
                if job.status == AuditJob.Status.CANCELLED or is_job_cancelled(job_id):
                    _finalize_cancelled()
                    return
                if not job.screenshots.exists():
                    raise RuntimeError("; ".join(errors) or "No screenshots captured")
                job.status = AuditJob.Status.SUCCEEDED
                if errors:
                    job.error_message = "; ".join(errors)[:2000]
                job.progress_message = (
                    f"Done with warnings ({len(errors)})" if errors else "Done"
                )
                job.finished_at = timezone.now()
                job.save(
                    update_fields=[
                        "status",
                        "error_message",
                        "progress_message",
                        "finished_at",
                    ]
                )
                logger.info(
                    "[AUDIT] Job %s pole bundle finished shots=%s errors=%s elapsed=%.1fs",
                    job_id,
                    job.screenshots.count(),
                    len(errors),
                    time.perf_counter() - t0,
                )
                return

            if job.device_type == AuditJob.DeviceType.VNC_BUNDLE:
                errors = _run_vnc_bundle(job_id, job.pole_number, output_dir)
                job.refresh_from_db()
                if job.status == AuditJob.Status.CANCELLED or is_job_cancelled(job_id):
                    _finalize_cancelled()
                    return
                if not job.screenshots.exists():
                    raise RuntimeError("; ".join(errors) or "No VNC screenshots")
                job.status = AuditJob.Status.SUCCEEDED
                if errors:
                    job.error_message = "; ".join(errors)[:2000]
                job.progress_message = (
                    f"Done with warnings ({len(errors)})" if errors else "Done"
                )
                job.finished_at = timezone.now()
                job.save(
                    update_fields=[
                        "status",
                        "error_message",
                        "progress_message",
                        "finished_at",
                    ]
                )
                logger.info(
                    "[AUDIT] Job %s VNC bundle finished shots=%s errors=%s elapsed=%.1fs",
                    job_id,
                    job.screenshots.count(),
                    len(errors),
                    time.perf_counter() - t0,
                )
                return

            if job.device_type == AuditJob.DeviceType.OVRC:
                errors = _run_ovrc_only(job_id, job.pole_number, output_dir)
                job.refresh_from_db()
                if job.status == AuditJob.Status.CANCELLED or is_job_cancelled(job_id):
                    _finalize_cancelled()
                    return
                if not job.screenshots.exists():
                    raise RuntimeError("; ".join(errors) or "No OvrC screenshots")
                job.status = AuditJob.Status.SUCCEEDED
                if errors:
                    job.error_message = "; ".join(errors)[:2000]
                # Prefer scraped clock text left by _capture_ovrc_times.
                if errors:
                    job.progress_message = f"Done with warnings ({len(errors)})"
                elif not (job.progress_message or "").strip() or job.progress_message in {
                    "Done",
                    "Starting…",
                }:
                    job.progress_message = "Done"
                job.finished_at = timezone.now()
                job.save(
                    update_fields=[
                        "status",
                        "error_message",
                        "progress_message",
                        "finished_at",
                    ]
                )
                logger.info(
                    "[AUDIT] Job %s OvrC finished shots=%s errors=%s elapsed=%.1fs",
                    job_id,
                    job.screenshots.count(),
                    len(errors),
                    time.perf_counter() - t0,
                )
                return

            if job.device_type == AuditJob.DeviceType.VBE_DAILY_ALL:
                errors, sites_ok = _run_vbe_daily_all(job_id, output_dir)
                job.refresh_from_db()
                if job.status == AuditJob.Status.CANCELLED or is_job_cancelled(job_id):
                    _finalize_cancelled()
                    return
                if sites_ok == 0:
                    raise RuntimeError("; ".join(errors) or "VBE Daily Checks failed")
                job.status = AuditJob.Status.SUCCEEDED
                if errors:
                    job.error_message = "; ".join(errors)[:2000]
                job.progress_message = (
                    f"Done with warnings ({len(errors)})" if errors else "Done"
                )
                job.finished_at = timezone.now()
                job.save(
                    update_fields=[
                        "status",
                        "error_message",
                        "progress_message",
                        "finished_at",
                        "target_host",
                    ]
                )
                logger.info(
                    "[AUDIT] Job %s VBE daily-all finished sites_ok=%s errors=%s elapsed=%.1fs",
                    job_id,
                    sites_ok,
                    len(errors),
                    time.perf_counter() - t0,
                )
                return

            if job.device_type in (
                AuditJob.DeviceType.DE_BUNDLE,
                AuditJob.DeviceType.DE_TV,
                AuditJob.DeviceType.VBE_DAILY,
            ):
                errors = _run_de_bundle(
                    job_id,
                    job.pole_number,
                    output_dir,
                    daily_export=job.device_type == AuditJob.DeviceType.VBE_DAILY,
                )
                job.refresh_from_db()
                if job.status == AuditJob.Status.CANCELLED or is_job_cancelled(job_id):
                    _finalize_cancelled()
                    return
                if not job.screenshots.exists():
                    raise RuntimeError("; ".join(errors) or "No DragonEye screenshots")
                job.status = AuditJob.Status.SUCCEEDED
                if errors:
                    job.error_message = "; ".join(errors)[:2000]
                job.progress_message = (
                    f"Done with warnings ({len(errors)})" if errors else "Done"
                )
                job.finished_at = timezone.now()
                job.save(
                    update_fields=[
                        "status",
                        "error_message",
                        "progress_message",
                        "finished_at",
                    ]
                )
                logger.info(
                    "[AUDIT] Job %s DE bundle finished shots=%s errors=%s daily=%s elapsed=%.1fs",
                    job_id,
                    job.screenshots.count(),
                    len(errors),
                    job.device_type == AuditJob.DeviceType.VBE_DAILY,
                    time.perf_counter() - t0,
                )
                return

            host = job.target_host
            if not host:
                if job.device_type == AuditJob.DeviceType.CBW:
                    host = pole_to_ip_address(job.pole_number, DeviceType.CBW)
                else:
                    host = pole_to_ip_address(job.pole_number, DeviceType.TF_CPU, lane=1)
                job.target_host = host
                job.save(update_fields=["target_host"])

            def progress(msg: str) -> None:
                _set_progress(job_id, msg)

            if job.device_type == AuditJob.DeviceType.CBW:
                progress(f"CBW capture {host}…")
                filepath = capture_cbw_with_passwords(
                    host=host,
                    username=settings.CBW_USERNAME,
                    passwords=settings.CBW_PASSWORDS,
                    output_dir=output_dir,
                    filename="cbw.png",
                    on_progress=progress,
                )
                label = "cbw"
            else:
                progress(f"VNC connect {host}…")
                filepath = capture_vnc(
                    output_dir=output_dir,
                    host=host,
                    password=settings.TF_VNC_PASSWORD,
                    on_progress=progress,
                )
                label = "vnc_l1"

            _save_shot(job_id, filepath, label)
            job.status = AuditJob.Status.SUCCEEDED
            job.progress_message = "Done"
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "progress_message", "finished_at"])
            logger.info(
                "[AUDIT] Job %s succeeded elapsed=%.1fs",
                job_id,
                time.perf_counter() - t0,
            )
        except CaptureCancelled:
            _finalize_cancelled()
        except Exception as exc:  # noqa: BLE001 — persist failure on job
            job.refresh_from_db()
            if job.status == AuditJob.Status.CANCELLED or is_job_cancelled(job_id):
                _finalize_cancelled()
                return
            logger.exception(
                "[AUDIT] Job %s failed after %.1fs",
                job_id,
                time.perf_counter() - t0,
            )
            job.status = AuditJob.Status.FAILED
            job.error_message = str(exc)[:2000]
            job.progress_message = f"Failed: {type(exc).__name__}"
            job.finished_at = timezone.now()
            job.save(
                update_fields=[
                    "status",
                    "error_message",
                    "progress_message",
                    "finished_at",
                ]
            )
    finally:
        _clear_cancel_flag(job_id)
        close_old_connections()
        _semaphore.release()
