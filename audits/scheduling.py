"""One-time scheduled DST audits (plan: UNATTENDED_NIGHTLY_DST_AUDIT, Phase 3).

Saving a schedule registers a Windows Task Scheduler task; at the set local
time the task runs `manage.py run_scheduled_audit <id>`, which runs the audit
in that process (the DST web server doesn't need to be up) and publishes when
the schedule names a round.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta

from django.db import close_old_connections
from django.utils import timezone

from audits.models import AuditJob, ScheduledAudit
from services import task_scheduler

logger = logging.getLogger(__name__)

MIN_LEAD = timedelta(minutes=1)


class ScheduleError(Exception):
    pass


def device_now() -> datetime:
    """The laptop's local clock, with its zone (what Task Scheduler fires on)."""
    return datetime.now().astimezone()


def parse_local(value: str) -> datetime:
    """A datetime-local input value ("2026-10-28T23:30") on the device clock → aware."""
    try:
        naive = datetime.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ScheduleError("Pick a date and time.") from exc
    if naive.tzinfo is not None:
        return naive
    return naive.astimezone()  # the device's own zone, DST rules included


def create_schedule(user, fire_at: datetime, opts: dict) -> ScheduledAudit:
    from audits.runner import _active_dst_installations

    if fire_at < timezone.now() + MIN_LEAD:
        raise ScheduleError("Pick a time at least a minute from now.")
    sites = _active_dst_installations(
        scope=opts["scope"],
        state=opts["state"],
        agency=opts["agency"],
        installation_ids=opts["installation_ids"],
    )
    if not sites:
        raise ScheduleError("None of the selected sites are eligible for DST audit")
    sched = ScheduledAudit.objects.create(
        fire_at=fire_at,
        scope=opts["scope"],
        state=opts["state"],
        agency=opts["agency"],
        installation_ids=[s.pk for s in sites],
        publish_round=opts["publish_round"],
        created_by=user,
    )
    try:
        task_scheduler.register(sched.task_name, sched.pk, fire_at)
    except task_scheduler.TaskSchedulerError as exc:
        sched.delete()
        raise ScheduleError(f"Windows Task Scheduler refused the task: {exc}") from exc
    logger.info(
        "[SCHED] #%s at %s sites=%s round=%r by=%s",
        sched.pk,
        fire_at.isoformat(),
        len(sites),
        sched.publish_round,
        getattr(user, "username", None),
    )
    return sched


def cancel_schedule(sched: ScheduledAudit) -> None:
    if sched.status != ScheduledAudit.Status.SCHEDULED:
        raise ScheduleError(f"Schedule #{sched.pk} is {sched.get_status_display().lower()}.")
    task_scheduler.delete(sched.task_name)
    sched.status = ScheduledAudit.Status.CANCELLED
    sched.message = "Cancelled"
    sched.save(update_fields=["status", "message"])


def run_now(sched: ScheduledAudit) -> None:
    """Fire the schedule's task now: same process, user and desktop as at night."""
    if sched.status != ScheduledAudit.Status.SCHEDULED:
        raise ScheduleError(f"Schedule #{sched.pk} is {sched.get_status_display().lower()}.")
    try:
        task_scheduler.run_now(sched.task_name)
    except task_scheduler.TaskSchedulerError as exc:
        raise ScheduleError(str(exc)) from exc


def run_schedule(schedule_id: int) -> ScheduledAudit:
    """Run one schedule in this process; the task calls this. Never runs twice."""
    from audits import runner

    claimed = ScheduledAudit.objects.filter(
        pk=schedule_id, status=ScheduledAudit.Status.SCHEDULED
    ).update(status=ScheduledAudit.Status.RUNNING, pid=os.getpid(), message="Starting…")
    sched = ScheduledAudit.objects.get(pk=schedule_id)
    if not claimed:
        logger.warning("[SCHED] #%s not run: status=%s", schedule_id, sched.status)
        return sched

    def finish(status: str, message: str) -> ScheduledAudit:
        sched.status, sched.message = status, message[:500]
        sched.save(update_fields=["status", "message"])
        task_scheduler.delete(sched.task_name)
        return sched

    busy = AuditJob.objects.filter(
        device_type=AuditJob.DeviceType.DST_AUDIT,
        status__in=[AuditJob.Status.PENDING, AuditJob.Status.RUNNING],
    ).first()
    if busy:
        return finish(ScheduledAudit.Status.FAILED, f"Not started: DST audit #{busy.pk} was already running")
    try:
        job, sites = runner.create_dst_audit_job(
            sched.created_by,
            scope=sched.scope,
            state=sched.state,
            agency=sched.agency,
            installation_ids=list(sched.installation_ids),
            publish_round=sched.publish_round,
        )
    except ValueError as exc:
        return finish(ScheduledAudit.Status.FAILED, f"Not started: {exc}")
    sched.job = job
    sched.publish_round = runner.read_dst_publish_round(job.pk)  # the default when none was typed
    sched.message = f"Running DST audit #{job.pk} ({len(sites)} site(s))"
    sched.save(update_fields=["job", "publish_round", "message"])
    logger.info("[SCHED] #%s fired: job=%s sites=%s", sched.pk, job.pk, len(sites))

    with runner._cancel_lock:
        runner._cancel_flags[job.pk] = threading.Event()
    stop = threading.Event()
    watcher = threading.Thread(
        target=runner.mirror_db_cancel, args=(job.pk, stop), name=f"sched-cancel-{job.pk}", daemon=True
    )
    watcher.start()
    try:
        runner._run_job(job.pk)
    finally:
        stop.set()
    # A held publish waits for the VPN to be off; the schedule is done after it.
    for thread in threading.enumerate():
        if thread.name == f"dst-publish-{job.pk}":
            sched.message = f"DST audit #{job.pk} finished; publishing to {sched.publish_round!r}…"
            sched.save(update_fields=["message"])
            thread.join()
    close_old_connections()
    job.refresh_from_db()
    message = f"DST audit #{job.pk}: {job.get_status_display().lower()}"
    if job.progress_message:
        message += f" ({job.progress_message})"
    publish = (
        AuditJob.objects.filter(device_type=AuditJob.DeviceType.PUBLISH, created_at__gte=job.created_at)
        .order_by("-created_at")
        .first()
        if sched.publish_round
        else None
    )
    if publish:
        message += f"; publish #{publish.pk}: {publish.get_status_display().lower()}"
    ok = job.status == AuditJob.Status.SUCCEEDED and (
        publish is None or publish.status == AuditJob.Status.SUCCEEDED
    )
    return finish(ScheduledAudit.Status.DONE if ok else ScheduledAudit.Status.FAILED, message)
