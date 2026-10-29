from datetime import datetime, timezone

from django.test import SimpleTestCase

from audits.ovrc_time import (
    OVRC_FIELD_NAMES,
    ovrc_fields,
    ovrc_fields_from_line,
    parse_ovrc_local,
)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class ParseOvrcLocalTests(SimpleTestCase):
    def test_current_scrape_format(self):
        wall, zone = parse_ovrc_local("Sun, Aug 02, 2026 01:11 AM EDT")
        self.assertEqual(wall, datetime(2026, 8, 2, 1, 11))
        self.assertEqual(zone, "EDT")

    def test_whitespace_is_normalized(self):
        wall, zone = parse_ovrc_local("  Sat, Aug 01, 2026   11:47 PM   ADT ")
        self.assertEqual(wall, datetime(2026, 8, 1, 23, 47))
        self.assertEqual(zone, "ADT")

    def test_legacy_converted_line(self):
        wall, zone = parse_ovrc_local("Aug 1, 2026, 11:47 p.m. EST")
        self.assertEqual(wall, datetime(2026, 8, 1, 23, 47))
        self.assertEqual(zone, "EST")

    def test_unknown_zone_keeps_wall_clock(self):
        wall, zone = parse_ovrc_local("Sun, Aug 02, 2026 01:11 AM")
        self.assertEqual(wall, datetime(2026, 8, 2, 1, 11))
        self.assertEqual(zone, "")

    def test_garbage(self):
        self.assertEqual(parse_ovrc_local("not a time"), (None, ""))
        self.assertEqual(parse_ovrc_local(""), (None, ""))


class OvrcFieldsTests(SimpleTestCase):
    def test_keys_match_model_field_names(self):
        self.assertEqual(tuple(ovrc_fields("FX1", "", None)), OVRC_FIELD_NAMES)

    def test_correct_device_has_no_drift(self):
        # 05:11 UTC on Aug 2 is 01:11 EDT.
        f = ovrc_fields("fx1260", "Sun, Aug 02, 2026 01:11 AM EDT", utc(2026, 8, 2, 5, 11))
        self.assertEqual(f["ovrc_fx"], "FX1260")
        self.assertEqual(f["ovrc_zone"], "EDT")
        self.assertEqual(f["drift_seconds"], 0)
        self.assertEqual(f["ovrc_device_time"], utc(2026, 8, 2, 5, 11))

    def test_wrong_zone_shows_wall_clock_drift(self):
        # Real case: FX1261 labelled ADT reads 02:10 at 05:11 UTC (01:11 Eastern).
        # Absolute time is ~right; the wall clock is ~an hour ahead.
        f = ovrc_fields("FX1261", "Sun, Aug 02, 2026 02:10 AM ADT", utc(2026, 8, 2, 5, 11))
        self.assertEqual(f["drift_seconds"], 59 * 60)
        self.assertEqual(f["ovrc_device_time"], utc(2026, 8, 2, 5, 10))

    def test_device_behind_is_negative(self):
        # 2026-11-01 07:30 UTC is 02:30 EST (after fall back); a device reading
        # 01:30 is an hour behind Eastern wall clock.
        f = ovrc_fields("FX9", "Sun, Nov 01, 2026 01:30 AM EST", utc(2026, 11, 1, 7, 30))
        self.assertEqual(f["drift_seconds"], -3600)

    def test_no_reference_time_means_no_drift(self):
        f = ovrc_fields("FX1", "Sun, Aug 02, 2026 01:11 AM EDT", None)
        self.assertIsNone(f["drift_seconds"])
        self.assertIsNotNone(f["ovrc_device_time"])

    def test_unparseable_raw(self):
        f = ovrc_fields("FX1", "n/a", utc(2026, 8, 2, 5, 11))
        self.assertIsNone(f["ovrc_device_time"])
        self.assertIsNone(f["drift_seconds"])
        self.assertEqual(f["ovrc_local_time_raw"], "n/a")


class OvrcFieldsFromLineTests(SimpleTestCase):
    def test_stored_progress_line(self):
        f = ovrc_fields_from_line(
            "OvrC FX1260: Sun, Aug 02, 2026 01:11 AM EDT", utc(2026, 8, 2, 5, 11)
        )
        self.assertEqual(f["ovrc_fx"], "FX1260")
        self.assertEqual(f["drift_seconds"], 0)

    def test_line_with_status_suffix(self):
        f = ovrc_fields_from_line("OvrC FX12: Sun, Aug 02, 2026 01:11 AM EDT · Done", None)
        self.assertEqual(f["ovrc_local_time_raw"], "Sun, Aug 02, 2026 01:11 AM EDT")

    def test_non_ovrc_lines(self):
        for line in ("Done", "Failed", "CBW failed: RuntimeError", ""):
            self.assertIsNone(ovrc_fields_from_line(line, None))
