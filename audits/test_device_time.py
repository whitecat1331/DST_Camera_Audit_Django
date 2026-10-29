"""Per-device time check: site timezone, drift against the site's wall clock, device readings."""

from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless

from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image

from audits.models import AuditJob, AuditScreenshot

from audits import device_drift
from audits.site_timezone import CENTRAL, EASTERN, PACIFIC, site_timezone
from services import device_clock


def inst(state, agency="", lat=None, lon=None):
    return SimpleNamespace(state=state, agency=agency, gps_lat=lat, gps_long=lon)


class SiteTimezoneTests(SimpleTestCase):
    def test_split_state_agency_list_wins(self):
        self.assertEqual(site_timezone(inst("Tennessee", "Smithville TN"))[0], CENTRAL)
        self.assertEqual(site_timezone(inst("Tennessee", "Knoxville"))[0], EASTERN)
        self.assertEqual(site_timezone(inst("Florida", "Shalimar"))[0], CENTRAL)
        self.assertEqual(site_timezone(inst("Florida", "Hollywood"))[0], EASTERN)

    def test_gps_used_when_valid_for_the_state(self):
        # Shreveport, LA.
        self.assertEqual(site_timezone(inst("Louisiana", "X", 32.5252, -93.7502)), (CENTRAL, "GPS"))

    def test_gps_outside_the_state_falls_back_to_the_state(self):
        # Bad coordinates (2026-10: Iowa sites with GPS in Eastern): Atlanta's.
        zone, why = site_timezone(inst("Iowa", "X", 33.749, -84.388))
        self.assertEqual(zone, CENTRAL)
        self.assertIn("not valid for Iowa", why)

    def test_no_gps_uses_the_state(self):
        self.assertEqual(site_timezone(inst("California", "X"))[0], PACIFIC)

    def test_unknown_state_assumes_eastern_and_says_so(self):
        zone, why = site_timezone(inst("R&D"))
        self.assertEqual(zone, EASTERN)
        self.assertIn("assumed Eastern", why)


AT = datetime(2026, 10, 29, 15, 12, 19, tzinfo=dt_timezone.utc)  # 11:12:19 EDT, 10:12:19 CDT


class DriftTests(SimpleTestCase):
    def test_central_site_on_central_time_passes(self):
        d = device_drift.drift_seconds("2026-10-29T10:12:19", AT, CENTRAL)
        self.assertEqual((d, device_drift.verdict(d)), (0, "pass"))

    def test_the_same_clock_against_eastern_is_an_hour_off(self):
        d = device_drift.drift_seconds("2026-10-29T10:12:19", AT, EASTERN)
        self.assertEqual((d, device_drift.verdict(d)), (-3600, "fail"))

    def test_device_still_on_daylight_time_after_fall_back(self):
        # 2026-11-01 13:00 UTC = 08:00 EST; a clock that missed the change shows 09:00.
        at = datetime(2026, 11, 1, 13, 0, tzinfo=dt_timezone.utc)
        self.assertEqual(device_drift.drift_seconds("2026-11-01T09:00:00", at, EASTERN), 3600)

    def test_within_two_minutes_passes_beyond_fails(self):
        self.assertEqual(device_drift.verdict(120), "pass")
        self.assertEqual(device_drift.verdict(-121), "fail")

    def test_unread_device_is_neither_pass_nor_fail(self):
        self.assertIsNone(device_drift.drift_seconds("", AT, EASTERN))
        self.assertEqual(device_drift.verdict(None), "unread")

    def test_blank_or_bad_zone_falls_back_to_eastern(self):
        self.assertEqual(device_drift.drift_seconds("2026-10-29T11:12:19", AT, ""), 0)
        self.assertEqual(device_drift.drift_seconds("2026-10-29T11:12:19", AT, "Nowhere/Zone"), 0)


class DeviceReadingTests(SimpleTestCase):
    def test_cbw_page_current_time(self):
        self.assertEqual(
            device_clock.parse_cbw_current("Thu, 29 Oct 2026 09:12:19"), datetime(2026, 10, 29, 9, 12, 19)
        )
        self.assertIsNone(device_clock.parse_cbw_current("Sync with NTP server"))

    def test_cbw_sidecar_round_trip(self):
        import tempfile
        from pathlib import Path

        image = Path(tempfile.mkdtemp()) / "cbw.png"
        device_clock.write_clock_sidecar(image, "Thu, 29 Oct 2026 09:12:19")
        reading = device_clock.read_capture("cbw", image, datetime(2026, 10, 29, 9, 12))
        self.assertEqual((reading.iso, reading.source), ("2026-10-29T09:12:19", "page"))

    def test_cbw_without_sidecar_is_unread(self):
        from pathlib import Path

        self.assertIsNone(device_clock.read_capture("cbw", Path("nowhere/cbw.png"), datetime(2026, 10, 29)))


    def test_ovrc_reading(self):
        from audits.runner import _ovrc_reading

        r = _ovrc_reading("Sun, Aug 02, 2026 01:11 AM EDT")
        self.assertEqual((r.iso, r.source), ("2026-08-02T01:11:00", "ovrc"))
        self.assertIsNone(_ovrc_reading("—"))


