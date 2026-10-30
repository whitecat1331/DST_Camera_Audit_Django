"""Unit tests for services.cbw_firmware (mocked HTTP, no live CBW)."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase, override_settings

from services.cbw_firmware import read_cbw_firmware


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


@override_settings(CBW_PASSWORD_HINTS_PATH=str(Path(tempfile.mkdtemp(prefix="cbw-fw-hints-")) / "hints.json"))
class ReadCbwFirmwareTests(SimpleTestCase):
    OVERVIEW = "http://10.3.38.177/overview.json"
    FIRMWARE = "http://10.3.38.177/firmware.json"
    STATE = "http://10.3.38.177/state.json"

    def _session_factory(self, routes):
        def factory():
            s = MagicMock()
            s.trust_env = False
            s.get = lambda url, timeout=8: routes.get(url, FakeResponse(200, {}))
            return s

        return factory

    @patch("services.cbw_auth.requests.Session")
    def test_ok(self, Session):
        Session.side_effect = self._session_factory(
            {
                self.OVERVIEW: FakeResponse(
                    200, {"x spc1_partNum": ["X-410-I"], "x spc1_firmRev": ["3.13.7.9"]}
                ),
                self.STATE: FakeResponse(200, {"serialNumber": "00:0C:C8:08:58:F3"}),
            }
        )
        res = read_cbw_firmware("10.3.38.177", "u", ["p"])
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.firmware, "3.13.7.9")
        self.assertEqual(res.part_number, "X-410-I")
        self.assertEqual(res.serial, "00:0C:C8:08:58:F3")

    @patch("services.cbw_auth.requests.Session")
    def test_firmware_json_fallback(self, Session):
        # overview.json lacks the firmware field; firmware.json (v3.12+) has it.
        Session.side_effect = self._session_factory(
            {
                self.OVERVIEW: FakeResponse(200, {"x spc1_wlPartNum": ["X-410-"]}),
                self.FIRMWARE: FakeResponse(
                    200, {"x spc1_partNum": ["X-410-I"], "x spc1_firmRev": ["3.13.7.9"]}
                ),
            }
        )
        res = read_cbw_firmware("10.3.38.177", "u", ["p"])
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.firmware, "3.13.7.9")

    @patch("services.cbw_auth.requests.Session")
    def test_auth_failed(self, Session):
        Session.side_effect = self._session_factory({self.OVERVIEW: FakeResponse(401)})
        res = read_cbw_firmware("10.3.38.177", "u", ["p1", "p2"])
        self.assertEqual(res.status, "auth_failed")

    @patch("services.cbw_auth.requests.Session")
    def test_unreachable(self, Session):
        def factory():
            s = MagicMock()
            s.trust_env = False
            s.get = MagicMock(side_effect=requests.ConnectionError("boom"))
            return s

        Session.side_effect = factory
        res = read_cbw_firmware("10.3.38.177", "u", ["p"])
        self.assertEqual(res.status, "unreachable")

    @patch("services.cbw_auth.requests.Session")
    def test_no_firmware(self, Session):
        # Both endpoints return JSON but neither exposes a firmware field.
        Session.side_effect = self._session_factory(
            {
                self.OVERVIEW: FakeResponse(200, {"x spc1_wlPartNum": ["X-410-"]}),
                self.FIRMWARE: FakeResponse(200, {"x spc1_wlPartNum": ["X-410-"]}),
            }
        )
        res = read_cbw_firmware("10.3.38.177", "u", ["p"])
        self.assertEqual(res.status, "no_firmware")

    def test_no_cbw(self):
        res = read_cbw_firmware("", "u", ["p"])
        self.assertEqual(res.status, "no_cbw")
