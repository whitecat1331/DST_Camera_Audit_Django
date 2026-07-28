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


def enqueue_audit_job(job_id: int) -> None:
    logger.info("[AUDIT] enqueue job=%s", job_id)
    thread = threading.Thread(
        target=_run_job,
        args=(job_id,),
        name=f"audit-job-{job_id}",
        daemon=True,
    )
    thread.start()


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


def _run_de_bundle(job_id: int, pole_number: str, output_dir: Path) -> list[str]:
    """Capture each DragonEye TeamViewer lane sequentially (TV is exclusive)."""
    from cameras.models import Installation
    from services.dragoneye_ids import mappings_for_fx
    from services.teamviewer_capture import capture_dragoneye_via_teamviewer

    close_old_connections()
    inst = (
        Installation.objects.filter(pole_number=pole_number, is_active=True)
        .order_by("-last_synced_at")
        .first()
    )
    if inst is None:
        return [f"No installation for pole {pole_number}"]
    fx = inst.fx_number
    if not fx:
        return [f"No FX serial on installation pole={pole_number}"]

    mappings = mappings_for_fx(fx)
    if not mappings:
        return [
            f"No TeamViewer ID mapped for {fx} — upload DragonEye Teamviewer IDs.csv"
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

    hosts = [m.teamviewer_id for m in mappings]
    AuditJob.objects.filter(pk=job_id).update(target_host=";".join(hosts))
    _set_progress(job_id, f"DragonEye {fx}: {len(mappings)} TeamViewer lane(s)…")

    errors: list[str] = []
    for m in mappings:
        label = m.thumb_key
        lane_bit = m.lane or "TV"
        filename = f"{label}.png"
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
            )
            _save_shot(job_id, path, label)
            _set_progress(job_id, f"Saved {fx} {lane_bit}")
            logger.info(
                "[AUDIT] Job %s DE ok fx=%s lane=%s tv=%s elapsed=%.1fs",
                job_id,
                fx,
                lane_bit,
                m.teamviewer_id,
                time.perf_counter() - t0,
            )
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


def _run_job(job_id: int) -> None:
    from audits.models import AuditJob
    from services.cbw_capture import capture_cbw_with_passwords
    from services.ip_map import DeviceType, pole_to_ip_address
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

        t0 = time.perf_counter()
        logger.info(
            "[AUDIT] Job %s running type=%s pole=%s host=%s",
            job_id,
            job.device_type,
            job.pole_number,
            job.target_host,
        )
        job.status = AuditJob.Status.RUNNING
        job.started_at = timezone.now()
        job.error_message = ""
        job.progress_message = "Starting…"
        job.save(update_fields=["status", "started_at", "error_message", "progress_message"])

        media_root = Path(settings.MEDIA_ROOT)
        output_dir = media_root / "audits" / str(job.pk)
        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            if job.device_type == AuditJob.DeviceType.POLE_BUNDLE:
                errors = _run_pole_bundle_parallel(job_id, job.pole_number, output_dir)
                job.refresh_from_db()
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

            if job.device_type in (
                AuditJob.DeviceType.DE_BUNDLE,
                AuditJob.DeviceType.DE_TV,
            ):
                errors = _run_de_bundle(job_id, job.pole_number, output_dir)
                job.refresh_from_db()
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
                    "[AUDIT] Job %s DE bundle finished shots=%s errors=%s elapsed=%.1fs",
                    job_id,
                    job.screenshots.count(),
                    len(errors),
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
        except Exception as exc:  # noqa: BLE001 — persist failure on job
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
        close_old_connections()
        _semaphore.release()
