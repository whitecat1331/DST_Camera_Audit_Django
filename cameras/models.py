from django.conf import settings
from django.db import models

from services.ip_map import cbw_ip, tf_cpu_ips


IMS_ROLE_PRIORITY = {
    "developer": 6,
    "admin": 5,
    "superuser": 4,
    "user": 3,
    "technician": 2,
    "readonly": 1,
}


class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    ims_role = models.CharField(max_length=64, blank=True, default="readonly")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.user.username} ({self.ims_role})"

    def role_at_least(self, min_role: str) -> bool:
        return IMS_ROLE_PRIORITY.get(self.ims_role, 0) >= IMS_ROLE_PRIORITY.get(min_role, 99)


class Installation(models.Model):
    ims_id = models.IntegerField(unique=True, db_index=True)
    identifier = models.CharField(max_length=128, blank=True, db_index=True)
    primary_platform = models.CharField(max_length=64, blank=True, db_index=True)
    all_platforms = models.CharField(max_length=255, blank=True)
    pole_number = models.CharField(max_length=64, blank=True, db_index=True)
    serial_number = models.CharField(max_length=128, blank=True, db_index=True)
    fl_number = models.CharField(max_length=64, blank=True)
    ip_address = models.CharField(max_length=64, blank=True)
    state = models.CharField(max_length=64, blank=True, db_index=True)
    agency = models.CharField(max_length=128, blank=True, db_index=True)
    location = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=64, blank=True, db_index=True)
    gps_lat = models.FloatField(null=True, blank=True)
    gps_long = models.FloatField(null=True, blank=True)
    vendor = models.CharField(max_length=128, blank=True)
    model = models.CharField(max_length=128, blank=True)
    camera_a = models.CharField(max_length=128, blank=True, default="")
    camera_b = models.CharField(max_length=128, blank=True, default="")
    camera_c = models.CharField(max_length=128, blank=True, default="")
    tf_a_ip = models.CharField(max_length=64, blank=True, default="")
    tf_b_ip = models.CharField(max_length=64, blank=True, default="")
    ims_updated_at = models.CharField(max_length=64, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ["pole_number", "identifier"]

    def __str__(self) -> str:
        return f"{self.identifier or self.serial_number} (pole {self.pole_number})"

    @property
    def derived_cbw_ip(self) -> str | None:
        try:
            return cbw_ip(self.pole_number)
        except ValueError:
            return None

    @property
    def derived_tf_cpu_ips(self) -> list[tuple[str, str]]:
        try:
            return tf_cpu_ips(self.pole_number)
        except ValueError:
            return []

    @property
    def has_gps(self) -> bool:
        return self.gps_lat is not None and self.gps_long is not None

    @property
    def is_dragoneye(self) -> bool:
        platform = (self.primary_platform or "").strip().upper()
        # VBE parents host DragonEye (FX) cameras on child enclosures.
        if platform in {"DE", "DRAGONEYE", "DRAGON EYE", "VBE"}:
            return True
        vendor = (self.vendor or "").strip().lower()
        return "dragoneye" in vendor or "dragon eye" in vendor

    @property
    def is_vbe(self) -> bool:
        platform = (self.primary_platform or "").strip().upper()
        if platform == "VBE":
            return True
        ident = (self.identifier or "").strip().upper()
        return ident.startswith("I-VBE-") or ident.startswith("VBE")

    @property
    def fx_number(self) -> str | None:
        """First FX#### found on this installation (serial / cameras / identifier)."""
        numbers = self.fx_numbers
        return numbers[0] if numbers else None

    @property
    def fx_numbers(self) -> list[str]:
        """All distinct FX#### values from serial, cameras, identifier, and devices."""
        import re

        found: list[str] = []
        seen: set[str] = set()

        def add_from(raw: str | None) -> None:
            for m in re.finditer(r"(FX\d+)", (raw or "").upper()):
                fx = m.group(1)
                if fx not in seen:
                    seen.add(fx)
                    found.append(fx)

        for raw in (self.serial_number, self.camera_a, self.camera_b, self.camera_c, self.identifier):
            add_from(raw)
        if self.pk:
            for d in self.devices.all():
                add_from(d.unit_serial)
                add_from(d.name)
        return found


class DragonEyeTeamViewerId(models.Model):
    """FX → TeamViewer ID mapping loaded from DragonEye Teamviewer IDs.csv."""

    fx_number = models.CharField(max_length=32, db_index=True)
    lane = models.CharField(max_length=8, blank=True, default="")  # L1, L2, …
    teamviewer_id = models.CharField(max_length=64)
    label = models.CharField(max_length=255, blank=True)
    source_filename = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["fx_number", "lane"]
        constraints = [
            models.UniqueConstraint(
                fields=["fx_number", "lane"],
                name="uniq_dragoneye_fx_lane",
            ),
        ]

    def __str__(self) -> str:
        lane = f" {self.lane}" if self.lane else ""
        return f"{self.fx_number}{lane} → {self.teamviewer_id}"

    @property
    def thumb_key(self) -> str:
        if self.lane:
            return f"de_{self.lane.lower()}"
        return "de_tv"


class InstallationDevice(models.Model):
    """PRTG enclosure device mirrored from IMS for an ASE installation."""

    installation = models.ForeignKey(
        Installation,
        on_delete=models.CASCADE,
        related_name="devices",
    )
    ims_device_id = models.IntegerField(db_index=True)
    enclosure_id = models.IntegerField(default=0)
    enclosure_label = models.CharField(max_length=255, blank=True)
    lane_code = models.CharField(max_length=32, blank=True)
    unit_serial = models.CharField(max_length=128, blank=True)
    prtg_objid = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=255, blank=True)
    host = models.CharField(max_length=64, blank=True, db_index=True)
    device_type = models.CharField(max_length=64, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["installation", "ims_device_id"],
                name="uniq_installation_ims_device",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.host})"


class InstallationSensor(models.Model):
    device = models.ForeignKey(
        InstallationDevice,
        on_delete=models.CASCADE,
        related_name="sensors",
    )
    prtg_objid = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=255, blank=True)
    last_value = models.CharField(max_length=255, blank=True)
    sensor_type = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name}={self.last_value}"


class InstallationComponent(models.Model):
    """Non-enclosure peripheral component from IMS (RFS/FLS/etc.)."""

    installation = models.ForeignKey(
        Installation,
        on_delete=models.CASCADE,
        related_name="components",
    )
    name = models.CharField(max_length=255, blank=True)
    host = models.CharField(max_length=64, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self) -> str:
        return self.name or self.host or "component"


class SyncState(models.Model):
    key = models.CharField(max_length=64, unique=True)
    value = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.key}={self.value}"
