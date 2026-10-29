"""Publish finished DST audit runs from local SQLite to the shared Supabase DB.

The laptop captures into SQLite (the camera VPN blocks Postgres egress); with
the VPN off, publish copies whole runs — parent + site jobs + screenshots +
image bytes — into the shared DB the review instance reads.

Order per run: images first (each its own small commit, skipped when the
sha256 already matches; several at once, the upload is bandwidth-bound), then
the run's rows in one transaction. Rows only appear once their images exist,
and an interrupted publish resumes by simply publishing again. Rows are
upserted by ``public_id``.

A run with a round also publishes live (LivePublisher): each site as it
finishes, whenever the shared DB answers. The final publish then re-sends
only what changed since (SentCache).
"""

from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.db import OperationalError, close_old_connections, connections, transaction
from django.db.models import Q
from django.utils import timezone

from django.conf import settings

from audits.assignment import assign_new_cameras
from audits.models import AuditJob, AuditRound, AuditScreenshot, StoredFile

logger = logging.getLogger(__name__)

SHARED = "shared"
THUMB_MAX_PX = 480
FINISHED = (
    AuditJob.Status.SUCCEEDED,
    AuditJob.Status.FAILED,
    AuditJob.Status.CANCELLED,
    AuditJob.Status.SKIPPED,
)
# Copied as-is; identity and foreign keys are remapped per database.
_REMAPPED = {"id", "parent_job", "created_by", "round"}
JOB_FIELDS = [f.attname for f in AuditJob._meta.concrete_fields if f.name not in _REMAPPED]
# Per-capture time fields (see AuditScreenshot), copied with each screenshot row.
SHOT_TIME_FIELDS = ("captured_at", "device_time_raw", "device_time", "device_time_source")

_publish_lock = threading.Lock()


class PublishError(Exception):
    pass


@dataclass
class PublishReport:
    round_name: str = ""
    runs: int = 0
    jobs: int = 0
    screenshots: int = 0
    images_uploaded: int = 0
    images_unchanged: int = 0
    assigned: int = 0
    sent_live: int = 0  # site jobs already published live and unchanged since
    missing_files: list[str] = field(default_factory=list)
    unmatched_poles: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"Published {self.runs} run(s) to '{self.round_name}': {self.jobs} jobs, "
            f"{self.screenshots} screenshots, {self.images_uploaded} images uploaded, "
            f"{self.images_unchanged} unchanged, {self.assigned} camera(s) newly assigned"
        ]
        if self.sent_live:
            parts.append(f"{self.sent_live} site(s) already published live")
        if self.missing_files:
            parts.append(f"{len(self.missing_files)} image file(s) missing locally")
        if self.unmatched_poles:
            parts.append(f"no local installation for: {', '.join(self.unmatched_poles)}")
        return "; ".join(parts)


def shared_configured() -> bool:
    return SHARED in connections.databases


def check_shared_reachable() -> None:
    if not shared_configured():
        raise PublishError("Shared DB is not configured (set DB_HOST in .env).")
    try:
        connections[SHARED].ensure_connection()
    except OperationalError as exc:
        host = connections.databases[SHARED].get("HOST", "")
        raise PublishError(
            f"Can't reach the shared DB at {host}. If the camera VPN is on, turn it "
            f"off and publish again. ({str(exc).splitlines()[0][:160]})"
        ) from exc


def clean_round_name(round_name: str) -> str:
    name = " ".join((round_name or "").split())
    if not name:
        raise PublishError("Round name is required.")
    if len(name) > 128:
        raise PublishError("Round name is too long (128 characters max).")
    return name


def publishable_runs(run_ids: list[int]) -> list[AuditJob]:
    """Local DST_AUDIT parents for run_ids; refuse unknown or unfinished runs."""
    runs = list(
        AuditJob.objects.filter(pk__in=run_ids, device_type=AuditJob.DeviceType.DST_AUDIT)
        .prefetch_related("child_jobs")
        .order_by("created_at")
    )
    found = {r.pk for r in runs}
    missing = [str(i) for i in run_ids if i not in found]
    if missing:
        raise PublishError(f"Not DST audit runs: #{', #'.join(missing)}")
    busy = [
        f"#{job.pk}"
        for run in runs
        for job in (run, *run.child_jobs.all())
        if job.status not in FINISHED
    ]
    if busy:
        raise PublishError(f"Runs still in progress, finish them first: {', '.join(busy)}")
    return runs


