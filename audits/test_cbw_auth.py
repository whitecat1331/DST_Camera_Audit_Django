"""CBW login lockout (E4): two failed logins → 429 for ~30 s, even for the right password."""

import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

from audits import runner
from services import cbw_auth, cbw_capture, cbw_relays
from services.cbw_auth import CbwAuthError, CbwUnreachable, cbw_login

HOST = "10.1.233.177"
PASSWORDS = ["p1", "p2", "p3"]


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


class FakeCbw:
    """Answers each login from a script of statuses, then `after` for the rest.

    `logins` records the password of every login, `gets` every URL on any session.
    """

    def __init__(self, *script, after=200, payload=None):
        self.script = list(script)
        self.after = after
        self.payload = payload or {"relay1": 0, "relay2": 1}
        self.logins: list[str] = []
        self.gets: list[str] = []

    def session(self):
        cbw = self
        s = mock.MagicMock()
        s.auth = None
        first = [True]

        def get(url, timeout=None):
            cbw.gets.append(url)
            if not first[0]:
                return FakeResponse(200, cbw.payload)  # later calls on a logged-in session
            first[0] = False
            cbw.logins.append(s.auth[1])
            status = cbw.script.pop(0) if cbw.script else cbw.after
            return FakeResponse(status, cbw.payload)

        s.get.side_effect = get
        return s


class HintsMixin:
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cbw-auth-"))
        self.hints = self.dir / "hints.json"
        override = override_settings(CBW_PASSWORD_HINTS_PATH=str(self.hints))
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.sleeps: list[float] = []

    def login(self, cbw: FakeCbw, **kw):
        with mock.patch.object(cbw_auth.requests, "Session", side_effect=cbw.session):
            return cbw_login(HOST, "admin", PASSWORDS, sleep=self.sleeps.append, **kw)


class CbwLoginTests(HintsMixin, SimpleTestCase):
    def test_401_401_429_200_waits_and_retries_the_same_password(self):
        cbw = FakeCbw(401, 401, 429, 200)
        login = self.login(cbw)
        self.assertEqual(login.index, 3)  # the third password (index 2), as on 233006 / 233043
        self.assertEqual(cbw.logins, ["p1", "p2", "p3", "p3"])  # never advanced past p3
        self.assertEqual(self.sleeps, [cbw_auth.LOCKOUT_WAIT_SECONDS])
        self.assertEqual(json.loads(self.hints.read_text()), {f"{HOST}/state.json": 3})
        self.assertNotIn("p3", self.hints.read_text())  # the number, never the password

    def test_remembered_password_first_logs_in_with_no_401_or_429(self):
        self.hints.write_text(json.dumps({f"{HOST}/state.json": 3}))
        cbw = FakeCbw(after=200)
        self.assertEqual(self.login(cbw).index, 3)
        self.assertEqual(cbw.logins, ["p3"])
        self.assertEqual(self.sleeps, [])

    def test_a_real_401_on_every_password_is_auth_failed(self):
        with self.assertRaises(CbwAuthError) as ctx:
            self.login(FakeCbw(after=401))
        self.assertEqual(ctx.exception.reason, "auth_failed")
        self.assertEqual(self.sleeps, [])

    def test_a_lockout_that_never_clears_is_locked_out(self):
        cbw = FakeCbw(401, 401, after=429)
        with self.assertRaises(CbwAuthError) as ctx:
            self.login(cbw)
        self.assertEqual(ctx.exception.reason, "locked_out")
        self.assertEqual(cbw.logins, ["p1", "p2", "p3", "p3", "p3"])
        self.assertEqual(len(self.sleeps), cbw_auth.LOCKOUT_RETRIES)

    def test_no_answer_is_unreachable(self):
        def dead():
            s = mock.MagicMock()
            s.get.side_effect = requests.ConnectTimeout("x")
            return s

        with mock.patch.object(cbw_auth.requests, "Session", side_effect=dead):
            with self.assertRaises(CbwUnreachable):
                cbw_login(HOST, "admin", PASSWORDS, sleep=self.sleeps.append)

    def test_setup_page_hint_keeps_the_bare_host_key(self):
        self.login(FakeCbw(401, 200), path="/setup.html")
        self.assertEqual(json.loads(self.hints.read_text()), {HOST: 2})


