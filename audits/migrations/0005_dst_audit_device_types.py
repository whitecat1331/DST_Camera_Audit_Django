# Generated manually for DST fleet audit device types + parent_job

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("audits", "0004_ovrc_vnc_device_types"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auditjob",
            name="device_type",
            field=models.CharField(
                choices=[
                    ("cbw", "CBW"),
                    ("tf_vnc", "TF VNC"),
                    ("vnc_bundle", "VNC L1/L2"),
                    ("pole_bundle", "Pole capture (CBW + VNC L1/L2)"),
                    ("de_tv", "DragonEye TeamViewer"),
                    ("de_bundle", "DragonEye capture (TeamViewer lanes)"),
                    ("ovrc", "OvrC local time"),
                    ("vbe_daily", "VBE Daily Checks (one site)"),
                    ("vbe_daily_all", "VBE Daily Checks (all sites)"),
                    ("dst_audit", "DST Audit (all sites)"),
                    ("dst_site", "DST Audit (one site)"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="auditjob",
            name="parent_job",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="child_jobs",
                to="audits.auditjob",
            ),
        ),
    ]