def make_thumbnail(data: bytes) -> bytes | None:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")
            img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=80, optimize=True)
            return out.getvalue()
    except Exception:  # noqa: BLE001 — a bad image still publishes, just without a thumb
        logger.warning("[PUBLISH] thumbnail failed", exc_info=True)
        return None


@dataclass
class SentCache:
    """What this process already published for one run.

    The final publish skips images whose digest it already sent (no query) and
    site jobs whose fingerprint hasn't changed since their live publish.
    """

    images: dict[str, str] = field(default_factory=dict)  # image name -> sha256
    jobs: dict[int, tuple] = field(default_factory=dict)  # local job pk -> fingerprint
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _upload_image(shot: AuditScreenshot, cache: SentCache | None = None) -> str:
    """Upload one image; returns "uploaded", "unchanged" or "missing"."""
    name = shot.image.name
    try:
        with shot.image.open("rb") as fh:
            data = fh.read()
    except (FileNotFoundError, OSError, ValueError):
        return "missing"
    digest = hashlib.sha256(data).hexdigest()
    if cache is not None:
        with cache.lock:
            if cache.images.get(name) == digest:
                return "unchanged"
    stored = (
        StoredFile.objects.using(SHARED).filter(name=name).values_list("sha256", flat=True).first()
    )
    if stored != digest:
        _store_image(name, data, digest)
    if cache is not None:
        with cache.lock:
            cache.images[name] = digest
    return "unchanged" if stored == digest else "uploaded"


def _store_image(name: str, data: bytes, digest: str) -> None:
    StoredFile.objects.using(SHARED).update_or_create(
        name=name,
        defaults={
            "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "size": len(data),
            "sha256": digest,
            "data": data,
            "thumb": make_thumbnail(data),
        },
    )


def _upload_images(shots, report: PublishReport, progress, label: str, cache: SentCache | None) -> None:
    """Upload shots' images a few at a time (measured: 4 → 2.5x, 8 no better).

    Each worker holds its own DB connections and closes them when it's done.
    The first worker error is raised once all workers have stopped.
    """
    if not shots:
        return
    work: queue.SimpleQueue = queue.SimpleQueue()
    for shot in shots:
        work.put(shot)
    lock = threading.Lock()
    done = [0]
    errors: list[BaseException] = []

    def drain() -> None:
        while not errors:
            try:
                shot = work.get_nowait()
            except queue.Empty:
                return
            outcome = _upload_image(shot, cache)
            with lock:
                done[0] += 1
                if outcome == "uploaded":
                    report.images_uploaded += 1
                elif outcome == "unchanged":
                    report.images_unchanged += 1
                else:
                    report.missing_files.append(shot.image.name)
                progress(f"{label}: image {done[0]}/{len(shots)}")

    def worker() -> None:
        try:
            drain()
        except BaseException as exc:  # noqa: BLE001 — re-raised in the caller
            with lock:
                errors.append(exc)
        finally:
            connections.close_all()  # this thread's connections only

    workers = max(1, min(int(getattr(settings, "DST_PUBLISH_UPLOAD_CONCURRENT", 4)), len(shots)))
    if workers == 1:
        drain()  # on the caller's own connections
        return
    threads = [threading.Thread(target=worker, name=f"publish-upload-{i}") for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]


def _shared_user_id(user, cache: dict[str, int | None]) -> int | None:
    """Users differ per DB (SSO creates them on each instance); match by username."""
    if user is None:
        return None
    if user.username not in cache:
        cache[user.username] = (
            get_user_model()
            .objects.using(SHARED)
            .filter(username=user.username)
            .values_list("pk", flat=True)
            .first()
        )
    return cache[user.username]


def _upsert_job(job: AuditJob, *, parent_id, round_id, users) -> AuditJob:
    values = {name: getattr(job, name) for name in JOB_FIELDS if name != "public_id"}
    values.update(
        parent_job_id=parent_id,
        round_id=round_id,
        created_by_id=_shared_user_id(job.created_by, users),
    )
    shared, _ = AuditJob.objects.using(SHARED).update_or_create(
        public_id=job.public_id, defaults=values
    )
    # auto_now_add overwrites created_at on insert; restore the capture time.
    AuditJob.objects.using(SHARED).filter(pk=shared.pk).update(created_at=job.created_at)
    return shared


