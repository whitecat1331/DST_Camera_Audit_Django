"""Unattended-run network phases: netstate, LTI → release → DE order, held publish."""

import shutil
import socket
import tempfile
import threading
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings

from audits import runner
from audits.models import AuditJob
from services import netstate

MEDIA = tempfile.mkdtemp(prefix="dst-netphase-test-")

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1200
  TCP    127.0.0.1:1337         0.0.0.0:0              LISTENING       4242
  TCP    127.0.0.1:1337         127.0.0.1:50211        ESTABLISHED     4242
  TCP    127.0.0.1:9000         0.0.0.0:0              LISTENING       77
"""


class ParseListenPortsTests(SimpleTestCase):
    def test_loopback_listen_ports_of_the_pid_only(self):
        self.assertEqual(netstate.parse_listen_ports(NETSTAT, [4242]), [1337])
        self.assertEqual(netstate.parse_listen_ports(NETSTAT, [1200]), [])  # not loopback
        self.assertEqual(netstate.parse_listen_ports(NETSTAT, []), [])


def fake_management(reply: bytes, *, password: bool = False):
    """One-shot OpenVPN-style management server; returns (port, received bytes list)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got: list[bytes] = []

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.sendall(b"ENTER PASSWORD:" if password else b">INFO:OpenVPN Management Interface Version 1\r\n")
            if password:
                return
            got.append(conn.recv(1024))
            conn.sendall(reply)
        srv.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1], got


class SendSigtermTests(SimpleTestCase):
    def test_sends_signal_and_returns_success(self):
        port, got = fake_management(b"SUCCESS: signal SIGTERM thrown\r\n")
        self.assertEqual(netstate.send_sigterm(port), "SUCCESS: signal SIGTERM thrown")
        self.assertEqual(got, [b"signal SIGTERM\r\n"])

    def test_error_reply_raises(self):
        port, _ = fake_management(b"ERROR: unknown command\r\n")
        with self.assertRaises(netstate.ReleaseError):
            netstate.send_sigterm(port)

    def test_password_prompt_raises(self):
        port, _ = fake_management(b"", password=True)
        with self.assertRaisesRegex(netstate.ReleaseError, "password"):
            netstate.send_sigterm(port)


class ReleaseVpnTests(SimpleTestCase):
    def test_nothing_to_release(self):
        with mock.patch.object(netstate, "vpn_up", return_value=False):
            self.assertEqual(netstate.release_vpn(), netstate.ReleaseResult(True, "VPN was not connected"))

    def release(self, *, tray=True, exit_ok=True, up=(True, False), internet=True, sigterm=None):
        ups = iter(up)
        with (
            mock.patch.object(netstate, "vpn_up", side_effect=lambda: next(ups, up[-1])),
            mock.patch.object(netstate, "_pids", return_value=[22004] if tray else []),
            mock.patch.object(netstate, "_run", return_value="") as run,
            mock.patch.object(netstate, "signal_tray_exit", return_value=exit_ok) as tray_exit,
            mock.patch.object(netstate, "management_port", return_value=1337),
            mock.patch.object(netstate, "send_sigterm", side_effect=sigterm or (lambda port: "SUCCESS")) as sig,
            mock.patch.object(netstate, "internet_ok", return_value=internet),
            mock.patch.object(netstate, "direct_up", return_value=True),
            mock.patch("time.sleep"),
            mock.patch.object(netstate, "TUNNEL_GONE_SECONDS", 0),
        ):
            result = netstate.release_vpn(wait_seconds=0)
        return result, run, tray_exit, sig

    def test_asks_the_tray_app_to_disconnect_and_never_force_closes_it(self):
        result, run, tray_exit, sig = self.release()
        self.assertEqual(result, netstate.ReleaseResult(True, "VPN released; internet OK"))
        tray_exit.assert_called_once_with()
        sig.assert_not_called()
        self.assertFalse(any("taskkill" in c.args[0] for c in run.call_args_list))

    def test_tray_refuses_is_reported_and_nothing_else_is_tried(self):
        result, run, _, sig = self.release(exit_ok=False)
        self.assertFalse(result.released)
        sig.assert_not_called()
        self.assertFalse(any("taskkill" in c.args[0] for c in run.call_args_list))

    def test_no_tray_app_falls_back_to_sigterm(self):
        result, _, tray_exit, sig = self.release(tray=False)
        self.assertTrue(result.released)
        tray_exit.assert_not_called()
        sig.assert_called_once_with(1337)

    def test_refused_sigterm_is_reported_not_raised(self):
        result, *_ = self.release(tray=False, sigterm=netstate.ReleaseError("nope"))
        self.assertEqual(result, netstate.ReleaseResult(False, "nope"))

    def test_tunnel_still_up_is_not_released(self):
        result, *_ = self.release(up=(True,))
        self.assertFalse(result.released)

    def test_no_internet_after_release_is_flagged(self):
        result, *_ = self.release(internet=False)
        self.assertTrue(result.released)
        self.assertFalse(result.network_ok)


