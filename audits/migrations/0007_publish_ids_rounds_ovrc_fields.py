import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill(apps, schema_editor):
    """Give every existing row a public_id; lift stored OvrC lines into fields."""
    from audits.ovrc_time import ovrc_fields_from_line

    db = schema_editor.connection.alias
    AuditJob = apps.get_model("audits", "AuditJob")
    AuditScreenshot = apps.get_model("audits", "AuditScreenshot")
    for job in AuditJob.objects.using(db).all():
        job.public_id = uuid.uuid4()
        update = ["public_id"]
        # Legacy rows only kept the line, not the scrape time; finished_at is
        # set right after the OvrC pass, so it is the closest reference.
        fields = ovrc_fields_from_line(job.progress_message or "", job.finished_at)
        if fields:
            for key, value in fields.items():
                setattr(job, key, value)
            update += list(fields)
        job.save(update_fields=update)
    for shot in AuditScreenshot.objects.using(db).all():
        shot.public_id = uuid.uuid4()
        shot.save(update_fields=["public_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("audits", "0006_confirm_capture_device_types"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditRound",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=128, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_rounds",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddField(
            model_name="auditjob",
            name="round",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="jobs",
                to="audits.auditround",
            ),
        ),
        migrations.AddField(model_name="auditjob", name="public_id", field=models.UUIDField(null=True, editable=False)),
        migrations.AddField(model_name="auditscreenshot", name="public_id", field=models.UUIDField(null=True, editable=False)),
        migrations.AddField(model_name="auditjob", name="ovrc_fx", field=models.CharField(blank=True, max_length=32)),
        migrations.AddField(model_name="auditjob", name="ovrc_local_time_raw", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="auditjob", name="ovrc_zone", field=models.CharField(blank=True, max_length=8)),
        migrations.AddField(model_name="auditjob", name="ovrc_device_time", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="auditjob", name="ovrc_captured_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="auditjob", name="drift_seconds", field=models.IntegerField(blank=True, null=True)),
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="auditjob",
            name="public_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="auditscreenshot",
            name="public_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