def _fingerprint(job: AuditJob, shots: list[AuditScreenshot]) -> tuple:
    """Everything publish copies for one job; equal means nothing to re-send."""
    return (
        tuple(getattr(job, name) for name in JOB_FIELDS),
        tuple(
            sorted(
                (str(s.public_id), s.label, s.image.name, *(getattr(s, n) for n in SHOT_TIME_FIELDS))
                for s in shots
            )
        ),
    )


def _publish_run(
    run: AuditJob,
    shared_round: AuditRound,
    report,
    users,
    progress,
    *,
    only: set[int] | None = None,
    cache: SentCache | None = None,
) -> None:
    """Publish the run's parent and its site jobs (just `only`, when given).

    With a cache, site jobs unchanged since this process last sent them are skipped.
    """
    children = list(run.child_jobs.all())
    if only is not None:
        children = [c for c in children if c.pk in only]
    shots_by_job: dict[int, list[AuditScreenshot]] = {}
    for shot in (
        AuditScreenshot.objects.filter(job__in=[run, *children]).exclude(image="").select_related("job")
    ):
        shots_by_job.setdefault(shot.job_id, []).append(shot)
    prints = {c.pk: _fingerprint(c, shots_by_job.get(c.pk, [])) for c in children}
    if cache is not None:
        with cache.lock:
            fresh = [c for c in children if cache.jobs.get(c.pk) != prints[c.pk]]
        report.sent_live += len(children) - len(fresh)
        children = fresh
    jobs = [run, *children]
    shots = [s for j in jobs for s in shots_by_job.get(j.pk, [])]
    _upload_images(shots, report, progress, f"Run #{run.pk}", cache)

    progress(f"Run #{run.pk}: saving {len(jobs)} jobs, {len(shots)} screenshots")
    with transaction.atomic(using=SHARED):
        shared_parent = _upsert_job(run, parent_id=None, round_id=shared_round.pk, users=users)
        shared_ids = {run.pk: shared_parent.pk}
        for child in jobs[1:]:
            shared_ids[child.pk] = _upsert_job(
                child, parent_id=shared_parent.pk, round_id=shared_round.pk, users=users
            ).pk
        for shot in shots:
            row, _ = AuditScreenshot.objects.using(SHARED).update_or_create(
                public_id=shot.public_id,
                defaults={
                    "job_id": shared_ids[shot.job_id],
                    "label": shot.label,
                    "image": shot.image.name,
                    **{name: getattr(shot, name) for name in SHOT_TIME_FIELDS},
                },
            )
            AuditScreenshot.objects.using(SHARED).filter(pk=row.pk).update(
                created_at=shot.created_at
            )
        # Cameras new to the round get their reviewer now (arrival order);
        # existing review rows are never touched by publish.
        cameras = [
            (c.pole_number, c.target_host)
            for c in sorted(jobs[1:], key=lambda j: (j.created_at, j.pk))
        ]
        report.assigned += len(
            assign_new_cameras(
                shared_round, cameras, reviewers=settings.DST_REVIEWERS, using=SHARED
            )
        )
    if cache is not None:
        with cache.lock:
            cache.jobs.update({c.pk: prints[c.pk] for c in children})
    report.runs += 1
    report.jobs += len(jobs)
    report.screenshots += len(shots)


def _unmatched_poles(runs: list[AuditJob]) -> list[str]:
    from cameras.models import Installation

    poles = sorted({c.pole_number for r in runs for c in r.child_jobs.all() if c.pole_number})
    return [
        p
        for p in poles
        if not Installation.objects.filter(Q(pole_number=p) | Q(identifier=p)).exists()
    ]