CLOCK_FIXTURES = Path(__file__).resolve().parent / "testdata" / "clock"


def _ocr_available() -> bool:
    try:
        from winrt.windows.media.ocr import OcrEngine

        return OcrEngine.try_create_from_user_profile_languages() is not None
    except Exception:  # noqa: BLE001
        return False


@skipUnless(_ocr_available(), "Windows OCR not available")
class OcrReaderTests(SimpleTestCase):
    """Real captures: the clock each one shows, or unread (never a wrong time)."""

    EXPECTED = datetime(2026, 10, 29, 10, 13)

    def read(self, label, name):
        return device_clock.read_capture(label, CLOCK_FIXTURES / name, self.EXPECTED)

    def test_vnc_clock_under_the_bottom_date(self):
        reading = self.read("vnc_l1", "vnc_1048_05.png")
        self.assertEqual((reading.iso, reading.source), ("2026-10-29T10:48:05", "ocr"))

    def test_vnc_clock_ocr_cannot_read_is_unread(self):
        # OCR sees only "AM" on this line (Phase 4 of PER_DEVICE_TIME_CHECK).
        self.assertIsNone(self.read("vnc_l1", "vnc_1012_01.png"))

    def test_dragoncam_top_bar_clock(self):
        reading = self.read("de_l1", "dragoncam_1015_02.png")
        self.assertEqual((reading.iso, reading.source), ("2026-10-29T10:15:00", "ocr-min"))

    def test_dragoncam_ignores_the_lagging_status_bar_time(self):
        # Status bar says "Time: 10:50:00" (last event); the system clock says 10:52.
        reading = self.read("de_l1", "dragoncam_bar_lags_1052.png")
        self.assertEqual(reading.iso, "2026-10-29T10:52:00")

    def test_dragoncam_without_a_clock_is_unread(self):
        import tempfile

        blank = Path(tempfile.mkdtemp()) / "de_l1.png"
        Image.new("RGB", (800, 600), "white").save(blank)
        self.assertIsNone(device_clock.read_capture("de_l1", blank, self.EXPECTED))

    def test_other_labels_are_not_read(self):
        self.assertIsNone(self.read("tv_status", "dragoncam_1015_02.png"))


class SaveShotTests(TestCase):
    """_save_shot keeps when the image was grabbed and the clock it shows."""

    databases = {"default"}

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.media = tempfile.mkdtemp(prefix="dst-shot-test-")
        self.job = AuditJob.objects.create(
            pole_number="233121", device_type=AuditJob.DeviceType.DST_SITE, site_tz=CENTRAL
        )
        self.png = Path(self.media) / "cbw.png"
        Image.new("RGB", (10, 10)).save(self.png)

    def save(self, label, **kw):
        from audits.runner import _save_shot

        with override_settings(MEDIA_ROOT=self.media):
            _save_shot(self.job.pk, self.png, label, **kw)
        return AuditScreenshot.objects.get(job=self.job, label=label)

    def test_capture_time_is_the_image_write_time(self):
        import os

        grabbed = datetime(2026, 10, 29, 15, 12, 19, tzinfo=dt_timezone.utc)
        os.utime(self.png, (grabbed.timestamp(), grabbed.timestamp()))
        device_clock.write_clock_sidecar(self.png, "Thu, 29 Oct 2026 10:12:19")
        shot = self.save("cbw")
        self.assertEqual(shot.captured_at, grabbed)
        self.assertEqual((shot.device_time, shot.device_time_source), ("2026-10-29T10:12:19", "page"))
        self.assertEqual(device_drift.drift_seconds(shot.device_time, shot.captured_at, self.job.site_tz), 0)

    def test_ovrc_passes_its_own_reading_and_time(self):
        from audits.runner import _ovrc_reading

        at = datetime(2026, 10, 29, 15, 0, tzinfo=dt_timezone.utc)
        shot = self.save("ovrc_fx1408", captured_at=at, reading=_ovrc_reading("Thu, Oct 29, 2026 10:00 AM CDT"))
        self.assertEqual((shot.captured_at, shot.device_time, shot.device_time_source), (at, "2026-10-29T10:00:00", "ovrc"))

    def test_unreadable_capture_leaves_the_device_time_blank(self):
        shot = self.save("cbw")  # no page text kept
        self.assertEqual((shot.device_time, shot.device_time_source), ("", ""))
