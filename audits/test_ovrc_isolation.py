"""OvrC fleet pass: one FX failing (FX1031, not a customer) must not end the pass."""

import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from audits import runner
from audits.models import AuditJob
from services import ovrc_capture
from services.ovrc_capture import OvrcClient, OvrcCustomerNotFound, OvrcTimeResult

AT = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def reading(fx: str, out: Path) -> OvrcTimeResult:
    return OvrcTimeResult(
        fx_number=fx,
        local_time="Fri, Oct 30, 2026 08:00 AM EDT",
        screenshot_path=out / f"ovrc_{fx.lower()}.png",
        dashboard_url="https://app.ovrc.com/#/x/dashboard",
        captured_at=AT,
    )


class FakeClient:
    """OvrcClient stand-in: `fail` maps FX → the exception its capture raises."""

    instances: list["FakeClient"] = []

    def __init__(self, *_, fail=None, recover_error=None, **__):
        self.fail = fail or {}
        self.recover_error = recover_error
        self.tried: list[str] = []
        self.recovered = 0
        FakeClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def capture_fx_local_time(self, fx, output_dir, filename=None):
        self.tried.append(fx)
        if fx in self.fail:
            raise self.fail[fx]
        return reading(fx, Path(output_dir))

    def recover(self):
        self.recovered += 1
        if self.recover_error:
            raise self.recover_error


class CaptureFxTimesTests(SimpleTestCase):
    def run_pass(self, fx_list, **client_kw):
        FakeClient.instances.clear()
        with mock.patch.object(ovrc_capture, "OvrcClient", side_effect=lambda *a, **k: FakeClient(**client_kw)):
            return ovrc_capture.capture_fx_times(fx_list, "u", "p", tempfile.gettempdir())

    def test_missing_customer_gets_a_reason_and_the_next_fx_is_read(self):
        missing = OvrcCustomerNotFound("FX1031 is not an OvrC customer (search: No results found)")
        results = self.run_pass(["FX1031", "FX1051"], fail={"FX1031": missing})
        self.assertEqual([r.fx_number for r in results], ["FX1031", "FX1051"])
        self.assertIn("not an OvrC customer", results[0].error)
        self.assertIsNone(results[0].screenshot_path)
        self.assertEqual(results[1].error, "")
        self.assertTrue(results[1].local_time)
        self.assertEqual(FakeClient.instances[0].recovered, 1)  # back to a clean list first

    def test_any_exception_is_isolated_to_its_fx(self):
        results = self.run_pass(["FX1", "FX2", "FX3"], fail={"FX2": TimeoutError("dashboard")})
        self.assertEqual([bool(r.error) for r in results], [False, True, False])
        self.assertIn("TimeoutError: dashboard", results[1].error)

    def test_lost_session_keeps_earlier_readings_and_marks_the_rest(self):
        results = self.run_pass(
            ["FX1", "FX2", "FX3", "FX4"],
            fail={"FX2": RuntimeError("boom")},
            recover_error=RuntimeError("login failed"),
        )
        self.assertEqual([r.fx_number for r in results], ["FX1", "FX2", "FX3", "FX4"])
        self.assertEqual(results[0].error, "")
        self.assertIn("boom", results[1].error)
        self.assertTrue(all("OvrC session lost" in r.error for r in results[2:]))
        self.assertEqual(FakeClient.instances[0].tried, ["FX1", "FX2"])

    def test_cancel_still_stops_the_pass(self):
        from services.teamviewer_capture import CaptureCancelled

        with self.assertRaises(CaptureCancelled):
            self.run_pass(["FX1", "FX2"], fail={"FX1": CaptureCancelled()})