def site(pk, key, kind):
    return SimpleNamespace(pk=pk, key=key, kind=kind, fx_numbers=["FX1"] if kind == "de" else [])


# One site at a time here: the in-memory test DB can't be shared across pool threads.
@override_settings(MEDIA_ROOT=MEDIA, DST_POWER_SETTLE_SECONDS=0, DST_LTI_SITE_CONCURRENT=1)
class NetworkPhaseOrderTests(TestCase):
    """_run_dst_audit with every device step stubbed; records what ran in which order."""

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.parent = AuditJob.objects.create(
            pole_number="DST-ALL", target_host="scope=all", device_type=AuditJob.DeviceType.DST_AUDIT,
            status=AuditJob.Status.RUNNING,
        )
        # Mixed order, as the eligible list comes back.
        self.sites = [site(1, "D1", "de"), site(2, "L1", "lti"), site(3, "D2", "de"), site(4, "L2", "lti")]
        self.events: list[str] = []

    def run_audit(
        self,
        *,
        camera_up=True,
        released=True,
        network_ok=True,
        publish_round="",
        lti_dead=(),
        lti_flaky=(),
        de_dead=(),
        no_tv=(),
        ovrc_shots=True,
    ):
        runner.write_dst_selection(self.parent.pk, [s.pk for s in self.sites], publish_round=publish_round)

        def capture(child_pk, inst, child_dir, **kw):
            self.events.append(f"capture {inst.key}")
            job = AuditJob.objects.get(pk=child_pk)
            job.screenshots.create(label="x").image.save("x.png", ContentFile(b"png"), save=True)
            return []

        def release(job_id):
            self.events.append("release")
            if not network_ok:
                return ["no internet; DE skipped"], False
            return ([] if released else ["VPN release failed (x); DE captures ran on the VPN"]), True

        def lti_power(inst):
            self.events.append(f"relay {inst.key}")
            if inst.key in lti_dead or (inst.key in lti_flaky and self.events.count(f"relay {inst.key}") == 1):
                return inst.key, f"{inst.key}: Failed to turn on relays", False
            return inst.key, None, False

        def de_power(job_id, sites):
            self.events.append("ovrc dcam on")
            dead = {s.key for s in sites if s.key in de_dead}
            return len(sites) - len(dead), False, [f"power-on {k}: OvrC DCAM failed" for k in sorted(dead)]

        def ovrc_times(job_id, assignments, fleet_dir):
            self.events.append("ovrc " + ",".join(key for _pk, key, _fx, _dir in assignments))
            for child_pk, _key, _fx, _dir in assignments:
                if ovrc_shots:
                    job = AuditJob.objects.get(pk=child_pk)
                    job.screenshots.create(label="ovrc_fx1").image.save("o.png", ContentFile(b"png"), save=True)
                    AuditJob.objects.filter(pk=child_pk).update(status=AuditJob.Status.SUCCEEDED)
            return []

        with (
            mock.patch.object(runner, "_active_dst_installations", return_value=self.sites),
            mock.patch.object(runner, "_dst_site_kind", side_effect=lambda s: s.kind),
            mock.patch.object(runner, "_dst_site_key", side_effect=lambda s: s.key),
            mock.patch.object(runner, "_power_on_lti_site", side_effect=lti_power),
            mock.patch.object(runner, "_power_on_all_de_sites", side_effect=de_power),
            mock.patch.object(runner, "_de_has_teamviewer", side_effect=lambda s: s.key not in no_tv),
            mock.patch.object(runner, "_dst_camera_network_up", return_value=camera_up),
            mock.patch.object(runner, "_dst_release_vpn", side_effect=release),
            mock.patch.object(runner, "_run_dst_site_capture", side_effect=capture),
            mock.patch.object(runner, "_dst_fleet_ovrc_times", side_effect=ovrc_times),
        ):
            return runner._run_dst_audit(self.parent.pk, runner.Path(MEDIA) / "run")

    def test_lti_half_then_release_then_de_half_never_interleaved(self):
        errors, ok = self.run_audit()
        self.assertEqual(ok, 4)
        self.assertEqual(errors, [])
        # DE power-on (OvrC) waits until the VPN is gone, like the DE captures.
        self.assertEqual(
            self.events[2:],
            ["capture L1", "capture L2", "release", "ovrc dcam on", "capture D1", "capture D2", "ovrc D1,D2"],
        )
        self.assertEqual(sorted(self.events[:2]), ["relay L1", "relay L2"])

    def test_camera_network_down_skips_lti_and_reports(self):
        errors, ok = self.run_audit(camera_up=False)
        self.assertEqual(ok, 2)
        self.assertIn("LTI skipped (2 site(s))", errors[0])
        self.assertEqual(
            self.events, ["release", "ovrc dcam on", "capture D1", "capture D2", "ovrc D1,D2"]
        )  # no relays, no LTI captures

    def test_failed_release_is_a_warning_and_de_still_runs(self):
        errors, ok = self.run_audit(released=False)
        self.assertEqual(ok, 4)
        self.assertEqual(errors, ["VPN release failed (x); DE captures ran on the VPN"])

    def test_no_internet_after_release_skips_de(self):
        errors, ok = self.run_audit(network_ok=False)
        self.assertEqual(ok, 2)
        self.assertEqual(errors, ["no internet; DE skipped"])
        self.assertEqual(self.events[2:], ["capture L1", "capture L2", "release"])

    def test_lti_only_run_that_publishes_releases_at_the_end(self):
        self.sites = [s for s in self.sites if s.kind == "lti"]
        self.run_audit(publish_round="R1")
        self.assertEqual(self.events[-1], "release")
        self.sites = self.sites[:1]
        self.events = []
        self.run_audit()  # no publish round → VPN left alone
        self.assertNotIn("release", self.events)

    def children(self):
        return {c.pole_number: c for c in AuditJob.objects.filter(parent_job=self.parent)}

    def test_lti_site_whose_power_on_failed_is_skipped_not_tried(self):
        errors, ok = self.run_audit(lti_dead={"L2"})
        self.assertNotIn("capture L2", self.events)
        self.assertEqual(ok, 3)
        kids = self.children()
        self.assertEqual(kids["L2"].status, AuditJob.Status.SKIPPED)
        self.assertIn("CBW power-on failed twice", kids["L2"].error_message)
        self.assertEqual(kids["L1"].status, AuditJob.Status.SUCCEEDED)
        self.assertIn("Skipped 1 offline site(s) without trying them", errors)
        self.assertEqual(
            [c.pole_number for c in AuditJob.objects.filter(parent_job=self.parent).order_by("pk")],
            ["L1", "L2", "D1", "D2"],
        )  # a skipped site still gets its row, in run order

    def test_power_on_that_fails_once_is_retried_and_the_site_captured(self):
        errors, ok = self.run_audit(lti_flaky={"L2"})
        self.assertEqual(self.events.count("relay L2"), 2)
        self.assertEqual(self.events.count("relay L1"), 1)  # only failures are retried
        self.assertIn("capture L2", self.events)
        self.assertEqual((ok, errors), (4, []))

    def test_de_site_whose_ovrc_power_on_failed_is_still_tried(self):
        # OvrC timing out isn't proof (FX1173: OvrC fails, TeamViewer answers).
        errors, ok = self.run_audit(de_dead={"D1"})
        self.assertIn("capture D1", self.events)
        self.assertEqual(self.children()["D1"].status, AuditJob.Status.SUCCEEDED)
        self.assertEqual(ok, 4)
        self.assertIn("power-on D1: OvrC DCAM failed", errors)

    def test_de_site_without_teamviewer_id_gets_the_ovrc_time_only(self):
        errors, ok = self.run_audit(no_tv={"D1"})
        self.assertNotIn("capture D1", self.events)
        self.assertIn("ovrc D1,D2", self.events)  # still in the OvrC pass
        d1 = self.children()["D1"]
        self.assertEqual(d1.status, AuditJob.Status.SUCCEEDED)
        self.assertEqual(d1.error_message, "")
        self.assertEqual(ok, 4)
        self.assertEqual(errors, [])

    def test_ovrc_only_site_without_an_ovrc_time_fails(self):
        errors, ok = self.run_audit(no_tv={"D1"}, ovrc_shots=False)
        self.assertEqual(self.children()["D1"].status, AuditJob.Status.FAILED)
        self.assertIn("D1: OvrC time not captured", errors)

    def test_child_jobs_are_numbered_in_run_order(self):
        self.run_audit()
        children = AuditJob.objects.filter(parent_job=self.parent).order_by("pk")
        self.assertEqual([c.pole_number for c in children], ["L1", "L2", "D1", "D2"])


