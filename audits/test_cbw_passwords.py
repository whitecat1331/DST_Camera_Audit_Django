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
    @staticmethod
    def menu_page(followed=False):
        driver = mock.Mock(current_url="http://10.3.201.177/setup.html#")
        driver.execute_script.return_value = followed
        return driver

    def test_a_click_that_loads_nothing_is_retried_on_a_fresh_link(self):
        """233201 (2026-10-29): a late menu rebuild swallowed the click twice in a row."""
        driver = self.menu_page()
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
        driver = self.menu_page()
        with mock.patch.object(cbw_capture, "_wait_for_date_time_page", return_value=False):
            self.assertFalse(cbw_capture._open_date_time(driver, lambda: [object()], "h"))
        self.assertEqual(driver.execute_script.call_count, cbw_capture._DATE_TIME_CLICKS)

    def test_a_click_the_menu_did_not_handle_is_retried_not_waited_on(self):
        """233127 (2026-10-29): the click opened the bare date-time.html template."""
        driver = self.menu_page()
        # Blocked more times than there are tries: blocked clicks don't use one up.
        driver.execute_script.side_effect = [True] * (cbw_capture._DATE_TIME_CLICKS + 2) + [False]
        with (
            mock.patch.object(cbw_capture, "_wait_for_date_time_page", return_value=True) as waited,
            mock.patch.object(cbw_capture.time, "sleep"),
        ):
            self.assertTrue(cbw_capture._open_date_time(driver, lambda: [object()], "10.3.127.177"))
        self.assertEqual(waited.call_count, 1)

    def test_a_menu_that_never_handles_clicks_gives_up(self):
        driver = self.menu_page(followed=True)
        clock = iter(range(0, 1000, 5))
        with (
            mock.patch.object(cbw_capture.time, "sleep"),
            mock.patch.object(cbw_capture.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            self.assertFalse(cbw_capture._open_date_time(driver, lambda: [object()], "h"))
        driver.back.assert_not_called()

    def test_a_page_that_left_the_menu_goes_back(self):
        driver = self.menu_page()
        driver.current_url = "http://10.3.127.177/date-time.html"
        with (
            mock.patch.object(cbw_capture, "_wait_for_date_time_page", return_value=True),
            mock.patch.object(cbw_capture, "_wait_for_menu_settled"),
        ):
            self.assertFalse(cbw_capture._open_date_time(driver, lambda: [object()], "10.3.127.177"))
        self.assertEqual(driver.back.call_count, cbw_capture._DATE_TIME_CLICKS)


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


class CbwNetworkProblemsTests(SimpleTestCase):
    @staticmethod
    def ev(method, **params):
        return {"message": json.dumps({"message": {"method": method, "params": params}})}

    def test_reports_failed_unfinished_and_error_requests_only(self):
        ev = self.ev
        log = [
            ev("Network.requestWillBeSent", requestId="1", timestamp=1.0, request={"url": "http://h/setup.html"}),
            ev("Network.responseReceived", requestId="1", response={"status": 200}),
            ev("Network.loadingFinished", requestId="1", timestamp=1.5),
            ev("Network.requestWillBeSent", requestId="2", timestamp=2.0, request={"url": "http://h/datetime.html?x=1"}),
            ev("Network.requestWillBeSent", requestId="3", timestamp=2.0, request={"url": "http://h/menu.js"}),
            ev("Network.loadingFailed", requestId="3", errorText="net::ERR_CONNECTION_RESET"),
            ev("Network.requestWillBeSent", requestId="4", timestamp=2.0, request={"url": "http://h/state.json"}),
            ev("Network.responseReceived", requestId="4", response={"status": 503}),
            ev("Network.loadingFinished", requestId="4", timestamp=2.1),
            {"message": "not json"},
        ]
        net = cbw_capture.network_problems(log)
        self.assertEqual(net["requests"], 4)
        self.assertEqual(
            net["problems"],
            [
                {"url": "http://h/datetime.html", "state": "pending"},
                {"url": "http://h/menu.js", "state": "failed", "error": "net::ERR_CONNECTION_RESET"},
                {"url": "http://h/state.json", "state": "done", "status": 503, "ms": 100},
            ],
        )
        self.assertEqual(net["slowest"][0], {"url": "http://h/setup.html", "ms": 500})


class CbwDateTimeFinderTests(SimpleTestCase):
    def test_the_link_by_address_wins_over_text_matches(self):
        """233201: a text XPath returned the hidden <li> before its <a>; the <li> ignores clicks."""
        driver = mock.Mock()
        link = object()
        driver.find_elements.side_effect = lambda by, sel: [link] if "date-time" in sel else ["li", "a"]
        self.assertEqual(cbw_capture._find_date_time_elements(driver), [link])

    def test_text_fallback_prefers_anchors(self):
        driver = mock.Mock()
        driver.find_elements.side_effect = lambda by, sel: ["a"] if sel.startswith("//a") else (["li", "a"] if sel.startswith("//*") else [])
        self.assertEqual(cbw_capture._find_date_time_elements(driver), ["a"])
