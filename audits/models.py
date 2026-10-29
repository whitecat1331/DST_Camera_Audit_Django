import uuid

from django.conf import settings
from django.db import models


class AuditRound(models.Model):
    """A named DST audit round (e.g. one time change) grouping published runs."""

    name = models.CharField(max_length=128, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_rounds",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Reviewer usernames snapshotted at the round's first assignment, so a later
    # team change never reshuffles an existing round (see audits/assignment.py).
    reviewers = models.JSONField(default=list, blank=True)
    # Per site type: the reviewer index its sequence starts at.
    assignment_offsets = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name


class AuditJob(models.Model):
    class DeviceType(models.TextChoices):
        CBW = "cbw", "CBW"
        TF_VNC = "tf_vnc", "TF VNC"
        VNC_BUNDLE = "vnc_bundle", "VNC L1/L2"
        POLE_BUNDLE = "pole_bundle", "Pole capture (CBW + VNC L1/L2)"
        DE_TV = "de_tv", "DragonEye TeamViewer"
        DE_BUNDLE = "de_bundle", "DragonEye capture (TeamViewer lanes)"
        OVRC = "ovrc", "OvrC local time"
        VBE_DAILY = "vbe_daily", "VBE Daily Checks (one site)"
        VBE_DAILY_ALL = "vbe_daily_all", "VBE Daily Checks (all sites)"
        DST_AUDIT = "dst_audit", "DST Audit (all sites)"
        DST_SITE = "dst_site", "DST Audit (one site)"
        CONFIRM_BATCH = "confirm_batch", "Confirm Captures (batch)"
        CONFIRM_SITE = "confirm_site", "Confirm Capture (one site)"
        REJECTION_REPORT = "rejection_report", "Rejection CSV Report"
        REJECTION_SITE = "rejection_site", "Rejection CSV (one camera)"
        PUBLISH = "publish", "Publish to shared DB"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        # DST site not attempted: known offline before capture (power-on failed).
        SKIPPED = "skipped", "Skipped"

    pole_number = models.CharField(max_length=64, db_index=True)
    target_host = models.CharField(max_length=64, blank=True)
    device_type = models.CharField(max_length=16, choices=DeviceType.choices)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    error_message = models.TextField(blank=True)
    progress_message = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_jobs",
    )
    parent_job = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="child_jobs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Stable identity across the local and shared databases (publish upsert key).
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    round = models.ForeignKey(
        AuditRound,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="jobs",
    )
    # Structured OvrC reading (see audits/ovrc_time.py). drift_seconds is the
    # device wall clock minus America/New_York wall clock at ovrc_captured_at.
    ovrc_fx = models.CharField(max_length=32, blank=True)
    ovrc_local_time_raw = models.CharField(max_length=64, blank=True)
    ovrc_zone = models.CharField(max_length=8, blank=True)
    ovrc_device_time = models.DateTimeField(null=True, blank=True)
    ovrc_captured_at = models.DateTimeField(null=True, blank=True)
    drift_seconds = models.IntegerField(null=True, blank=True)
    # The site's own IANA zone (audits/site_timezone.py), set at capture: what
    # each device's clock should follow. Blank on runs before it existed.
    site_tz = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Audit {self.pk} {self.device_type} pole {self.pole_number} ({self.status})"


def audit_upload_to(instance: "AuditScreenshot", filename: str) -> str:
    return f"audits/{instance.job_id}/{filename}"


class AuditScreenshot(models.Model):
    job = models.ForeignKey(
        AuditJob,
        on_delete=models.CASCADE,
        related_name="screenshots",
    )
    label = models.CharField(max_length=128)
    image = models.ImageField(upload_to=audit_upload_to)
    created_at = models.DateTimeField(auto_now_add=True)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    # When the image was grabbed (created_at is when the row was saved, seconds later).
    captured_at = models.DateTimeField(null=True, blank=True)
    # The device's own clock as shown in this capture (services/device_clock.py):
    # the text read, the wall clock as ISO "YYYY-MM-DDTHH:MM:SS" (no zone: it's
    # what the device displays), and where it came from (page / ocr / ovrc).
    device_time_raw = models.CharField(max_length=128, blank=True)
    device_time = models.CharField(max_length=19, blank=True)
    device_time_source = models.CharField(max_length=16, blank=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.label} (job {self.job_id})"


class StoredFile(models.Model):
    """Image bytes in the shared DB, keyed by the local storage name.

    Written by publish (capture role); served by the review instance.
    """

    name = models.CharField(max_length=255, unique=True)
    content_type = models.CharField(max_length=64)
    size = models.IntegerField()
    sha256 = models.CharField(max_length=64)
    data = models.BinaryField()
    thumb = models.BinaryField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.size} bytes)"


class InstallationAuditReview(models.Model):
    """Shared review state for one camera in one round.

    Created by publish when a camera first enters a round, with its assigned
    reviewer; publish never updates existing shared rows, so assignments,
    checkmarks and notes survive re-publishing. The laptop's copy is a mirror
    of the shared rows, refreshed by each publish (audits/publish.py), so its
    own review page shows the same state. Usernames are stored as strings
    because users differ per database (each instance creates them at SSO
    sign-in).
    """

    round = models.ForeignKey(AuditRound, on_delete=models.CASCADE, related_name="reviews")
    camera_key = models.CharField(max_length=64)
    kind = models.CharField(max_length=8, blank=True)
    assignment_seq = models.IntegerField(null=True, blank=True)
    assigned_to = models.CharField(max_length=150, blank=True, db_index=True)
    assigned_by = models.CharField(max_length=150, blank=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    completed = models.BooleanField(default=False)
    completed_by = models.CharField(max_length=150, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    notes_updated_by = models.CharField(max_length=150, blank=True)
    notes_updated_at = models.DateTimeField(null=True, blank=True)
    # Optimistic concurrency: every edit must carry the version it read.
    version = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["round", "camera_key"], name="uniq_review_round_camera"),
        ]

    def __str__(self) -> str:
        return f"{self.round} / {self.camera_key} -> {self.assigned_to or 'unassigned'}"


class ScheduledAudit(models.Model):
    """A one-time DST audit fired by Windows Task Scheduler on the laptop's clock.

    Capture laptop only. Task Scheduler owns the timing (wake, local clock and
    time changes); this row owns what to run and what happened.
    """

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    fire_at = models.DateTimeField()
    scope = models.CharField(max_length=16, default="all")
    state = models.CharField(max_length=64, blank=True)
    agency = models.CharField(max_length=128, blank=True)
    installation_ids = models.JSONField(default=list)
    publish_round = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.SCHEDULED)
    message = models.CharField(max_length=500, blank=True)
    # The run_scheduled_audit process while running (a runserver restart must
    # not reap a live scheduled run's jobs).
    pid = models.IntegerField(null=True, blank=True)
    job = models.ForeignKey(
        AuditJob, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["fire_at"]

    def __str__(self) -> str:
        return f"Scheduled audit #{self.pk} at {self.fire_at:%Y-%m-%d %H:%M}"

    @property
    def task_name(self) -> str:
        return f"DST scheduled audit {self.pk}"