@override_settings(MEDIA_ROOT=MEDIA)
class PublishAfterRunTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("tech")

    def test_held_until_reachable_then_publishes(self):
        from audits import publish

        answers = iter([False, False, True])
        with (
            mock.patch.object(publish, "_execute_publish") as execute,
            mock.patch("time.sleep") as sleep,
        ):
            execute.side_effect = lambda *a: publish._publish_lock.release()
            job = publish.publish_after_run(
                7, "R1", self.user, wait_seconds=600, reachable=lambda: next(answers)
            )
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(execute.call_args.args[1:], ([7], "R1", self.user))
        self.assertEqual(job.device_type, AuditJob.DeviceType.PUBLISH)
        self.assertFalse(publish._publish_lock.locked())

    def test_gives_up_after_the_wait_and_says_how_to_finish(self):
        from audits import publish

        with mock.patch.object(publish, "_execute_publish") as execute:
            job = publish.publish_after_run(7, "R1", self.user, wait_seconds=0, reachable=lambda: False)
        execute.assert_not_called()
        self.assertEqual(job.status, AuditJob.Status.FAILED)
        job.refresh_from_db()
        self.assertEqual(job.progress_message, "Publish held: shared DB not reachable")
        self.assertIn("publish run #7", job.error_message)

    def test_run_with_no_round_publishes_to_the_dated_default(self):
        from audits import publish

        sites = [site(5, "L9", "lti")]
        with (
            mock.patch.object(runner, "_active_dst_installations", return_value=sites),
            mock.patch.object(publish, "shared_configured", return_value=True),
            override_settings(DST_ROLE="capture"),
        ):
            job, _ = runner.create_dst_audit_job(
                self.user, scope="all", state="", agency="", installation_ids=[5]
            )
            typed, _ = runner.create_dst_audit_job(
                self.user, scope="all", state="", agency="", installation_ids=[5], publish_round="R2"
            )
        # Date and time, so two runs on the same day get their own rounds.
        self.assertRegex(runner.read_dst_publish_round(job.pk), r"^DST audit \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertEqual(runner.read_dst_publish_round(typed.pk), "R2")
        with mock.patch.object(publish, "shared_configured", return_value=False):
            self.assertEqual(runner.default_publish_round(), "")  # can't publish here → no round

    def test_start_request_validates_the_round(self):
        runner.write_dst_selection(99, [1], publish_round="R1")
        self.assertEqual(runner.read_dst_publish_round(99), "R1")
        runner.write_dst_selection(98, [1])
        self.assertEqual(runner.read_dst_publish_round(98), "")


class StaleTeamViewerWindowTests(SimpleTestCase):
    def test_windows_open_before_connect_are_never_the_new_session(self):
        from services import teamviewer_capture as tv

        windows = [(11, "FX1361 L2 - TeamViewer", 900_000), (22, "FX1260 L1 - TeamViewer", 800_000)]
        with (
            mock.patch.object(tv, "_enum_session_windows", return_value=windows),
            mock.patch.object(tv.ctypes, "windll", create=True) as windll,
        ):
            windll.user32.IsWindow.return_value = True
            self.assertEqual(tv._find_session_window(None, "123").title, "FX1361 L2 - TeamViewer")
            self.assertEqual(
                tv._find_session_window(None, "123", exclude=frozenset({11})).title,
                "FX1260 L1 - TeamViewer",
            )
            self.assertIsNone(tv._find_session_window(None, "123", exclude=frozenset({11, 22})))


def de_site(key, *fx):
    return SimpleNamespace(pole_number=key, identifier=key, ims_id=0, fx_numbers=list(fx))


@override_settings(OVRC_USERNAME="u", OVRC_PASSWORD="p")
class DePowerOnWarningTests(TestCase):
    """OvrC power-on failures are per-site warnings; DE sites are never skipped on them."""

    def power_on(self, sites, *, results=None, raises=None):
        from services import ovrc_capture

        def fake(fx_numbers, **kw):
            self.assertTrue(kw.get("continue_on_error"))
            if raises:
                raise raises
            return results

        with mock.patch.object(ovrc_capture, "ensure_dcam_on_for_fxes", side_effect=fake):
            return runner._power_on_all_de_sites(0, sites)

    def test_a_failed_fx_is_a_warning_for_its_site_only(self):
        from services.ovrc_capture import OvrcDcamResult

        ok, _changed, errors = self.power_on(
            [de_site("A", "FX1"), de_site("B", "FX2", "FX3")],
            results=[
                OvrcDcamResult("FX1", "already_on"),
                OvrcDcamResult("FX2", "turned_on"),
                OvrcDcamResult("FX3", "failed", "TimeoutException: "),
            ],
        )
        self.assertEqual(ok, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("power-on B: OvrC DCAM failed (FX3: TimeoutException", errors[0])

    def test_a_failed_ovrc_login_is_one_warning(self):
        ok, _changed, errors = self.power_on(
            [de_site("A", "FX1")], raises=RuntimeError("login page timed out")
        )
        self.assertEqual(ok, 0)
        self.assertIn("RuntimeError: login page timed out", errors[0])


class EnsureDcamContinueOnErrorTests(SimpleTestCase):
    def run_fleet(self, **kw):
        from services import ovrc_capture

        client = mock.MagicMock()
        client.__enter__.return_value = client

        def one(fx):
            if fx == "FX2":
                raise TimeoutError("no WattBox")
            return ovrc_capture.OvrcDcamResult(fx, "already_on")

        client.ensure_dcam_system_on.side_effect = one
        with mock.patch.object(ovrc_capture, "OvrcClient", return_value=client):
            return ovrc_capture.ensure_dcam_on_for_fxes(["FX1", "FX2", "FX3"], "u", "p", **kw)

    def test_one_dead_fx_does_not_stop_the_rest(self):
        results = self.run_fleet(continue_on_error=True)
        self.assertEqual([(r.fx_number, r.action) for r in results], [("FX1", "already_on"), ("FX2", "failed"), ("FX3", "already_on")])
        self.assertIn("TimeoutError: no WattBox", results[1].detail)

    def test_default_still_raises_for_other_callers(self):
        with self.assertRaises(TimeoutError):
            self.run_fleet()


class LtiSitePoolTests(SimpleTestCase):
    """_dst_capture_half with a pool: every site once, counts add up, cancel stops the rest."""

    def make_run(self):
        return runner._DstRun(0, None, 8, runner.Path(MEDIA))

    def test_every_site_captured_once_with_overlap(self):
        run = self.make_run()
        seen, active, peak = [], [0], [0]
        lock = threading.Lock()

        def capture(run_, inst, index):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            threading.Event().wait(0.05)
            with lock:
                active[0] -= 1
                seen.append((inst, index))
            run_.record("ok" if inst % 2 else "skipped")

        with (
            mock.patch.object(runner, "_dst_capture_site", side_effect=capture),
            mock.patch.object(runner, "_dst_log"),
            mock.patch("audits.models.AuditJob.objects"),
        ):
            runner._dst_capture_half(run, list(range(8)), "LTI", workers=4)
        self.assertEqual(sorted(seen), [(i, i + 1) for i in range(8)])
        self.assertGreater(peak[0], 1)
        self.assertLessEqual(peak[0], 4)
        self.assertEqual((run.done, run.sites_ok, run.skipped), (8, 4, 4))
        self.assertEqual(run.record("failed"), "LTI: 9/8 · 4 ok · 1 failed · 4 skipped")

    def test_cancel_in_one_site_stops_sites_not_yet_started(self):
        from services.teamviewer_capture import CaptureCancelled

        run = self.make_run()
        started = []

        def capture(run_, inst, index):
            started.append(inst)
            if inst == 0:
                raise CaptureCancelled("Audit cancelled by user")
            threading.Event().wait(0.05)

        with (
            mock.patch.object(runner, "_dst_capture_site", side_effect=capture),
            mock.patch.object(runner, "_dst_log"),
            mock.patch("audits.models.AuditJob.objects"),
        ):
            with self.assertRaises(CaptureCancelled):
                runner._dst_capture_half(run, list(range(40)), "LTI", workers=2)
        self.assertLess(len(started), 40)
