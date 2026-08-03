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
_cancel_lock = threading.RLock()  # RLock: child registration may nest is_job_cancelled()
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


def _set_progress(job_id: int, message: str, *, mirror_parent: bool = True) -> None:
    """Thread-safe progress update (reloads job row to avoid stale instances).

    When mirror_parent is True and the job has a parent_job (DST site child),
    also update the parent so the DST console shows TeamViewer/OvrC steps.
    """
    from audits.models import AuditJob

    text = (message or "")[:255]
    with _progress_lock:
        close_old_connections()
        updated = AuditJob.objects.filter(pk=job_id).update(progress_message=text)
        if updated:
            logger.info("[AUDIT] Job %s progress: %s", job_id, text)
        if mirror_parent and updated:
            parent_id = (
                AuditJob.objects.filter(pk=job_id)
                .exclude(parent_job_id=None)
                .values_list("parent_job_id", flat=True)
                .first()
            )
            if parent_id:
                AuditJob.objects.filter(pk=parent_id).update(progress_message=text)


def _save_shot(job_id: int, filepath: Path, label: str) -> None:
    from audits.models import AuditJob, AuditScreenshot

    close_old_connections()
    job = AuditJob.objects.get(pk=job_id)
    with filepath.open("rb") as fh:
        shot = AuditScreenshot(job=job, label=label)
        shot.image.save(filepath.name, File(fh), save=True)
    captured_at = timezone.localtime(shot.created_at)
    logger.info(
        "[AUDIT] Job %s saved screenshot label=%s path=%s captured_at=%s",
        job_id,
        label,
        filepath.name,
        captured_at.strftime("%Y-%m-%d %H:%M:%S %Z"),
    )


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


