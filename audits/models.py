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