def publish_runs(
    run_ids: list[int],
    round_name: str,
    *,
    username: str | None = None,
    progress: Callable[[str], None] = lambda msg: None,
    caches: dict[int, SentCache] | None = None,
) -> PublishReport:
    round_name = clean_round_name(round_name)
    runs = publishable_runs(run_ids)
    progress("Connecting to the shared DB…")
    check_shared_reachable()

    report = PublishReport(round_name=round_name)
    users: dict[str, int | None] = {}
    local_user = (
        get_user_model().objects.filter(username=username).first() if username else None
    )
    shared_round, _ = AuditRound.objects.using(SHARED).get_or_create(
        name=round_name, defaults={"created_by_id": _shared_user_id(local_user, users)}
    )
    local_round, _ = AuditRound.objects.get_or_create(
        name=round_name, defaults={"created_by": local_user}
    )
    for run in runs:
        _publish_run(run, shared_round, report, users, progress, cache=(caches or {}).get(run.pk))
        AuditJob.objects.filter(
            Q(pk=run.pk) | Q(parent_job_id=run.pk)
        ).update(round=local_round)
    report.unmatched_poles = _unmatched_poles(runs)
    logger.info("[PUBLISH] %s", report.summary())
    return report


def start_publish_job(run_ids: list[int], round_name: str, user) -> AuditJob:
    """Run publish_runs on a background thread, tracked as a PUBLISH AuditJob."""
    if not _publish_lock.acquire(blocking=False):
        raise PublishError("A publish is already running.")
    try:
        # Fail fast in the request, not in the thread.
        round_name = clean_round_name(round_name)
        publishable_runs(run_ids)
        job = AuditJob.objects.create(
            pole_number="publish",
            target_host=round_name[:64],
            device_type=AuditJob.DeviceType.PUBLISH,
            status=AuditJob.Status.RUNNING,
            created_by=user,
            started_at=timezone.now(),
            progress_message=f"Publishing {len(run_ids)} run(s)…",
        )
    except Exception:
        _publish_lock.release()
        raise

    threading.Thread(
        target=_execute_publish, args=(job, run_ids, round_name, user), name=f"publish-{job.pk}", daemon=True
    ).start()
    return job


def _execute_publish(
    job: AuditJob, run_ids: list[int], round_name: str, user, caches: dict[int, SentCache] | None = None
) -> None:
    """Publish and record the outcome on job; releases _publish_lock (caller holds it)."""
    from audits.runner import _set_progress

    close_old_connections()
    try:
        report = publish_runs(
            run_ids,
            round_name,
            username=getattr(user, "username", None),
            progress=lambda msg: _set_progress(job.pk, msg),
            caches=caches,
        )
        status, message, error = AuditJob.Status.SUCCEEDED, report.summary(), ""
        if report.missing_files:
            error = "Missing image files: " + ", ".join(report.missing_files)
    except PublishError as exc:
        status, message, error = AuditJob.Status.FAILED, "Publish failed", str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[PUBLISH] job %s crashed", job.pk)
        status, message = AuditJob.Status.FAILED, "Publish failed"
        error = f"{type(exc).__name__}: {exc}"
    finally:
        _publish_lock.release()
    AuditJob.objects.filter(pk=job.pk).update(
        status=status,
        progress_message=message[:255],
        error_message=error[:2000],
        finished_at=timezone.now(),
    )
    if SHARED in connections.databases:
        connections[SHARED].close()
    close_old_connections()


def publish_after_run(
    run_id: int,
    round_name: str,
    user,
    *,
    wait_seconds: float | None = None,
    poll_seconds: float = 30.0,
    reachable: Callable[[], bool] | None = None,
    cache: SentCache | None = None,
) -> AuditJob:
    """Publish a finished run once the shared DB answers (the VPN is off).

    Held publish: while the camera VPN still blocks the shared DB, the PUBLISH
    job waits and says so, then publishes by itself as soon as it can.
    Synchronous; the caller picks the thread.
    """
    import time

    from audits.runner import _set_progress
    from services.netstate import direct_up

    reachable = reachable or direct_up
    if wait_seconds is None:
        wait_seconds = float(getattr(settings, "DST_PUBLISH_WAIT_SECONDS", 12 * 3600))
    round_name = clean_round_name(round_name)
    job = AuditJob.objects.create(
        pole_number="publish",
        target_host=round_name[:64],
        device_type=AuditJob.DeviceType.PUBLISH,
        status=AuditJob.Status.RUNNING,
        created_by=user,
        started_at=timezone.now(),
        progress_message=f"Publishing run #{run_id}…",
    )
    deadline = time.monotonic() + wait_seconds
    while not reachable():
        left = deadline - time.monotonic()
        if left <= 0:
            AuditJob.objects.filter(pk=job.pk).update(
                status=AuditJob.Status.FAILED,
                progress_message="Publish held: shared DB not reachable",
                error_message=(
                    f"The shared DB did not answer for {wait_seconds / 3600:.1f} h (camera VPN "
                    f"still on?). Turn the VPN off and publish run #{run_id} from the DST page."
                ),
                finished_at=timezone.now(),
            )
            job.refresh_from_db()
            return job
        _set_progress(job.pk, f"Waiting for the VPN to be off to publish ({int(left // 60)} min left)…")
        time.sleep(min(poll_seconds, left))
    _publish_lock.acquire()
    _execute_publish(job, [run_id], round_name, user, {run_id: cache} if cache else None)
    job.refresh_from_db()
    return job


