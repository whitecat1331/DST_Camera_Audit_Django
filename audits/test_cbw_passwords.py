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