def _dst_log(job_id: int, message: str, *args) -> None:
    """DST phase logging with immediate handler flush (helps diagnose hangs)."""
    logger.info("[DST] Job %s " + message, job_id, *args)
    for handler in logging.root.handlers:
        try:
            handler.flush()
        except Exception:  # noqa: BLE001
            pass
    for handler in logger.handlers:
        try:
            handler.flush()
        except Exception:  # noqa: BLE001
            pass


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
    _dst_log(job_id, "DE bundle begin pole=%s daily=%s dir=%s", pole_number, daily_export, output_dir)
    from cameras.models import Installation
    from services.dragoneye_ids import mappings_for_fx
    from services.teamviewer_capture import (
        CaptureCancelled,
        capture_dragoneye_via_teamviewer,
    )

    _dst_log(job_id, "DE bundle imports ok — closing old DB connections")
    close_old_connections()
    _raise_if_cancelled(job_id)
    _dst_log(job_id, "DE bundle looking up installation pole/identifier=%s", pole_number)
    t_lookup = time.perf_counter()
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
    _dst_log(
        job_id,
        "DE bundle lookup done elapsed=%.2fs found=%s",
        time.perf_counter() - t_lookup,
        bool(inst),
    )
    if inst is None:
        _dst_log(job_id, "DE bundle abort: no installation")
        return [f"No installation for pole {pole_number}"]
    _dst_log(job_id, "DE bundle reading fx_numbers for pk=%s", inst.pk)
    t_fx = time.perf_counter()
    fx_list = inst.fx_numbers
    _dst_log(
        job_id,
        "DE bundle fx_numbers=%s elapsed=%.2fs",
        fx_list,
        time.perf_counter() - t_fx,
    )
    if not fx_list:
        _dst_log(job_id, "DE bundle abort: no FX serial")
        return [f"No FX serial on installation pole={pole_number}"]

    all_mappings: list[tuple[str, object]] = []
    for fx in fx_list:
        _dst_log(job_id, "DE bundle mapping lookup fx=%s", fx)
        t_map = time.perf_counter()
        mapped = list(mappings_for_fx(fx))
        _dst_log(
            job_id,
            "DE bundle mapping fx=%s count=%s elapsed=%.2fs",
            fx,
            len(mapped),
            time.perf_counter() - t_map,
        )
        for m in mapped:
            all_mappings.append((fx, m))
    if not all_mappings:
        joined = ", ".join(fx_list)
        msg = (
            f"No TeamViewer ID mapped for {joined} — upload DragonEye Teamviewer IDs.csv"
        )
        _dst_log(job_id, "DE bundle abort: %s", msg)
        _set_progress(job_id, msg[:255])
        return [msg]

    tv_passwords = list(getattr(settings, "TEAMVIEWER_PASSWORDS", []) or [])
    cam_passwords = list(getattr(settings, "TV_CAMERA_PASSWORDS", []) or [])
    if not cam_passwords:
        # Fallback for older settings without TV_CAMERA_PASSWORDS.
        legacy = list(getattr(settings, "TV_PASSWORDS", []) or [])
        cam_passwords = [legacy[-1]] if legacy else list(tv_passwords[-1:] if tv_passwords else [])
    cam_user = getattr(settings, "TV_USERNAME", "") or ""
    tv_path = getattr(settings, "TEAMVIEWER_PATH", "") or ""

    if not tv_passwords and not cam_passwords:
        _dst_log(job_id, "DE bundle abort: no TV passwords configured")
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
    _dst_log(
        job_id,
        "DE bundle starting %s lane(s) tv_ids=%s",
        len(all_mappings),
        hosts,
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
            _dst_log(
                job_id,
                "DE lane start fx=%s lane=%s tv=%s file=%s",
                fx,
                lane_bit,
                m.teamviewer_id,
                filename,
            )
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
            _dst_log(
                job_id,
                "DE lane ok fx=%s lane=%s tv=%s elapsed=%.1fs",
                fx,
                lane_bit,
                m.teamviewer_id,
                time.perf_counter() - t0,
            )
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
            _dst_log(job_id, "DE lane cancelled fx=%s lane=%s", fx, lane_bit)
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
            _dst_log(
                job_id,
                "DE lane failed fx=%s lane=%s err=%s",
                fx,
                lane_bit,
                type(exc).__name__,
            )
    _dst_log(job_id, "DE bundle finished errors=%s", len(errors))
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


def _eastern_display(dt) -> str:
    """Format a datetime in US/Eastern wall time with a fixed EST label."""
    from zoneinfo import ZoneInfo

    from django.utils.formats import date_format

    if dt is None:
        return ""
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    eastern = dt.astimezone(ZoneInfo("America/New_York"))
    return f"{date_format(eastern, 'M j, Y, g:i a')} EST"


def _ovrc_local_display(raw: str) -> str:
    """Normalize whitespace on an OvrC LOCAL DATE AND TIME scrape; keep its zone.

    Example: 'Sat, Aug 01, 2026 11:47 PM EDT' → same clock + abbreviation from OvrC.
    Do not convert to Eastern — the zone label is part of the DST audit signal.
    """
    return " ".join((raw or "").split())


def _format_ovrc_timestamp_line(fx: str, local_time_raw: str) -> str:
    """Single canonical OvrC timestamp line stored on the site job."""
    fx_bit = (fx or "").strip().upper() or "FX"
    display = _ovrc_local_display(local_time_raw)
    return f"OvrC {fx_bit}: {display}"


def _parse_ovrc_timestamp_from_progress(progress: str) -> str:
    """Extract a stored OvrC timestamp line from progress_message, if present."""
    text = (progress or "").strip()
    if not text:
        return ""
    if text.startswith("OvrC "):
        # May be "OvrC FX: …" or previously converted "… EST"
        line = text.split(" · ")[0].strip()
        # Legacy converted lines: "OvrC FX1261: Aug 1, 2026, 11:47 p.m. EST"
        # Prefer re-hydrating from the raw scrape if we still have Done · ADT form.
        return line
    if "·" in text:
        tail = text.split("·", 1)[1].strip()
        if ":" in tail:
            fx, _, rest = tail.partition(":")
            fx = fx.strip()
            rest = rest.strip()
            if fx.upper().startswith("FX") and rest:
                return _format_ovrc_timestamp_line(fx, rest)
    if text.upper().startswith("FX") and ":" in text:
        fx, _, rest = text.partition(":")
        return _format_ovrc_timestamp_line(fx.strip(), rest.strip())
    return ""


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
            line = _format_ovrc_timestamp_line(r.fx_number, r.local_time)
            _set_progress(job_id, line)
            logger.info(
                "[AUDIT] Job %s OvrC ok fx=%s local_time=%s est=%s",
                job_id,
                r.fx_number,
                r.local_time,
                line,
            )
            times.append(line)
        logger.info(
            "[AUDIT] Job %s OvrC done count=%s elapsed=%.1fs",
            job_id,
            len(results),
            time.perf_counter() - t0,
        )
        if times and not errors:
            from audits.models import AuditJob

            # One OvrC timestamp line on the job (last FX if several).
            AuditJob.objects.filter(pk=job_id).update(progress_message=times[-1][:255])
    except CaptureCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[AUDIT] Job %s OvrC failed", job_id)
        errors.append(f"OvrC: {exc}")
        _set_progress(job_id, f"OvrC failed: {type(exc).__name__}")
    return errors


def _dst_fleet_ovrc_times(
    parent_job_id: int,
    assignments: list[tuple[int, str, list[str], Path]],
    fleet_dir: Path,
) -> list[str]:
    """One OvrC login for all DE FX; attach each shot to the owning child job.

    assignments: (child_pk, site_key, fx_list, child_dir)
    """
    from audits.models import AuditJob
    from services.ovrc_capture import capture_fx_times
    from services.teamviewer_capture import CaptureCancelled

    if not assignments:
        return []

    ovrc_user = (getattr(settings, "OVRC_USERNAME", "") or "").strip()
    ovrc_pass = (getattr(settings, "OVRC_PASSWORD", "") or "").strip()
    if not ovrc_user or not ovrc_pass:
        return ["OvrC fleet: OVRC_USERNAME / OVRC_PASSWORD not configured"]

    fx_owners: dict[str, list[tuple[int, str, Path]]] = {}
    all_fx: list[str] = []
    for child_pk, site_key, fx_list, child_dir in assignments:
        for raw in fx_list:
            fx = (raw or "").strip().upper()
            if not fx:
                continue
            all_fx.append(fx)
            fx_owners.setdefault(fx, []).append((child_pk, site_key, child_dir))

    unique_fx = list(dict.fromkeys(all_fx))
    if not unique_fx:
        return ["OvrC fleet: no FX serials"]

    fleet_dir.mkdir(parents=True, exist_ok=True)
    _dst_log(
        parent_job_id,
        "phase3 fleet OvrC begin fx=%s children=%s",
        unique_fx,
        len(assignments),
    )
    _set_progress(
        parent_job_id,
        f"Phase 4/4: OvrC local time for {len(unique_fx)} FX…",
    )
    AuditJob.objects.filter(pk=parent_job_id).update(
        target_host=f"ovrc 0/{len(unique_fx)}"
    )

    errors: list[str] = []
    t0 = time.perf_counter()
    try:
        results = capture_fx_times(
            unique_fx,
            username=ovrc_user,
            password=ovrc_pass,
            output_dir=fleet_dir,
            base_url=getattr(settings, "OVRC_BASE_URL", "") or "https://app.ovrc.com",
            headless=True,
            on_progress=lambda msg: _set_progress(parent_job_id, f"Phase 4/4: {msg}"),
        )
    except CaptureCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("[DST] Job %s fleet OvrC failed", parent_job_id)
        return [f"OvrC fleet: {exc}"]

    seen_fx = {(r.fx_number or "").strip().upper() for r in results}
    for fx in unique_fx:
        if fx not in seen_fx:
            for child_pk, site_key, _dir in fx_owners.get(fx, []):
                errors.append(f"{site_key}: OvrC missing result for {fx}")

    child_times: dict[int, list[str]] = {}
    child_errs: dict[int, list[str]] = {}
    for r in results:
        fx = (r.fx_number or "").strip().upper()
        label = f"ovrc_{fx.lower()}"
        line = _format_ovrc_timestamp_line(fx, r.local_time)
        owners = fx_owners.get(fx) or []
        for child_pk, site_key, _child_dir in owners:
            try:
                _save_shot(child_pk, Path(r.screenshot_path), label)
                child_times.setdefault(child_pk, []).append(line)
                logger.info(
                    "[DST] Job %s fleet OvrC ok child=%s site=%s fx=%s raw=%s est=%s",
                    parent_job_id,
                    child_pk,
                    site_key,
                    fx,
                    r.local_time,
                    line,
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"{site_key}: OvrC save {fx}: {exc}"
                errors.append(msg)
                child_errs.setdefault(child_pk, []).append(msg)

    # Store one OvrC timestamp on each child; keep list status separate from the clock.
    for child_pk, site_key, fx_list, _child_dir in assignments:
        close_old_connections()
        try:
            child = AuditJob.objects.get(pk=child_pk)
        except AuditJob.DoesNotExist:
            continue
        if child.status == AuditJob.Status.CANCELLED:
            continue
        extra = child_errs.get(child_pk) or []
        times = child_times.get(child_pk) or []
        prev = (child.error_message or "").strip()
        if extra:
            merged = "; ".join([p for p in [prev, *extra] if p])[:2000]
            child.error_message = merged
        warns = 0
        if child.error_message:
            warns = len([x for x in child.error_message.split(";") if x.strip()])
        if child.screenshots.exists():
            child.status = AuditJob.Status.SUCCEEDED
            # OvrC timestamp is the progress line; warnings stay on error_message.
            if times:
                child.progress_message = times[-1][:255]
            elif warns:
                child.progress_message = f"Done with warnings ({warns})"
            else:
                child.progress_message = "Done"
            # Fleet OvrC runs after the per-site TV pass — bump finished_at so
            # audit Finished is not earlier than the OvrC capture wall time.
            child.finished_at = timezone.now()
        child.save(
            update_fields=["error_message", "status", "progress_message", "finished_at"]
        )

    logger.info(
        "[DST] Job %s fleet OvrC done fx=%s elapsed=%.1fs errors=%s",
        parent_job_id,
        len(results),
        time.perf_counter() - t0,
        len(errors),
    )
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
            # Inline check — avoid nested lock if Lock is used; safe with RLock too.
            pev = _cancel_flags.get(job_id)
            if pev is not None and pev.is_set():
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


def _dst_site_kind(inst) -> str | None:
    """Return 'lti' or 'de' when the installation is eligible for DST audit."""
    if inst.is_dragoneye:
        if not (inst.fx_numbers or []):
            return None
        if inst.is_vbe:
            from services.vbe_daily_checks import is_canonical_vbe_identifier

            if not is_canonical_vbe_identifier(inst.identifier):
                return None
        return "de"
    pole = (inst.pole_number or "").strip()
    if not pole:
        return None
    try:
        from services.ip_map import DeviceType, pole_to_ip_address

        pole_to_ip_address(pole, DeviceType.CBW)
    except (ValueError, TypeError):
        return None
    return "lti"


def _active_dst_installations(
    *,
    scope: str = "all",
    state: str = "",
    agency: str = "",
    installation_ids: list[int] | None = None,
) -> list:
    """Active installations eligible for a DST timezone audit run.

    When installation_ids is provided, only those PKs are considered and
    browse filters (scope/state/agency) are ignored — explicit selection wins.
    Ineligible platforms are still dropped. Selection order is kept.
    """
    from cameras.models import Installation

    close_old_connections()
    qs = Installation.objects.filter(is_active=True)
    ids: list[int] | None = None
    explicit = installation_ids is not None
    if explicit:
        ids = []
        for x in installation_ids:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                continue
        if not ids:
            return []
        qs = qs.filter(pk__in=ids)

    if not explicit:
        state = (state or "").strip()
        agency = (agency or "").strip()
        if state:
            qs = qs.filter(state__iexact=state)
        if agency:
            qs = qs.filter(agency__iexact=agency)

    qs = qs.order_by("primary_platform", "pole_number", "identifier")
    by_id = {inst.pk: inst for inst in qs}
    candidates = (
        [by_id[i] for i in ids if i in by_id]
        if ids is not None
        else list(by_id.values())
    )

    scope = (scope or "all").strip().lower()
    rows: list = []
    for inst in candidates:
        kind = _dst_site_kind(inst)
        if kind is None:
            continue
        if not explicit:
            if scope == "lti" and kind != "lti":
                continue
            if scope in {"de", "fx", "dragoneye"} and kind != "de":
                continue
        rows.append(inst)
    return rows


def _dst_selection_path(job_id: int) -> Path:
    return Path(settings.MEDIA_ROOT) / "audits" / f"dst_selection_{job_id}.json"


def write_dst_selection(job_id: int, installation_ids: list[int]) -> Path:
    """Persist selected installation PKs for a DST audit parent job."""
    path = _dst_selection_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    path.write_text(
        json.dumps({"installation_ids": [int(x) for x in installation_ids]}),
        encoding="utf-8",
    )
    logger.info(
        "[DST] Job %s selection written count=%s path=%s",
        job_id,
        len(installation_ids),
        path,
    )
    return path


def read_dst_selection(job_id: int) -> list[int] | None:
    """Return selected installation PKs, or None when no selection file exists."""
    import json

    path = _dst_selection_path(job_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("[DST] Job %s failed to read selection file", job_id)
        return None
    raw = data.get("installation_ids") or []
    out: list[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _dst_site_key(inst) -> str:
    return (inst.pole_number or inst.identifier or f"ims-{inst.ims_id}").strip()


def _power_on_lti_site(inst) -> tuple[str, str | None, bool]:
    """Turn CBW relays on. Returns (summary, error_or_none, did_turn_on)."""
    from services.cbw_relays import turn_all_relays_on

    pole = (inst.pole_number or "").strip()
    try:
        result = turn_all_relays_on(
            pole,
            settings.CBW_USERNAME,
            settings.CBW_PASSWORDS,
        )
        changed = result.action == "turned_on"
        return f"{pole}: relays {result.action}", None, changed
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[DST] power-on LTI failed pole=%s err=%s",
            pole,
            type(exc).__name__,
        )
        return pole, f"{pole}: {exc}", False


def _power_on_de_site(inst) -> tuple[str, str | None, bool]:
    """Turn OvrC WattBox DCAM System ON. Returns (summary, error_or_none, did_turn_on)."""
    from services.ovrc_capture import ensure_dcam_on_for_fxes

    key = _dst_site_key(inst)
    fx_list = list(inst.fx_numbers or [])
    ovrc_user = (getattr(settings, "OVRC_USERNAME", "") or "").strip()
    ovrc_pass = (getattr(settings, "OVRC_PASSWORD", "") or "").strip()
    if not ovrc_user or not ovrc_pass:
        return key, f"{key}: OVRC_USERNAME / OVRC_PASSWORD not configured", False
    try:
        results = ensure_dcam_on_for_fxes(
            fx_list,
            username=ovrc_user,
            password=ovrc_pass,
            base_url=getattr(settings, "OVRC_BASE_URL", "") or "https://app.ovrc.com",
            headless=True,
        )
        bits = [f"{r.fx_number}:{r.action}" for r in results]
        changed = any((r.action or "") == "turned_on" for r in results)
        return f"{key}: DCAM {', '.join(bits)}", None, changed
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[DST] power-on DE failed site=%s fx=%s err=%s",
            key,
            ",".join(fx_list),
            type(exc).__name__,
        )
        return key, f"{key}: {exc}", False


def _power_on_all_de_sites(
    job_id: int,
    de_sites: list,
) -> tuple[int, bool, list[str]]:
    """Phase 1 DE: one OvrC login, DCAM ON for every selected FX.

    Returns (power_ok_count, any_turned_on, errors).
    """
    from services.ovrc_capture import ensure_dcam_on_for_fxes

    if not de_sites:
        return 0, False, []

    ovrc_user = (getattr(settings, "OVRC_USERNAME", "") or "").strip()
    ovrc_pass = (getattr(settings, "OVRC_PASSWORD", "") or "").strip()
    if not ovrc_user or not ovrc_pass:
        errs = [
            f"power-on {_dst_site_key(inst)}: OVRC_USERNAME / OVRC_PASSWORD not configured"
            for inst in de_sites
        ]
        return 0, False, errs

    # Preserve site → FX so we can attribute results / failures.
    site_fx: list[tuple[object, str, list[str]]] = []
    all_fx: list[str] = []
    for inst in de_sites:
        key = _dst_site_key(inst)
        fx_list = [
            (fx or "").strip().upper()
            for fx in (inst.fx_numbers or [])
            if (fx or "").strip()
        ]
        # Dedupe within site, keep order.
        fx_list = list(dict.fromkeys(fx_list))
        site_fx.append((inst, key, fx_list))
        all_fx.extend(fx_list)

    unique_fx = list(dict.fromkeys(all_fx))
    _dst_log(
        job_id,
        "phase1 DE batch OvrC DCAM sites=%s fx=%s",
        len(de_sites),
        unique_fx,
    )
    _set_progress(
        job_id,
        f"Phase 1/4: DE power-on {len(unique_fx)} FX across {len(de_sites)} site(s)…",
    )

    if not unique_fx:
        errs = [f"power-on {key}: no FX serials" for _inst, key, fx in site_fx]
        return 0, False, errs

    try:
        results = ensure_dcam_on_for_fxes(
            unique_fx,
            username=ovrc_user,
            password=ovrc_pass,
            base_url=getattr(settings, "OVRC_BASE_URL", "") or "https://app.ovrc.com",
            headless=True,
            on_progress=lambda msg: _set_progress(job_id, f"Phase 1/4: {msg}"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[DST] Job %s phase1 DE batch failed: %s",
            job_id,
            type(exc).__name__,
        )
        return (
            0,
            False,
            [f"power-on DE fleet: {exc}"],
        )

    by_fx = {(r.fx_number or "").strip().upper(): r for r in results}
    power_ok = 0
    any_turned_on = False
    errors: list[str] = []
    for _inst, key, fx_list in site_fx:
        if not fx_list:
            errors.append(f"power-on {key}: no FX serials")
            continue
        missing = [fx for fx in fx_list if fx not in by_fx]
        if missing:
            errors.append(f"power-on {key}: no DCAM result for {', '.join(missing)}")
            continue
        bits = [f"{fx}:{by_fx[fx].action}" for fx in fx_list]
        changed = any((by_fx[fx].action or "") == "turned_on" for fx in fx_list)
        power_ok += 1
        if changed:
            any_turned_on = True
        logger.info(
            "[DST] Job %s power-on ok %s: DCAM %s",
            job_id,
            key,
            ", ".join(bits),
        )
    return power_ok, any_turned_on, errors


def _run_dst_site_capture(
    job_id: int,
    inst,
    output_dir: Path,
    *,
    kind: str | None = None,
    include_ovrc: bool = True,
) -> list[str]:
    """Capture one site for DST audit: LTI=CBW+VNC, DE=TV(+OvrC). Logs wall times.

    When include_ovrc is False, DE sites skip OvrC time scrapes (fleet pass does them).
    """
    # Long settle / OvrC power-on can leave a stale SQLite connection on this thread.
    _dst_log(job_id, "site capture enter inst_pk=%s pole=%s", getattr(inst, "pk", None), getattr(inst, "pole_number", ""))
    t0 = time.perf_counter()
    close_old_connections()
    _dst_log(job_id, "site capture DB connections refreshed (%.2fs)", time.perf_counter() - t0)

    # Re-load a fresh Installation row — the fleet list object can be stale after OvrC.
    from cameras.models import Installation

    inst_pk = getattr(inst, "pk", None)
    if inst_pk:
        t_reload = time.perf_counter()
        fresh = Installation.objects.filter(pk=inst_pk, is_active=True).first()
        _dst_log(
            job_id,
            "site capture reload pk=%s found=%s (%.2fs)",
            inst_pk,
            bool(fresh),
            time.perf_counter() - t_reload,
        )
        if fresh is not None:
            inst = fresh

    kind = (kind or _dst_site_kind(inst) or "").strip().lower() or None
    _dst_log(job_id, "site capture kind resolved=%s", kind)
    key = _dst_site_key(inst)
    fx_list: list[str] = []
    if kind == "de":
        t_fx = time.perf_counter()
        fx_list = list(inst.fx_numbers or [])
        _dst_log(
            job_id,
            "site capture fx_list=%s (%.2fs)",
            fx_list,
            time.perf_counter() - t_fx,
        )
    wall_start = timezone.localtime(timezone.now())
    _dst_log(
        job_id,
        "site capture start key=%s kind=%s fx=%s wall=%s",
        key,
        kind,
        ",".join(fx_list) or "-",
        wall_start.strftime("%Y-%m-%d %H:%M:%S %Z"),
    )
    _set_progress(
        job_id,
        f"Capturing {key} ({kind}) @ {wall_start.strftime('%H:%M:%S')}…",
    )

    errors: list[str] = []
    if kind == "lti":
        _dst_log(job_id, "site capture calling LTI pole bundle")
        errors.extend(_run_pole_bundle_parallel(job_id, key, output_dir))
        _dst_log(job_id, "site capture LTI bundle returned errors=%s", len(errors))
    elif kind == "de":
        _dst_log(job_id, "site capture calling DE TeamViewer bundle")
        errors.extend(_run_de_bundle(job_id, key, output_dir, daily_export=False))
        _dst_log(job_id, "site capture DE bundle returned errors=%s", len(errors))
        if include_ovrc:
            _raise_if_cancelled(job_id)
            _dst_log(job_id, "site capture calling OvrC times fx=%s", fx_list)
            errors.extend(_capture_ovrc_times(job_id, fx_list, output_dir))
            _dst_log(job_id, "site capture OvrC returned errors=%s", len(errors))
        else:
            _dst_log(job_id, "site capture skipping OvrC (fleet pass later)")
    else:
        _dst_log(job_id, "site capture abort: kind not eligible (%s)", kind)
        return [f"{key}: not eligible for DST audit"]

    wall_end = timezone.localtime(timezone.now())
    from audits.models import AuditScreenshot

    close_old_connections()
    shots = list(
        AuditScreenshot.objects.filter(job_id=job_id).order_by("created_at")
    )
    stamp_lines: list[str] = []
    for shot in shots:
        local = timezone.localtime(shot.created_at)
        line = f"{shot.label}: {local.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        stamp_lines.append(line)
        _dst_log(job_id, "timestamp %s", line)

    stamp_path = output_dir / "capture_timestamps.txt"
    stamp_path.write_text(
        "\n".join(
            [
                f"site={key}",
                f"kind={kind}",
                f"started={wall_start.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                f"finished={wall_end.strftime('%Y-%m-%d %H:%M:%S %Z')}",
                "",
                *stamp_lines,
                "",
                "Compare device clocks in screenshots to these wall times.",
                "LTI: CBW time ≈ VNC time ≈ screenshot wall time.",
                "DE/FX: TeamViewer time ≈ OvrC time ≈ screenshot wall time.",
            ]
        ),
        encoding="utf-8",
    )
    _dst_log(
        job_id,
        "site capture done key=%s shots=%s errors=%s wall_end=%s",
        key,
        len(shots),
        len(errors),
        wall_end.strftime("%Y-%m-%d %H:%M:%S %Z"),
    )
    if stamp_lines:
        _set_progress(job_id, f"{key}: " + "; ".join(stamp_lines)[:240])
    return errors


def _run_dst_audit(
    job_id: int,
    output_dir: Path,
    *,
    scope: str = "all",
    state: str = "",
    agency: str = "",
) -> tuple[list[str], int]:
    """Fleet DST audit: power on all sites first, settle, then capture.

    Phase 1 — LTI relays (parallel) + one OvrC login for all DE DCAM outlets
    Phase 2 — shared boot settle (skipped if everything was already on)
    Phase 3 — capture each site (TV/CBW/VNC), then one fleet OvrC time pass

    Creates a child dst_site job per installation. Returns (errors, sites_ok).
    """
    from audits.models import AuditJob
    from services.teamviewer_capture import CaptureCancelled

    close_old_connections()
    _raise_if_cancelled(job_id)
    bulk = AuditJob.objects.get(pk=job_id)

    # Filters / selection stored on parent at create time.
    meta = _parse_dst_audit_meta(bulk.target_host)
    scope = scope or meta.get("scope") or "all"
    state = state or meta.get("state") or ""
    agency = agency or meta.get("agency") or ""
    selected_ids = read_dst_selection(job_id)

    sites = _active_dst_installations(
        scope=scope,
        state=state,
        agency=agency,
        installation_ids=selected_ids,
    )
    if not sites:
        return ["No eligible installations for DST audit"], 0

    total = len(sites)
    lti_sites = [s for s in sites if _dst_site_kind(s) == "lti"]
    de_sites = [s for s in sites if _dst_site_kind(s) == "de"]
    logger.info(
        "[DST] Job %s starting fleet audit total=%s lti=%s de=%s scope=%s state=%s agency=%s selected=%s",
        job_id,
        total,
        len(lti_sites),
        len(de_sites),
        scope,
        state or "*",
        agency or "*",
        len(selected_ids) if selected_ids is not None else "all",
    )

    all_errors: list[str] = []
    power_ok = 0
    any_turned_on = False

    # ── Phase 1a: CBW relays (parallel) ──────────────────────────────────
    if lti_sites:
        _set_progress(job_id, f"Phase 1/4: powering on {len(lti_sites)} LTI site(s)…")
        AuditJob.objects.filter(pk=job_id).update(
            target_host=f"power_on 0/{total}"
        )
        max_workers = min(
            len(lti_sites),
            max(1, int(getattr(settings, "AUDIT_STEP_CONCURRENT", 3))),
        )
        logger.info(
            "[DST] Job %s phase1 LTI relays workers=%s",
            job_id,
            max_workers,
        )
        done = 0
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"dst-pwr-{job_id}",
        ) as pool:
            futures = {pool.submit(_power_on_lti_site, inst): inst for inst in lti_sites}
            for fut in as_completed(futures):
                _raise_if_cancelled(job_id)
                inst = futures[fut]
                done += 1
                changed = False
                try:
                    summary, err, changed = fut.result()
                except Exception as exc:  # noqa: BLE001
                    err = f"{_dst_site_key(inst)}: {exc}"
                    summary = _dst_site_key(inst)
                if err:
                    all_errors.append(f"power-on {err}")
                    logger.warning("[DST] Job %s power-on fail %s", job_id, err)
                else:
                    power_ok += 1
                    if changed:
                        any_turned_on = True
                    logger.info("[DST] Job %s power-on ok %s", job_id, summary)
                _set_progress(
                    job_id,
                    f"Phase 1/4: LTI power-on {done}/{len(lti_sites)} "
                    f"(fleet {power_ok}/{total})…",
                )
                AuditJob.objects.filter(pk=job_id).update(
                    target_host=f"power_on {power_ok}/{total}"
                )

    # ── Phase 1b: OvrC DCAM for all DE sites (one login) ─────────────────
    if de_sites:
        logger.info("[DST] Job %s phase1 DE OvrC DCAM count=%s", job_id, len(de_sites))
        de_ok, de_changed, de_errs = _power_on_all_de_sites(job_id, de_sites)
        power_ok += de_ok
        if de_changed:
            any_turned_on = True
        for err in de_errs:
            all_errors.append(err)
            logger.warning("[DST] Job %s power-on fail %s", job_id, err)
        AuditJob.objects.filter(pk=job_id).update(
            target_host=f"power_on {power_ok}/{total}"
        )

    logger.info(
        "[DST] Job %s phase1 complete power_ok=%s/%s turned_on=%s errors=%s",
        job_id,
        power_ok,
        total,
        any_turned_on,
        len(all_errors),
    )

    # ── Phase 2: settle / boot wait (only if something was powered on) ───
    settle = max(0, int(getattr(settings, "DST_POWER_SETTLE_SECONDS", 90)))
    if settle > 0 and any_turned_on:
        logger.info(
            "[DST] Job %s phase2 settle wait=%ss (cameras were turned on)",
            job_id,
            settle,
        )
        deadline = time.monotonic() + settle
        while True:
            _raise_if_cancelled(job_id)
            remaining = int(max(0, deadline - time.monotonic()))
            if remaining <= 0:
                break
            _set_progress(
                job_id,
                f"Phase 2/4: waiting {remaining}s for cameras to boot…",
            )
            AuditJob.objects.filter(pk=job_id).update(
                target_host=f"settle {remaining}s"
            )
            time.sleep(min(5, remaining))
    elif settle > 0:
        logger.info(
            "[DST] Job %s phase2 settle skipped — all sites already on",
            job_id,
        )
        _set_progress(job_id, "Phase 2/4: settle skipped (already on)")
        AuditJob.objects.filter(pk=job_id).update(target_host="settle skipped")

    # ── Phase 3: capture each site (TV/CBW/VNC), then one fleet OvrC pass ─
    sites_ok = 0
    de_ovrc_assignments: list[tuple[int, str, list[str], Path]] = []
    _dst_log(job_id, "phase3 begin total=%s", total)
    _set_progress(job_id, f"Phase 3/4: capturing 0/{total} sites…")
    AuditJob.objects.filter(pk=job_id).update(target_host=f"capture 0/{total}")

    for index, inst in enumerate(sites, start=1):
        _raise_if_cancelled(job_id)
        close_old_connections()
        _dst_log(
            job_id,
            "phase3 loop index=%s/%s inst_pk=%s",
            index,
            total,
            getattr(inst, "pk", None),
        )
        t_key = time.perf_counter()
        key = _dst_site_key(inst)
        kind = _dst_site_kind(inst) or "?"
        _dst_log(
            job_id,
            "phase3 site key=%s kind=%s (resolve %.2fs)",
            key,
            kind,
            time.perf_counter() - t_key,
        )
        _set_progress(job_id, f"Phase 3/4: {index}/{total} capturing {key} ({kind})…")
        _dst_log(job_id, "phase3 creating child AuditJob…")
        t_child = time.perf_counter()
        child = AuditJob.objects.create(
            pole_number=key,
            target_host=kind,
            device_type=AuditJob.DeviceType.DST_SITE,
            status=AuditJob.Status.RUNNING,
            created_by_id=getattr(bulk, "created_by_id", None),
            parent_job_id=job_id,
            started_at=timezone.now(),
            progress_message=f"DST audit {job_id} ({index}/{total})",
        )
        _dst_log(
            job_id,
            "phase3 child created pk=%s (%.2fs)",
            child.pk,
            time.perf_counter() - t_child,
        )
        with _cancel_lock:
            child_ev = threading.Event()
            _cancel_flags[child.pk] = child_ev
            _job_children.setdefault(job_id, set()).add(child.pk)
            pev = _cancel_flags.get(job_id)
            if pev is not None and pev.is_set():
                child_ev.set()
        _dst_log(job_id, "phase3 cancel flag registered for child=%s", child.pk)
        child_dir = output_dir / key.replace("/", "_").replace("\\", "_")
        _dst_log(job_id, "phase3 preparing child dir %s", child_dir)
        child_dir.mkdir(parents=True, exist_ok=True)
        _dst_log(job_id, "phase3 child dir ready")
        try:
            close_old_connections()
            _dst_log(
                job_id,
                "phase3 invoking site capture child=%s kind=%s",
                child.pk,
                kind,
            )
            # DE OvrC times run once after all TeamViewer lanes (fleet pass).
            errors = _run_dst_site_capture(
                child.pk,
                inst,
                child_dir,
                kind=kind,
                include_ovrc=False,
            )
            _dst_log(
                job_id,
                "phase3 site capture returned child=%s errors=%s",
                child.pk,
                len(errors),
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
            child.target_host = kind
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
            if kind == "de" and child.status != AuditJob.Status.CANCELLED:
                fx_list = [
                    (fx or "").strip().upper()
                    for fx in (inst.fx_numbers or [])
                    if (fx or "").strip()
                ]
                fx_list = list(dict.fromkeys(fx_list))
                if fx_list:
                    de_ovrc_assignments.append((child.pk, key, fx_list, child_dir))
            logger.info(
                "[DST] Job %s child %s site=%s status=%s shots=%s",
                job_id,
                child.pk,
                key,
                child.status,
                child.screenshots.count(),
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
            logger.exception("[DST] Job %s child failed site=%s", job_id, key)
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

        AuditJob.objects.filter(pk=job_id).update(
            target_host=f"capture {sites_ok}/{total}"
        )
        _set_progress(
            job_id,
            f"Phase 3/4: {index}/{total} done — {sites_ok} ok, "
            f"{len(all_errors)} warning(s)…",
        )

    # Fleet OvrC pass: one login for every DE FX after all TV captures.
    if de_ovrc_assignments:
        _raise_if_cancelled(job_id)
        ovrc_errs = _dst_fleet_ovrc_times(
            job_id,
            de_ovrc_assignments,
            output_dir / "_ovrc_fleet",
        )
        for err in ovrc_errs:
            all_errors.append(err)

    _set_progress(job_id, f"DST Audit finished {sites_ok}/{total} site(s)")
    AuditJob.objects.filter(pk=job_id).update(
        target_host=f"done {sites_ok}/{total}"
    )
    logger.info(
        "[DST] Job %s finished sites_ok=%s/%s errors=%s",
        job_id,
        sites_ok,
        total,
        len(all_errors),
    )
    if sites_ok == 0 and not all_errors:
        all_errors.append("No sites captured")
    return all_errors, sites_ok


def _parse_dst_audit_meta(raw: str) -> dict[str, str]:
    """Parse scope/state/agency from parent target_host metadata."""
    out: dict[str, str] = {}
    text = (raw or "").strip()
    if not text or "=" not in text:
        return out
    for part in text.split(";"):
        if "=" not in part:
            continue
        key, _, val = part.partition("=")
        key = key.strip().lower()
        if key in {"scope", "state", "agency"}:
            out[key] = val.strip()
    return out


def _encode_dst_audit_meta(scope: str, state: str, agency: str) -> str:
    parts = [f"scope={(scope or 'all').strip().lower() or 'all'}"]
    if (state or "").strip():
        parts.append(f"state={state.strip()}")
    if (agency or "").strip():
        parts.append(f"agency={agency.strip()}")
    return ";".join(parts)


def _run_job(job_id: int) -> None:
    from audits.models import AuditJob
    from services.cbw_capture import capture_cbw_with_passwords
    from services.ip_map import DeviceType, pole_to_ip_address
    from services.teamviewer_capture import CaptureCancelled
    from services.vnc_capture import capture_vnc

    # Wait for a worker slot without blocking forever. A hung prior job (e.g. TeamViewer)
    # can hold the semaphore; surface that in progress and allow cancel/timeout.
    slot_timeout = max(30, int(getattr(settings, "AUDIT_SLOT_WAIT_SECONDS", 300)))
    close_old_connections()
    try:
        job = AuditJob.objects.get(pk=job_id)
    except AuditJob.DoesNotExist:
        logger.error("[AUDIT] Job %s not found before slot wait", job_id)
        return

    if job.status == AuditJob.Status.CANCELLED or is_job_cancelled(job_id):
        AuditJob.objects.filter(pk=job_id).update(
            status=AuditJob.Status.CANCELLED,
            error_message="Cancelled by user",
            progress_message="Cancelled",
            finished_at=timezone.now(),
        )
        return

    _set_progress(job_id, "Waiting for audit worker slot…")
    logger.info("[AUDIT] Job %s waiting for worker slot (timeout=%ss)", job_id, slot_timeout)
    deadline = time.monotonic() + slot_timeout
    acquired = False
    while True:
        if is_job_cancelled(job_id):
            AuditJob.objects.filter(pk=job_id).update(
                status=AuditJob.Status.CANCELLED,
                error_message="Cancelled by user",
                progress_message="Cancelled",
                finished_at=timezone.now(),
            )
            logger.info("[AUDIT] Job %s cancelled while waiting for slot", job_id)
            return
        if _semaphore.acquire(blocking=True, timeout=2.0):
            acquired = True
            break
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            AuditJob.objects.filter(pk=job_id).update(
                status=AuditJob.Status.FAILED,
                error_message=(
                    "Timed out waiting for an audit worker slot. Another capture is "
                    "likely stuck (often TeamViewer). Restart the Django server and retry."
                ),
                progress_message="Failed: worker slot timeout",
                finished_at=timezone.now(),
            )
            logger.error(
                "[AUDIT] Job %s timed out waiting for worker slot after %ss",
                job_id,
                slot_timeout,
            )
            return
        _set_progress(job_id, f"Waiting for audit worker slot ({remaining}s)…")

    if not acquired:
        return

    try:
        close_old_connections()
        try:
            job = AuditJob.objects.get(pk=job_id)
        except AuditJob.DoesNotExist:
            logger.error("[AUDIT] Job %s not found", job_id)
            return

        if job.status in (
            AuditJob.Status.CANCELLED,
            AuditJob.Status.FAILED,
        ) or is_job_cancelled(job_id):
            # Reaped/cancelled while we waited for the slot.
            logger.info(
                "[AUDIT] Job %s status=%s after slot acquire — exiting",
                job_id,
                job.status,
            )
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

            if job.device_type == AuditJob.DeviceType.DST_AUDIT:
                errors, sites_ok = _run_dst_audit(job_id, output_dir)
                job.refresh_from_db()
                if job.status == AuditJob.Status.CANCELLED or is_job_cancelled(job_id):
                    _finalize_cancelled()
                    return
                if sites_ok == 0:
                    raise RuntimeError("; ".join(errors) or "DST Audit failed")
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
                    "[DST] Job %s fleet audit finished sites_ok=%s errors=%s elapsed=%.1fs",
                    job_id,
                    sites_ok,
                    len(errors),
                    time.perf_counter() - t0,
                )
                return

            if job.device_type == AuditJob.DeviceType.DST_SITE:
                # Standalone dst_site (normally spawned inline by dst_audit).
                from cameras.models import Installation

                key = job.pole_number
                inst = (
                    Installation.objects.filter(pole_number=key, is_active=True)
                    .order_by("-last_synced_at")
                    .first()
                )
                if inst is None:
                    inst = (
                        Installation.objects.filter(identifier=key, is_active=True)
                        .order_by("-last_synced_at")
                        .first()
                    )
                if inst is None:
                    raise RuntimeError(f"No installation for {key}")
                errors = _run_dst_site_capture(job_id, inst, output_dir)
                job.refresh_from_db()
                if job.status == AuditJob.Status.CANCELLED or is_job_cancelled(job_id):
                    _finalize_cancelled()
                    return
                if not job.screenshots.exists():
                    raise RuntimeError("; ".join(errors) or "No DST screenshots")
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
                    "[DST] Job %s site finished shots=%s errors=%s elapsed=%.1fs",
                    job_id,
                    job.screenshots.count(),
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
