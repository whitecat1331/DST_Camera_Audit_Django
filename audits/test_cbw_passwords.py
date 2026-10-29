"""CBW password order: remembered number first, each password once per round, a time budget."""

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings
from selenium.common.exceptions import TimeoutException

from services import cbw_capture

HINTS = Path(tempfile.mkdtemp(prefix="cbw-hints-")) / "hints.json"


@override_settings(CBW_PASSWORD_HINTS_PATH=str(HINTS), CBW_CAPTURE_BUDGET_SECONDS=0)
class CbwPasswordOrderTests(SimpleTestCase):
    def setUp(self):
        HINTS.unlink(missing_ok=True)
        self.tried: list[str] = []

    def capture(self, works="p3", probe_ok=True, **kw):
        def shot(url, user, password, out, **_):
            self.tried.append(password)
            if password == works:
                return Path(out) / "cbw.png"
            raise TimeoutException("menu")  # wrong password shows up as a menu timeout

        with (
            mock.patch.object(cbw_capture, "_probe_cbw_auth", return_value=probe_ok),
            mock.patch.object(cbw_capture, "screenshot_date_time", side_effect=shot),
            mock.patch.object(cbw_capture, "_has_clock", return_value=True),
        ):
            return cbw_capture.capture_cbw_with_passwords(
                "10.1.54.177", "admin", ["p1", "p2", "p3"], tempfile.gettempdir(), **kw
            )

    def test_each_password_once_before_any_retry_and_the_winner_is_remembered(self):
        self.capture()
        self.assertEqual(self.tried, ["p1", "p2", "p3"])  # was p1 ×3, p2 ×3, then p3
        self.assertEqual(json.loads(HINTS.read_text()), {"10.1.54.177": 3})
        self.assertNotIn("p3", HINTS.read_text())  # the number, never the password

    def test_remembered_password_goes_first(self):
        HINTS.write_text(json.dumps({"10.1.54.177": 3}))
        self.capture()
        self.assertEqual(self.tried, ["p3"])

    def test_all_timing_out_retries_each_round_then_fails(self):
        with self.assertRaisesMessage(RuntimeError, "CBW capture failed for 10.1.54.177"):
            self.capture(works="none")
        self.assertEqual(self.tried, ["p1", "p2", "p3"] * 3)

    def test_budget_stops_new_attempts(self):
        with mock.patch.object(cbw_capture.time, "monotonic", side_effect=[0, 0, 200, 200]):
            with self.assertRaisesMessage(RuntimeError, "gave up after 150s"):
                self.capture(works="none", budget_seconds=150)
        self.assertEqual(self.tried, ["p1"])


class DateTimePageWaitTests(SimpleTestCase):
    """The screenshot waits for the Date/Time content, not just a visible #mainContent."""

    def test_waits_for_current_clock_text(self):
        def main_content(text):
            return mock.Mock(find_element=mock.Mock(return_value=mock.Mock(text=text)))

        wait = mock.Mock()
        self.assertTrue(cbw_capture._wait_for_date_time_page(wait, "10.1.36.177"))
        (cond,), _ = wait.until.call_args
        self.assertFalse(cond(main_content("Part Number: X-410-I  Firmware Revision: 2.5")))
        self.assertTrue(cond(main_content("Current: Thu, 29 Oct 2026 13:12:31  Set:")))

    def test_slow_page_is_logged_not_raised(self):
        wait = mock.Mock(until=mock.Mock(side_effect=TimeoutException("Date/Time")))
        with self.assertLogs("services.cbw_capture", level="WARNING") as logs:
            self.assertFalse(cbw_capture._wait_for_date_time_page(wait, "10.1.36.177"))
        self.assertIn("Date/Time page did not load", logs.output[0])


class CbwDateTimeClickTests(SimpleTestCase):
    def test_a_click_that_loads_nothing_is_retried_on_a_fresh_link(self):
        """233201 (2026-10-29): a late menu rebuild swallowed the click twice in a row."""
        driver = mock.Mock()
        finds = []

        def find_links():
            finds.append(object())
            return [finds[-1]]

        with mock.patch.object(cbw_capture, "_wait_for_date_time_page", side_effect=[False, True]):
            self.assertTrue(cbw_capture._open_date_time(driver, find_links, "10.3.201.177"))
        clicked = [c.args[1] for c in driver.execute_script.call_args_list]
        self.assertEqual(clicked, finds)  # each click on the element found just before it
        self.assertEqual(len(clicked), 2)

    def test_gives_up_after_the_last_click(self):
        driver = mock.Mock()
        with mock.patch.object(cbw_capture, "_wait_for_date_time_page", return_value=False):
            self.assertFalse(cbw_capture._open_date_time(driver, lambda: [object()], "h"))
        self.assertEqual(driver.execute_script.call_count, cbw_capture._DATE_TIME_CLICKS)


@override_settings(CBW_PASSWORD_HINTS_PATH=str(HINTS), CBW_CAPTURE_BUDGET_SECONDS=0)
class CbwRecaptureTests(SimpleTestCase):
    """No "Current:" clock means the Date/Time page never loaded: capture once more."""

    def run_capture(self, clocks):
        out = Path(tempfile.mkdtemp(prefix="cbw-recap-"))
        calls = []

        def shot(url, user, password, out_dir, filename="cbw.png", **_):
            calls.append(filename)
            path = Path(out_dir) / filename
            path.write_bytes(f"image {len(calls)}".encode())
            Path(str(path) + ".clock.txt").write_text(clocks[len(calls) - 1], encoding="utf-8")
            return path

        with (
            mock.patch.object(cbw_capture, "_probe_cbw_auth", return_value=True),
            mock.patch.object(cbw_capture, "screenshot_date_time", side_effect=shot),
        ):
            path = cbw_capture.capture_cbw_with_passwords("10.3.201.177", "admin", ["p1"], out)
        return path, calls, sorted(f.name for f in out.iterdir())

    def test_a_capture_with_a_clock_is_not_repeated(self):
        _path, calls, _files = self.run_capture(["Thu, 29 Oct 2026 14:39:59"])
        self.assertEqual(calls, ["cbw.png"])

    def test_a_recapture_with_the_clock_replaces_the_first(self):
        path, calls, files = self.run_capture(["", "Thu, 29 Oct 2026 14:40:41"])
        self.assertEqual(calls, ["cbw.png", "retry_cbw.png"])
        self.assertEqual(path.read_bytes(), b"image 2")
        self.assertTrue(cbw_capture._has_clock(path))
        self.assertEqual(files, ["cbw.png", "cbw.png.clock.txt"])

    def test_a_recapture_without_a_clock_keeps_the_first(self):
        path, calls, files = self.run_capture(["", ""])
        self.assertEqual(len(calls), 2)
        self.assertEqual(path.read_bytes(), b"image 1")
        self.assertEqual(files, ["cbw.png", "cbw.png.clock.txt"])