class ProbeTests(HintsMixin, SimpleTestCase):
    def probe(self, cbw):
        with (
            mock.patch.object(cbw_auth.requests, "Session", side_effect=cbw.session),
            mock.patch.object(cbw_auth.time, "sleep", side_effect=self.sleeps.append),
        ):
            return cbw_capture._probe_cbw_auth(HOST, "admin", PASSWORDS)

    def test_a_429_is_not_counted_as_accepted(self):
        # Was: p3's 429 counted as accepted (< 500). Here the lockout clears to a 401.
        cbw = FakeCbw(401, 401, 429, after=401)
        with self.assertRaises(CbwAuthError) as ctx:
            self.probe(cbw)
        self.assertEqual(ctx.exception.reason, "auth_failed")
        self.assertEqual(cbw.logins, ["p1", "p2", "p3", "p3"])

    def test_waits_out_the_lockout_and_returns_the_password_number(self):
        self.assertEqual(self.probe(FakeCbw(401, 401, 429, 200)), 3)
        self.assertEqual(self.sleeps, [cbw_auth.LOCKOUT_WAIT_SECONDS])

    def test_every_password_rejected_raises(self):
        with self.assertRaises(CbwAuthError):
            self.probe(FakeCbw(after=401))

    def test_no_answer_means_no_information(self):
        def dead():
            s = mock.MagicMock()
            s.get.side_effect = requests.ConnectionError("x")
            return s

        with mock.patch.object(cbw_auth.requests, "Session", side_effect=dead):
            self.assertIsNone(cbw_capture._probe_cbw_auth(HOST, "admin", PASSWORDS))


class RelayTests(HintsMixin, SimpleTestCase):
    def relays_on(self, cbw):
        with (
            mock.patch.object(cbw_auth.requests, "Session", side_effect=cbw.session),
            mock.patch.object(cbw_auth.time, "sleep", side_effect=self.sleeps.append),
            mock.patch.object(cbw_relays, "pole_to_ip_address", return_value=HOST),
        ):
            return cbw_relays.turn_all_relays_on("233006", "admin", PASSWORDS)

    def test_lockout_then_right_password_turns_relays_on_in_one_session(self):
        cbw = FakeCbw(401, 401, 429, 200)
        result = self.relays_on(cbw)
        self.assertEqual(result.action, "turned_on")
        self.assertEqual(cbw.logins, ["p1", "p2", "p3", "p3"])
        self.assertEqual(cbw.gets[-1], f"http://{HOST}/state.json?relay1=1&relay2=1")

    def test_auth_failed_is_raised_for_the_runner(self):
        with self.assertRaises(CbwAuthError):
            self.relays_on(FakeCbw(after=401))


class VncOnlyBundleTests(SimpleTestCase):
    """D3: a CBW no password gets into → no CBW capture, VNC still runs."""

    def test_cbw_skip_runs_vnc_and_records_the_reason(self):
        ran: list[str] = []
        with (
            mock.patch.object(runner, "_lane2_present", return_value=False),
            mock.patch.object(runner, "_capture_cbw_step", side_effect=lambda *a: ran.append("cbw") or ("cbw", None)),
            mock.patch.object(
                runner, "_capture_vnc_step", side_effect=lambda job, lane, host, label, out: ran.append(label) or (label, None)
            ),
            mock.patch("audits.models.AuditJob.objects") as jobs,
        ):
            jobs.filter.return_value.update.return_value = 1
            errors = runner._run_pole_bundle_parallel(1, "233006", Path("."), cbw_skip="CBW auth_failed @ h: all 3 got 401")
        self.assertEqual(ran, ["vnc_l1"])
        self.assertEqual(errors, ["CBW: CBW auth_failed @ h: all 3 got 401; capture skipped"])