class SearchFailsFastTests(SimpleTestCase):
    def make_client(self, rows_after: int | None, empty=True):
        c = OvrcClient("u", "p")
        c.driver = mock.MagicMock()
        calls = {"n": 0}

        def rows(_fx):
            calls["n"] += 1
            return [mock.MagicMock()] if rows_after is not None and calls["n"] > rows_after else []

        return c, [
            mock.patch.object(c, "_search_box", return_value=mock.MagicMock()),
            mock.patch.object(c, "_customer_rows_matching", side_effect=rows),
            mock.patch.object(c, "_no_results_shown", return_value=empty),
        ]

    def test_no_results_found_raises_in_seconds_not_25(self):
        c, patches = self.make_client(rows_after=None)
        with patches[0], patches[1], patches[2]:
            t0 = time.monotonic()
            with self.assertRaises(OvrcCustomerNotFound) as ctx:
                c._search_customers("FX1031")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("not an OvrC customer", str(ctx.exception))

    def test_a_brief_empty_list_while_filtering_still_finds_the_row(self):
        c, patches = self.make_client(rows_after=3)  # "No results" for ~1.2 s, then the row
        with patches[0], patches[1], patches[2]:
            c._search_customers("FX1051")  # returns, doesn't raise


class WholeFxIdTests(SimpleTestCase):
    """FX140 (I-VBE-0019) isn't an OvrC customer; the search lists FX1400–FX1409 instead."""

    def client_listing(self, *texts):
        c = OvrcClient("u", "p")
        c.driver = mock.MagicMock()
        c.driver.find_elements.return_value = [mock.MagicMock(text=t) for t in texts]
        return c

    def test_fx140_does_not_match_fx1400(self):
        c = self.client_listing("Blue Line - FX1400", "Blue Line - FX1401")
        self.assertEqual(c._customer_rows_matching("FX140"), [])
        self.assertEqual(c._other_customer_ids("FX140"), ["FX1400", "FX1401"])

    def test_the_exact_id_still_matches(self):
        c = self.client_listing("Blue Line - FX1400", "FX140 - Main St (SB)")
        self.assertEqual([r.text for r in c._customer_rows_matching("FX140")], ["FX140 - Main St (SB)"])

    def test_only_longer_ids_listed_is_not_a_customer(self):
        c = self.client_listing("Blue Line - FX1400")
        with (
            mock.patch.object(c, "_search_box", return_value=mock.MagicMock()),
            mock.patch.object(c, "_no_results_shown", return_value=False),
        ):
            t0 = time.monotonic()
            with self.assertRaises(OvrcCustomerNotFound) as ctx:
                c._search_customers("FX140")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("FX140 is not an OvrC customer (search: only FX1400)", str(ctx.exception))


@override_settings(OVRC_USERNAME="u", OVRC_PASSWORD="p")
class FleetPassAttachesPerFxTests(TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dst-ovrc-fleet-"))
        self.parent = AuditJob.objects.create(pole_number="DST-ALL", device_type=AuditJob.DeviceType.DST_AUDIT)
        self.missing = AuditJob.objects.create(
            pole_number="200049", device_type=AuditJob.DeviceType.DST_AUDIT, parent_job=self.parent
        )
        self.ok = AuditJob.objects.create(
            pole_number="220106", device_type=AuditJob.DeviceType.DST_AUDIT, parent_job=self.parent
        )

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_unknown_fx_reason_on_its_site_reading_on_the_other(self):
        results = [
            ovrc_capture._failed_time("FX1031", "FX1031 is not an OvrC customer (search: No results found)"),
            reading("FX1051", self.dir),
        ]
        saved: list[tuple[int, str]] = []
        with (
            mock.patch("services.ovrc_capture.capture_fx_times", return_value=results),
            mock.patch.object(runner, "_save_shot", side_effect=lambda pk, path, label, **kw: saved.append((pk, label))),
            mock.patch.object(runner, "_save_wattbox_shot", return_value=""),
        ):
            errors = runner._dst_fleet_ovrc_times(
                self.parent.pk,
                [(self.missing.pk, "200049", ["FX1031"], self.dir), (self.ok.pk, "220106", ["FX1051"], self.dir)],
                self.dir / "_ovrc_fleet",
            )

        self.assertEqual(errors, ["200049: OvrC FX1031: FX1031 is not an OvrC customer (search: No results found)"])
        self.assertEqual(saved, [(self.ok.pk, "ovrc_fx1051")])
        self.missing.refresh_from_db()
        self.ok.refresh_from_db()
        self.assertIn("not an OvrC customer", self.missing.error_message)
        self.assertEqual(self.ok.error_message, "")  # its reading was saved (above), no warning
