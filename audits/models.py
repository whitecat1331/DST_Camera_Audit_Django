from django.conf import settings
from django.db import models


class AuditJob(models.Model):
    class DeviceType(models.TextChoices):
        CBW = "cbw", "CBW"
        TF_VNC = "tf_vnc", "TF VNC"
        POLE_BUNDLE = "pole_bundle", "Pole capture (CBW + VNC L1/L2)"
        DE_TV = "de_tv", "DragonEye TeamViewer"
        DE_BUNDLE = "de_bundle", "DragonEye capture (TeamViewer lanes)"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

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
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

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

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.label} (job {self.job_id})"