class LivePublisher:
    """Publish a running DST audit's sites as they finish, so review fills in as it goes.

    Sites queue up via site_done(). While the shared DB doesn't answer (the LTI
    half runs on the camera VPN) the queue just grows; once it answers, each
    batch goes out on this thread, alongside the capture. A failed batch is
    logged and left to the final publish (those sites aren't in the cache), so
    it never touches the capture. close() stops it and hands back the cache.
    """

    def __init__(
        self,
        run_id: int,
        round_name: str,
        user,
        *,
        reachable: Callable[[], bool] | None = None,
        poll_seconds: float = 15.0,
    ) -> None:
        self.run_id = run_id
        self.round_name = clean_round_name(round_name)
        self.user = user
        self.reachable = reachable
        self.poll_seconds = poll_seconds
        self.cache = SentCache()
        self.pending: list[int] = []
        self.published = 0
        self.closed = False
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._loop, name=f"dst-live-publish-{run_id}", daemon=True)

    def start(self) -> LivePublisher:
        self._thread.start()
        return self

    def site_done(self, child_pk: int) -> None:
        with self._cond:
            self.pending.append(child_pk)
            self._cond.notify()

    def close(self, timeout: float | None = 300.0) -> SentCache:
        """Stop after the batch in flight; the final publish sends whatever is left."""
        with self._cond:
            self.closed = True
            self._cond.notify()
        if self._thread.is_alive():
            self._thread.join(timeout)
        logger.info("[PUBLISH] live run #%s: %s site(s) published live", self.run_id, self.published)
        return self.cache

    def _is_reachable(self) -> bool:
        if self.reachable is not None:
            return self.reachable()
        from services.netstate import direct_up

        return direct_up()

    def _loop(self) -> None:
        close_old_connections()
        try:
            while True:
                with self._cond:
                    while not self.pending and not self.closed:
                        self._cond.wait()
                    if self.closed:
                        return
                if not self._is_reachable():
                    with self._cond:  # the VPN is still up: wait (or close)
                        self._cond.wait(self.poll_seconds)
                    continue
                with self._cond:
                    batch, self.pending = self.pending, []
                self._publish(batch)
        finally:
            connections.close_all()  # this thread's connections only

    def _publish(self, batch: list[int]) -> None:
        try:
            run = AuditJob.objects.get(pk=self.run_id)
            users: dict[str, int | None] = {}
            shared_round, _ = AuditRound.objects.using(SHARED).get_or_create(
                name=self.round_name, defaults={"created_by_id": _shared_user_id(self.user, users)}
            )
            report = PublishReport(round_name=self.round_name)
            _publish_run(run, shared_round, report, users, lambda _m: None, only=set(batch), cache=self.cache)
            self.published += len(batch)
            logger.info(
                "[PUBLISH] live run #%s: %s site(s) to %r, %s image(s) uploaded",
                self.run_id,
                len(batch),
                self.round_name,
                report.images_uploaded,
            )
        except Exception:  # noqa: BLE001 — the final publish retries these sites
            logger.warning("[PUBLISH] live run #%s: batch of %s failed", self.run_id, len(batch), exc_info=True)
            if SHARED in connections.databases:
                connections[SHARED].close()  # a broken connection mustn't poison the next batch
