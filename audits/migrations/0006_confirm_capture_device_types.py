# Generated manually for Confirm Captures device types

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("audits", "0005_dst_audit_device_types"),
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
                    ("confirm_batch", "Confirm Captures (batch)"),
                    ("confirm_site", "Confirm Capture (one site)"),
                ],
                max_length=16,
            ),
        ),
    ]
